"""Canonical deterministic demonstration of the WP2 scientific-state pipeline.

Runs one frozen WP1 sequence over the real runtime and prints the WP2 readings
after every commit:

    C committed                      -> insufficient
    E1 + L1 supports C               -> supported
    E2 + L2 opposes C                -> conflicted
    E3 + L3 valid but scope-incompat -> E3/L3 preserved, excluded_by_scope += 1,
                                        status remains conflicted

Every mutation traverses WP1: a proposal, an admission mandate, one numbered
attempt, an execution result, an independent verification receipt, and a
ledger commit. No LLM, no device, no network; the ledger is written to a
temporary directory so the repository is left untouched.

``E3`` is itself a scientific claim: the frozen scope rule compares two
*claim* scopes, so the link ``L3`` is excluded precisely because its target
claim's scope is disjoint from ``C``'s. Both records stay committed and
visible; only the derived status ignores the link.

Run with:  python examples/claim_evidence_demo.py
"""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from bioma.runtime.participant import RuntimeParticipant
from bioma.core.models import (
    AgentMessage,
    CapabilitySpec,
    Evidence,
    ScientificState,
    StateDelta,
)
from bioma.core.scientific_relations import OPPOSES, RELATION_KEY, SUPPORTS
from bioma.core.scientific_status import (
    CONFLICTED,
    INSUFFICIENT,
    SUPPORTED,
    derive_claim_status,
    link_tally,
)
from bioma.core.scientific_views import (
    CLAIM_EVIDENCE_LINK_KIND,
    SCIENTIFIC_CLAIM_KIND,
    parse_claim,
    partition_evidence,
)
from bioma.core.transitions import ExecutionPermit, ExecutionResult, TransitionProposal
from bioma.core.verification import VerificationEngine
from bioma.runtime.admission import TransitionAdmission
from bioma.runtime.ledger import RunLedger

CAPABILITY_ID = "scientific.protocol_compute"
METHOD_ID = "compute_metric"
MESSAGE_TYPE = "task.submitted"
TASK_ID = "wp2-claim-evidence-demo"
ACTOR = "agent"

CLAIM_ID = "C"
CLAIM_SCOPE = ["human"]
RIVAL_ID = "E3"
RIVAL_SCOPE = ["mouse"]

SUBSTANTIVE_IDS = ("E1", "E2")
LINK_IDS = ("L1", "L2", "L3")

EvidenceProvider = Callable[[], list[Evidence]]


# --- deterministic actors and capability -----------------------------------


def _capability(owner: str) -> CapabilitySpec:
    """One registered capability whose only effect is a bounded metric write."""
    return CapabilitySpec(
        id=CAPABILITY_ID,
        agent_id=owner,
        description="Deterministic WP2 claim/evidence capability",
        accepts_message_types=[MESSAGE_TYPE],
        max_risk_level="low",
        methods=[
            dict(
                id=METHOD_ID,
                description="Record one claim/evidence transition",
                message_type=MESSAGE_TYPE,
                result=dict(
                    responsibility=CAPABILITY_ID,
                    evidence_kinds=["prediction"],
                    verification_obligations=["transition_integrity"],
                ),
                action_contract=dict(
                    writable_fields=["metrics"],
                    risk_level="low",
                    expected_effects=[
                        dict(id="produced", source="result", path="evidence",
                             operator="nonempty")
                    ],
                ),
            )
        ],
    )


class DemoActor(RuntimeParticipant):
    """A deterministic actor that proposes the next prepared evidence set."""

    def __init__(self, name: str = ACTOR) -> None:
        super().__init__([name])
        self.owner = name
        self.advertise(_capability(name))
        self._pending: EvidenceProvider = lambda: []

    def provide(self, provider: EvidenceProvider) -> None:
        self._pending = provider

    def handle(self, state: ScientificState, message: AgentMessage) -> StateDelta:
        return StateDelta(
            agent_id=self.owner,
            source_message_id=message.id,
            expected_version=state.version,
            updates={"metrics": {"commits": state.version + 1}},
            evidence=list(self._pending()),
        )


# --- deterministic evidence ------------------------------------------------


def _claim(claim_id: str, scope: list[str]) -> Evidence:
    return Evidence(id=claim_id, kind=SCIENTIFIC_CLAIM_KIND, source=ACTOR,
                    claim=f"claim {claim_id}", value={"scope": list(scope)})


def _observation(evidence_id: str, measurement: float) -> Evidence:
    return Evidence(id=evidence_id, kind="prediction", source=ACTOR,
                    claim=f"measurement for {evidence_id}",
                    value={"measurement": measurement})


def _link(link_id: str, claim_id: str, target_id: str, relation: str) -> Evidence:
    return Evidence(id=link_id, kind=CLAIM_EVIDENCE_LINK_KIND, source=ACTOR,
                    claim=f"{relation}: {claim_id} <= {target_id}",
                    parent_ids=[claim_id, target_id],
                    value={RELATION_KEY: relation})


STEPS: tuple[tuple[str, EvidenceProvider], ...] = (
    ("C committed", lambda: [_claim(CLAIM_ID, CLAIM_SCOPE)]),
    ("E1 + L1 supports C", lambda: [
        _observation("E1", 1.0), _link("L1", CLAIM_ID, "E1", SUPPORTS)]),
    ("E2 + L2 opposes C", lambda: [
        _observation("E2", 2.0), _link("L2", CLAIM_ID, "E2", OPPOSES)]),
    ("E3 + L3 valid but scope-incompatible", lambda: [
        _claim(RIVAL_ID, RIVAL_SCOPE),
        _link("L3", CLAIM_ID, RIVAL_ID, SUPPORTS)]),
)

EXPECTED_STATUSES = (INSUFFICIENT, SUPPORTED, CONFLICTED, CONFLICTED)


# --- WP1 driver ------------------------------------------------------------


def _assignment() -> AgentMessage:
    return AgentMessage(sender="queryagent", recipient=ACTOR,
                        message_type=MESSAGE_TYPE,
                        payload={"selected_capability": CAPABILITY_ID,
                                 "method_id": METHOD_ID})


def _run_step(actor: DemoActor, ledger: RunLedger, state: ScientificState,
              provide: EvidenceProvider):
    """Drive one transition through the real proposal->mandate->receipt path."""
    message = _assignment()
    actor.provide(provide)
    delta = TransitionAdmission(ledger).execute(actor, state, message)
    engine = VerificationEngine()
    result, receipt = engine.verify_with_receipt(
        state, delta, expected_agent=ACTOR, source_message=message)
    if not result.ok:
        raise SystemExit("verification rejected: " + "; ".join(result.reasons))
    approved = engine.approve(state, delta, expected_agent=ACTOR,
                              source_message=message)
    after = ledger.commit_transition(state, approved, message, receipt)
    return after, delta, receipt


# --- reporting -------------------------------------------------------------


def _ids(records) -> str:
    return ", ".join(record.id for record in records) or "-"


def _scopes(records) -> str:
    rendered = ", ".join(f"{record.id}={parse_claim(record).scope}"
                         for record in records)
    return rendered or "-"


def _report_step(label: str, state: ScientificState, delta: StateDelta,
                 receipt) -> None:
    partition = partition_evidence(state.evidence)
    tally = link_tally(state.evidence, CLAIM_ID)
    proof = delta.transition or {}
    proposal = TransitionProposal.model_validate(proof["proposal"])
    permit = ExecutionPermit.model_validate(proof["permit"])
    result = ExecutionResult.model_validate(proof["result"])

    print(f"\n[commit v{state.version}] {label}")
    print(f"   claim id                    : {CLAIM_ID}")
    print(f"   claim scopes                : {_scopes(partition.claims)}")
    print(f"   evidence ids (E_t)          : {_ids(partition.substantive)}")
    print(f"   link ids (L_t)              : {_ids(partition.links)}")
    print(f"   derived status              : "
          f"{derive_claim_status(state.evidence, CLAIM_ID)}")
    print(f"   supports / opposes / context: "
          f"{tally.supports} / {tally.opposes} / {tally.context}")
    print(f"   excluded_by_scope           : {tally.excluded_by_scope}")
    print(f"   proposal                    : {proposal.identity}")
    print(f"   mandate                     : {permit.mandate_id}")
    print(f"   attempt                     : {permit.attempt}  ({permit.attempt_id})")
    print(f"   execution receipt           : {result.receipt_id}")
    print(f"   verify receipt              : {receipt.receipt_hash}")
    print(f"   commit version              : {state.version}")


def main() -> None:
    print("BioMA WP2 claim/evidence canonical demonstration")
    print("=" * 64)

    with tempfile.TemporaryDirectory(prefix="bioma-wp2-") as tmp:
        ledger = RunLedger(Path(tmp) / "run.db")
        actor = DemoActor()
        state = ScientificState(task_id=TASK_ID, objective="assess a claim")

        for (label, provide), expected in zip(STEPS, EXPECTED_STATUSES):
            state, delta, receipt = _run_step(actor, ledger, state, provide)
            _report_step(label, state, delta, receipt)
            actual = derive_claim_status(state.evidence, CLAIM_ID)
            assert actual == expected, f"{label}: {actual!r} != {expected!r}"

        print("\n" + "-" * 64)
        print("Final readings")
        partition = partition_evidence(state.evidence)
        final = link_tally(state.evidence, CLAIM_ID)
        print(f"   state version               : {state.version}")
        print(f"   claim id                    : {CLAIM_ID}")
        print(f"   derived status              : "
              f"{derive_claim_status(state.evidence, CLAIM_ID)}")
        print(f"   supports / opposes / context: "
              f"{final.supports} / {final.opposes} / {final.context}")
        print(f"   excluded_by_scope           : {final.excluded_by_scope}")
        print(f"   evidence ids (E_t)          : {_ids(partition.substantive)}")
        print(f"   link ids (L_t)              : {_ids(partition.links)}")
        committed = {item.id for item in state.evidence}
        preserved = {RIVAL_ID, "L3"} <= committed
        print(f"   E3 and L3 preserved         : {preserved}")

        assert set(SUBSTANTIVE_IDS) | set(LINK_IDS) <= committed
        assert final.excluded_by_scope == 1
        assert derive_claim_status(state.evidence, CLAIM_ID) == CONFLICTED
        assert ledger.verify_chain(TASK_ID) is True
        assert ledger.replay_state(TASK_ID).version == state.version

        print("\nVERDICT: PASS - the WP2 chain is deterministic, append-only,")
        print("         replay-safe from the ledger, and reproducible.")


if __name__ == "__main__":
    main()
