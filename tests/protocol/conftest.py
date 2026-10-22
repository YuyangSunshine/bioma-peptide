"""Deterministic fixtures for the Scientific Transition Protocol suite.

Everything here runs on the toy/deterministic path: no LLM, no device, no
network. Helpers are exposed through fixtures (rather than module imports) so
the suite needs no package `__init__` files.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bioma.runtime.participant import RuntimeParticipant
from bioma.core.models import (
    AgentMessage,
    CapabilitySpec,
    Evidence,
    ScientificState,
    StateDelta,
)
from bioma.core.verification import VerificationEngine
from bioma.runtime.admission import TransitionAdmission
from bioma.runtime.ledger import RunLedger

CAPABILITY_ID = "scientific.protocol_compute"
METHOD_ID = "compute_metric"
MESSAGE_TYPE = "task.submitted"


def compute_capability(owner="predagent", *, risk="low", contract_risk="low",
                       writable=("metrics",), resources=(), preconditions=(),
                       invariants=()):
    """A registered capability with an explicit, closed-world action contract.

    `risk` is the capability's authority ceiling; `contract_risk` is the risk
    the action contract itself declares, so the two can be made to disagree.
    """
    return CapabilitySpec(
        id=CAPABILITY_ID,
        agent_id=owner,
        description="Deterministic protocol capability for contract tests",
        accepts_message_types=[MESSAGE_TYPE],
        max_risk_level=risk,
        resources=list(resources),
        methods=[
            dict(
                id=METHOD_ID,
                description="Compute one bounded metric",
                message_type=MESSAGE_TYPE,
                result=dict(
                    responsibility=CAPABILITY_ID,
                    evidence_kinds=["prediction"],
                    verification_obligations=["transition_integrity"],
                ),
                action_contract=dict(
                    writable_fields=list(writable),
                    resources=list(resources),
                    risk_level=contract_risk,
                    preconditions=list(preconditions),
                    invariants=list(invariants),
                    expected_effects=[
                        dict(id="produced", source="result", path="evidence", operator="nonempty")
                    ],
                ),
            )
        ],
    )


class ToyActor(RuntimeParticipant):
    """A deterministic scientific actor that proposes bounded metric writes."""

    def __init__(self, name="predagent", *, outcome="completed", handler=None,
                 capability=None):
        super().__init__([name])
        self.owner = name
        self.outcome = outcome
        self.handler = handler
        self.advertise(capability or compute_capability(name))

    def handle(self, state, message):
        if self.handler:
            return self.handler(state, message)
        return StateDelta(
            agent_id=self.owner,
            source_message_id=message.id,
            expected_version=state.version,
            updates={"metrics": {"value": 1}},
            evidence=[Evidence(kind="prediction", source=self.owner,
                               claim="computed one bounded metric")],
            outcome=self.outcome,
        )


def assignment(recipient="predagent", *, payload=None):
    return AgentMessage(
        sender="queryagent",
        recipient=recipient,
        message_type=MESSAGE_TYPE,
        payload={"selected_capability": CAPABILITY_ID, "method_id": METHOD_ID,
                 **(payload or {})},
    )


def fresh_state(**overrides):
    values = {"task_id": "protocol", "objective": "compute a bounded metric"}
    values.update(overrides)
    return ScientificState(**values)


@pytest.fixture
def protocol():
    """Constants and factories shared by the protocol tests."""
    return SimpleNamespace(
        CAPABILITY_ID=CAPABILITY_ID,
        METHOD_ID=METHOD_ID,
        MESSAGE_TYPE=MESSAGE_TYPE,
        capability=compute_capability,
        actor=ToyActor,
        assignment=assignment,
        state=fresh_state,
    )


@pytest.fixture
def chain(tmp_path):
    """Run one complete deterministic transition and return every artefact."""

    def run(actor=None, *, state=None, message=None, ledger=None, verify=True):
        actor = actor if actor is not None else ToyActor()
        ledger = ledger if ledger is not None else RunLedger(tmp_path / "run.db")
        state = state if state is not None else fresh_state()
        message = message or assignment(actor.owner)
        delta = TransitionAdmission(ledger).execute(actor, state, message)
        receipt = None
        after = None
        if verify:
            result, receipt = VerificationEngine().verify_with_receipt(
                state, delta, expected_agent=actor.owner, source_message=message)
            assert result.ok, result.reasons
            after = ledger.commit_transition(
                state, delta.model_copy(update={"status": "verified"}), message, receipt)
        return SimpleNamespace(actor=actor, ledger=ledger, state=state, message=message,
                               delta=delta, receipt=receipt, after=after)

    return run


@pytest.fixture
def routed_offers():
    """Two competing providers, routed through the real CapabilityRouter."""

    def build(*, selection_mode="all", max_providers=1, budget=None):
        from bioma.capabilities.registry import CapabilityRegistry
        from bioma.capabilities.router import CapabilityRouter
        from bioma.core.models import TaskRequest

        actors = [ToyActor("predagent"), ToyActor("comagent")]
        registry = CapabilityRegistry([actor.capabilities[0] for actor in actors])
        request = TaskRequest(
            requester="queryagent",
            required_capability=CAPABILITY_ID,
            objective="compute a bounded metric",
            message_type=MESSAGE_TYPE,
            selection_mode=selection_mode,
            max_providers=max_providers,
            budget=budget,
        )
        router = CapabilityRouter(
            registry, {actor.owner: actor.assess for actor in actors})
        decision = router.route(
            request, active_agents={"predagent", "comagent"}, ttl=5)
        return SimpleNamespace(actors=actors, registry=registry, request=request,
                               decision=decision)

    return build
