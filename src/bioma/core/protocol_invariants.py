"""The eleven Scientific Transition Protocol invariants as typed checks.

Each invariant raises a specific `ProtocolViolation` subclass carrying its
`code` and `invariant` id, so a caller (or a test) can assert *which* rule
rejected a transition instead of a generic failure. These helpers encode the
contract; the runtime remains the enforcer (admission, verification, ledger).
"""
from __future__ import annotations

from typing import Any, Iterable

from bioma.core.models import StateDelta
from bioma.core.transitions import ExecutionPermit, TransitionProposal

# Fields that carry scientific content. Failure must never change these.
SCIENTIFIC_FIELDS = (
    "predictions",
    "protocol",
    "machine_code",
    "observations",
    "evidence",
    "artifacts",
    "task_requests",
    "offers",
    "commitments",
)


class ProtocolViolation(ValueError):
    code = "protocol_violation"
    invariant = "?"


class DirectStateWrite(ProtocolViolation):
    code, invariant = "direct_state_write", "I1"


class StaleProposal(ProtocolViolation):
    code, invariant = "stale_proposal", "I2"


class MissingMandate(ProtocolViolation):
    code, invariant = "missing_mandate", "I3"


class MandateProposalMismatch(ProtocolViolation):
    code, invariant = "mandate_proposal_mismatch", "I4"


class EffectOutsideMandate(ProtocolViolation):
    code, invariant = "effect_outside_mandate", "I5"


class EvidenceIncomplete(ProtocolViolation):
    code, invariant = "evidence_incomplete", "I6"


class UnverifiedCommit(ProtocolViolation):
    code, invariant = "unverified_commit", "I7"


class DuplicateTransition(ProtocolViolation):
    code, invariant = "duplicate_transition", "I8"


class ExecutionMutatedState(ProtocolViolation):
    code, invariant = "execution_mutated_state", "I9"


class ReconciliationRequired(ProtocolViolation):
    code, invariant = "reconciliation_required", "I10"


class RuntimeInventedScience(ProtocolViolation):
    code, invariant = "runtime_invented_science", "I11"


class AttemptCardinalityViolation(ProtocolViolation):
    code, invariant = "attempt_cardinality_violation", "I12"


def assert_no_direct_write(delta: StateDelta) -> None:
    """I1: an agent yields a proposed delta, never committed state or a proof."""
    if not isinstance(delta, StateDelta):
        raise DirectStateWrite("agent output must be a StateDelta")
    if delta.status != "proposed":
        raise DirectStateWrite("agent must not self-verify state: " + str(delta.status))
    if delta.transition is not None:
        raise DirectStateWrite("agent must not supply its own admission proof")


def assert_fresh_proposal(proposal: TransitionProposal, state) -> None:
    """I2a: a proposal may not be based on a state version from the future."""
    if proposal.base_version > state.version:
        raise StaleProposal(
            f"proposal base_version {proposal.base_version} > current {state.version}"
        )


def assert_commit_version(delta: StateDelta, state) -> None:
    """I2b: commit-time compare-and-swap on the expected state version."""
    if delta.expected_version != state.version:
        raise StaleProposal(
            f"stale delta: expected {delta.expected_version}, current {state.version}"
        )


def assert_mandated(permit: ExecutionPermit | None, *, now: float | None = None) -> None:
    """I3: no execution without a live, authorized mandate."""
    if permit is None:
        raise MissingMandate("execution without a mandate is forbidden")
    if permit.decision != "authorized":
        raise MissingMandate("mandate is not authorized: " + str(permit.decision))
    if now is not None and not permit.live(now=now):
        raise MissingMandate("mandate expired or revoked")


def assert_mandate_bound(permit: ExecutionPermit, proposal: TransitionProposal) -> None:
    """I4: a mandate authorizes exactly one proposal."""
    if not permit.authorizes(proposal):
        raise MandateProposalMismatch(
            f"mandate {permit.mandate_id} does not authorize {proposal.proposal_ref}"
        )


def assert_effects_confined(permit: ExecutionPermit, actual_effects: Iterable[str]) -> None:
    """I5: actual effects must stay inside the mandate's allowed effects."""
    outside = sorted(set(actual_effects) - set(permit.allowed_effects))
    if outside:
        raise EffectOutsideMandate("effects outside mandate: " + ", ".join(outside))


def assert_evidence_complete(required: Iterable[str], satisfied: Iterable[str]) -> None:
    """I6: every mandatory evidence obligation must be satisfied before commit."""
    missing = sorted(set(required) - set(satisfied))
    if missing:
        raise EvidenceIncomplete("missing evidence obligations: " + ", ".join(missing))


def assert_verified(delta: StateDelta) -> None:
    """I7: an unverified receipt may never become committed state."""
    if delta.status != "verified":
        raise UnverifiedCommit("commit requires verified state, got " + str(delta.status))


def transition_key(message_id: str) -> str:
    """The ledger idempotency key that makes a transition exactly-once."""
    return "transition:" + message_id


def assert_single_commit(events: Iterable[Any], message_id: str) -> int:
    """I8: replaying one transition may not double-commit. Returns commit count."""
    key = transition_key(message_id)
    keys = [getattr(event, "idempotency_key", None) for event in events
            if getattr(event, "kind", None) == "TransitionCommitted"]
    count = sum(key == item for item in keys)
    if count > 1:
        raise DuplicateTransition(f"transition {key} committed {count} times")
    return count


def assert_failure_preserved(before, after) -> None:
    """I9: execution failure must not mutate committed scientific content."""
    changed = sorted(
        field
        for field in SCIENTIFIC_FIELDS
        if getattr(before, field) != getattr(after, field)
    )
    if changed:
        raise ExecutionMutatedState("failure changed scientific state: " + ", ".join(changed))


def assert_effect_certain(execution_status: str | None) -> None:
    """I10: an uncertain external effect requires reconciliation, not retry."""
    if execution_status == "uncertain":
        raise ReconciliationRequired("uncertain external effect requires reconciliation")


def assert_actor_authored(proposal: TransitionProposal, expected: TransitionProposal) -> None:
    """I11: the runtime may bind a proposal but never invent its science."""
    for field in ("action", "target", "method_id", "capability_id", "contract", "result_contract"):
        if getattr(proposal, field) != getattr(expected, field):
            raise RuntimeInventedScience("runtime must not author proposal field: " + field)


# --- Layer A binding rules (collaboration protocol) -------------------------


class UnboundCommitment(ProtocolViolation):
    code, invariant = "unbound_commitment", "A1"


class UnboundProposal(ProtocolViolation):
    code, invariant = "unbound_proposal", "A3"


def assert_commitment_binds_offer(commitment, offers) -> None:
    """A1/A2: a commitment must reference an offer belonging to its request."""
    if commitment.offer_id is None:
        raise UnboundCommitment("commitment has no accepted offer")
    matches = [offer for offer in offers if offer.id == commitment.offer_id]
    if not matches:
        raise UnboundCommitment("commitment references unknown offer: " + commitment.offer_id)
    if matches[0].task_request_id != commitment.task_request_id:
        raise UnboundCommitment("accepted offer belongs to another request")


def assert_proposal_binds_commitment(proposal: TransitionProposal, commitments) -> None:
    """A3: a proposal that declares a commitment must declare a real one.

    Proposals without a commitment remain valid: they model locally initiated
    transitions that were never assigned through the collaboration protocol.
    """
    if proposal.commitment_id is None:
        return
    if proposal.commitment_id not in {item.id for item in commitments}:
        raise UnboundProposal("proposal references unknown commitment: " + proposal.commitment_id)


# --- Layer B cardinality (proposal : mandate : attempt : outcome) -----------


def assert_attempt_cardinality(events, *, sink_events=()) -> None:
    """I12: one proposal -> one mandate -> numbered attempts -> one outcome.

    Retries, failures and reconciliations are only auditable when every attempt
    of a proposal is separately identified and has at most one terminal outcome.
    """
    from bioma.core.protocol_trace import analyze_cardinality
    report = analyze_cardinality(events, sink_events=sink_events)
    if report.problems:
        raise AttemptCardinalityViolation("; ".join(report.problems))
