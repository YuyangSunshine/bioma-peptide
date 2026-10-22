"""Receipts and effect confinement (I4, I5).

A mandate authorizes exactly one proposal and exactly one set of effects. The
receipt records what actually happened and must agree with both.
"""
from __future__ import annotations

import time

import pytest

from bioma.core.models import StateDelta
from bioma.core.protocol_invariants import (
    EffectOutsideMandate,
    MandateProposalMismatch,
    assert_effects_confined,
    assert_mandate_bound,
)
from bioma.core.transitions import (
    ExecutionPermit,
    ExecutionResult,
    TransitionProposal,
    verify_transition_proof,
)
from bioma.runtime.admission import TransitionAdmission
from bioma.runtime.ledger import RunLedger


def test_receipt_matches_proposal_and_mandate(chain):
    run = chain()
    proposal = TransitionProposal.model_validate(run.delta.transition["proposal"])
    permit = ExecutionPermit.model_validate(run.delta.transition["permit"])
    receipt = ExecutionResult.model_validate(run.delta.transition["result"])

    # One identity threads proposal -> mandate -> receipt.
    assert receipt.proposal_id == permit.proposal_id == proposal.identity
    assert receipt.mandate_id == permit.mandate_id
    assert receipt.receipt_id.startswith("receipt:")
    assert proposal.proposal_ref == "proposal:" + proposal.identity

    # The receipt describes reality, not intent.
    assert receipt.executor == run.actor.owner
    assert receipt.execution_status == "completed"
    assert receipt.work_outcome == run.delta.outcome
    assert receipt.actual_effects == sorted(run.delta.updates)
    assert receipt.idempotency_key == run.message.id
    assert receipt.evidence_ids == [item.id for item in run.delta.evidence]
    assert receipt.started_at is not None and receipt.ended_at >= receipt.started_at

    # The mandate carries the authority ceiling that admission derived.
    assert permit.allowed_effects == ["metrics"]
    assert permit.risk_ceiling == "low"
    assert permit.evidence_obligations == ["transition_integrity"]
    assert permit.revocable is True
    assert permit.issued_at is not None


def test_mandate_bound_to_a_single_proposal(chain):
    run = chain()
    proposal = TransitionProposal.model_validate(run.delta.transition["proposal"])
    permit = ExecutionPermit.model_validate(run.delta.transition["permit"])
    assert_mandate_bound(permit, proposal)
    other = permit.model_copy(update={"proposal_id": "some-other-proposal"})
    with pytest.raises(MandateProposalMismatch):
        assert_mandate_bound(other, proposal)


def test_receipt_claiming_another_proposal_is_rejected(chain):
    run = chain()
    tampered = run.delta.model_copy(deep=True)
    tampered.transition["result"]["proposal_id"] = "some-other-proposal"
    with pytest.raises(ValueError, match="identity mismatch"):
        verify_transition_proof(run.state, tampered, run.message)


def test_actual_effect_outside_allowed_effects_is_rejected(protocol, tmp_path):
    ledger = RunLedger(tmp_path / "run.db")
    # The admitted contract permits writing only `metrics`.
    actor = protocol.actor(capability=protocol.capability(writable=("metrics",)))

    def overreach(state, message):
        return StateDelta(agent_id=actor.owner, source_message_id=message.id,
                          expected_version=state.version,
                          updates={"protocol": {"stolen": True}},
                          outcome="completed")

    actor.handler = overreach
    with pytest.raises(ValueError, match="exceed proposed write set"):
        TransitionAdmission(ledger).execute(actor, protocol.state(), protocol.assignment())

    kinds = [event.kind for event in ledger.events("protocol")]
    assert "ExecutionResultRecorded" not in kinds
    assert "TransitionCommitted" not in kinds
    assert [event.payload["status"] for event in ledger.events("protocol")
            if event.kind == "ExecutionAttemptFailed"] == ["failed"]


def test_effect_confinement_invariant_is_explicit():
    permit = ExecutionPermit(
        proposal_id="proposal-x", provider_id="predagent", implementation_hash="h",
        contract_hash="c", admitted_version=0, expires_at=time.time() + 600,
        allowed_effects=["metrics"])
    assert_effects_confined(permit, ["metrics"])
    assert_effects_confined(permit, [])
    with pytest.raises(EffectOutsideMandate):
        assert_effects_confined(permit, ["metrics", "protocol"])
    with pytest.raises(EffectOutsideMandate):
        assert_effects_confined(permit, ["machine_code"])
