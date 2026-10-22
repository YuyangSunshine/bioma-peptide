"""Layer B gates: admission must reject before any execution occurs.

Covers stale proposals (I2), missing capability, failed preconditions, risk
above the capability ceiling, execution without a mandate (I3), and a mandate
that was invalidated during execution.
"""
from __future__ import annotations

import time

import pytest

from bioma.core.action_contracts import Predicate
from bioma.core.models import Evidence, StateDelta
from bioma.core.protocol import (
    ScientificExecutionMandate,
    ScientificTransitionProposal,
)
from bioma.core.protocol_invariants import (
    MissingMandate,
    StaleProposal,
    assert_commit_version,
    assert_fresh_proposal,
    assert_mandated,
)
from bioma.core.transitions import ExecutionPermit, TransitionProposal
from bioma.runtime.admission import TransitionAdmission
from bioma.runtime.ledger import RunLedger


class SimpleProposal:
    """Minimal stand-in carrying only the proposal identity a mandate binds."""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.proposal_ref = "proposal:" + identity


def rejected_codes(ledger, task_id="protocol"):
    return [event.payload.get("code") for event in ledger.events(task_id)
            if event.kind == "TransitionRejected"]


def attempted_statuses(ledger, task_id="protocol"):
    return [event.payload.get("status") for event in ledger.events(task_id)
            if event.kind == "ExecutionAttemptFailed"]


def test_layer_b_aliases_are_the_existing_objects():
    assert ScientificExecutionMandate is ExecutionPermit
    assert ScientificTransitionProposal is TransitionProposal


def test_stale_parent_state_rejects_proposal(protocol):
    state = protocol.state()
    actor = protocol.actor()
    proposal = actor.default_proposal(state, protocol.assignment())
    assert_fresh_proposal(proposal, state)

    # I2a: a proposal anchored on a future state version is stale.
    future = proposal.model_copy(update={"base_version": state.version + 1})
    with pytest.raises(StaleProposal):
        assert_fresh_proposal(future, state)

    # I2b: commit-time compare-and-swap rejects a stale delta.
    delta = StateDelta(agent_id=actor.owner, source_message_id="m",
                       expected_version=state.version)
    assert_commit_version(delta, state)
    with pytest.raises(StaleProposal):
        assert_commit_version(
            delta.model_copy(update={"expected_version": state.version + 1}), state)

    # The same rule is enforced by the state machine itself.
    committed = state.commit(delta.model_copy(update={"status": "verified"}))
    assert committed.version == state.version + 1
    with pytest.raises(ValueError, match="Stale StateDelta"):
        committed.commit(delta.model_copy(update={"status": "verified"}))


def test_missing_capability_rejects_admission(protocol, tmp_path):
    ledger = RunLedger(tmp_path / "run.db")
    actor = protocol.actor()
    message = protocol.assignment(payload={"selected_capability": "no.such.capability"})
    with pytest.raises(ValueError):
        TransitionAdmission(ledger).execute(actor, protocol.state(), message)
    # No mandate and no receipt may exist for a transition that was never admitted.
    kinds = [event.kind for event in ledger.events("protocol")]
    assert "ExecutionPermitted" not in kinds
    assert "ExecutionResultRecorded" not in kinds


def test_failed_precondition_rejects_admission(protocol, tmp_path):
    ledger = RunLedger(tmp_path / "run.db")
    capability = protocol.capability(preconditions=[
        Predicate(id="positive_volume", source="state", path="metrics.volume",
                  operator="gte", value=1)])
    actor = protocol.actor(capability=capability)
    with pytest.raises(ValueError, match="precondition"):
        TransitionAdmission(ledger).execute(actor, protocol.state(), protocol.assignment())
    assert "precondition_failed" in rejected_codes(ledger)


def test_risk_above_ceiling_rejects_admission(protocol, tmp_path):
    ledger = RunLedger(tmp_path / "run.db")
    # The action contract claims high risk; the capability only authorizes low.
    actor = protocol.actor(capability=protocol.capability(risk="low", contract_risk="high"))
    with pytest.raises(ValueError, match="Risk ceiling"):
        TransitionAdmission(ledger).execute(actor, protocol.state(), protocol.assignment())
    assert "risk_above_ceiling" in rejected_codes(ledger)


def test_execution_without_mandate_is_forbidden(chain):
    run = chain()
    permit = ExecutionPermit.model_validate(run.delta.transition["permit"])
    # A transition that ran through admission always carries a live mandate.
    assert_mandated(permit, now=permit.issued_at)
    assert permit.decision == "authorized"
    assert permit.mandate_id.startswith("mandate:")
    # The mandate is durable: the journaled permit is the same object.
    journaled = [event for event in run.ledger.events("protocol")
                 if event.kind == "ExecutionPermitted"]
    assert len(journaled) == 1
    assert journaled[0].payload == run.delta.transition["permit"]
    # Execution without any mandate is a protocol violation.
    with pytest.raises(MissingMandate):
        assert_mandated(None)


def test_forged_mandate_cannot_be_committed(chain):
    run = chain()
    forged = run.delta.model_copy(deep=True)
    forged.transition["permit"]["expires_at"] = 0.0
    with pytest.raises(ValueError, match="journaled admission evidence"):
        run.ledger.commit_transition(
            run.state, forged.model_copy(update={"status": "verified"}),
            run.message, run.receipt)


def test_expired_mandate_is_forbidden():
    expired = ExecutionPermit(
        proposal_id="proposal-x", provider_id="predagent", implementation_hash="h",
        contract_hash="c", admitted_version=0, expires_at=time.time() - 1,
        issued_at=time.time() - 100)
    assert expired.authorizes(SimpleProposal(identity="proposal-x"))
    with pytest.raises(MissingMandate, match="expired"):
        assert_mandated(expired, now=time.time())


def test_authority_changed_during_execution_is_rejected(protocol, tmp_path):
    ledger = RunLedger(tmp_path / "run.db")
    actor = protocol.actor()

    def invalidate(state, message):
        actor.capabilities.clear()  # the executor's authority changed mid-flight
        return StateDelta(agent_id=actor.owner, source_message_id=message.id,
                          expected_version=state.version,
                          updates={"metrics": {"value": 1}},
                          evidence=[Evidence(kind="prediction", source=actor.owner,
                                             claim="value computed")])

    actor.handler = invalidate
    with pytest.raises(ValueError, match="changed during execution"):
        TransitionAdmission(ledger).execute(actor, protocol.state(), protocol.assignment())
    assert attempted_statuses(ledger) == ["failed"]
