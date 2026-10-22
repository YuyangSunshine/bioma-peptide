"""Paper-level protocol event vocabulary mapped onto real runtime events.

The paper names its collaboration/transition events. The runtime already emits
equivalents for all of them; this module records the mapping instead of
renaming existing events (existing schemas stay intact). Paper events the
runtime does *not* emit as a distinct event are listed in `MISSING_EVENTS` so
they are reported rather than silently assumed; that tuple is empty today.
"""
from __future__ import annotations

# paper event -> the concrete event kind(s) that carry it today.
PROTOCOL_EVENTS: dict[str, tuple[str, ...]] = {
    # Layer A - decentralized collaboration
    "task.requested": ("task.requested",),
    "task.offer.created": ("capability.offers",),
    "task.commitment.created": ("commitment.created",),
    # Layer B - verifiable state transition
    "proposal.created": ("proposal.created", "TransitionProposed"),
    "admission.approved": ("ExecutionPermitted",),
    "admission.rejected": ("TransitionRejected",),
    "mandate.issued": ("ExecutionPermitted",),
    "execution.started": ("execution.started", "plan.step.started", "agent.started"),
    "execution.completed": ("ExecutionResultRecorded",),
    "execution.failed": ("ExecutionAttemptFailed",),
    "execution.uncertain": ("ExecutionAttemptFailed",),
    "receipt.created": ("ExecutionResultRecorded",),
    "evidence.bound": ("TransitionCommitted", "state.committed"),
    "verification.approved": ("verification.approved",),
    "verification.rejected": ("verification.rejected",),
    "state.committed": ("TransitionCommitted", "state.committed"),
    # Failure-resilient execution
    "recovery.started": ("runtime.recovery",),
    "execution.blocked": ("runtime.blocked",),
    "execution.deadline_exceeded": ("runtime.deadline_exceeded",),
    # Effect reconciliation now has distinct runtime events (WP1.5).
    "reconciliation.required": ("reconciliation.required",),
    "reconciliation.completed": ("reconciliation.completed",),
    "reconciliation.failed": ("reconciliation.failed",),
}

# Paper events with no distinct runtime event today. Reported, not invented.
MISSING_EVENTS: tuple[str, ...] = ()

# Stages that are only present when the run actually needs them, in order.
OPTIONAL_STAGES: tuple[str, ...] = ("reconciliation",)

# The canonical order the deterministic trace must exhibit.
CANONICAL_ORDER: tuple[str, ...] = (
    "task.requested",
    "task.offer.created",
    "task.commitment.created",
    "proposal.created",
    "admission.approved",
    "mandate.issued",
    "execution.started",
    "receipt.created",
    "evidence.bound",
    "verification.approved",
    "state.committed",
)

# Which consolidated stage a concrete event kind belongs to.
_STAGE_OF = {
    "task.requested": "request",
    "capability.offers": "offer",
    "commitment.created": "commitment",
    "proposal.created": "proposal",
    "TransitionProposed": "proposal",
    "ExecutionPermitted": "admission",
    "TransitionRejected": "admission",
    "ExecutionResultRecorded": "execution",
    "ExecutionAttemptFailed": "execution",
    "TransitionCommitted": "commit",
    "state.committed": "commit",
    "verification.started": "verification",
    "verification.approved": "verification",
    "verification.rejected": "verification",
    "runtime.recovery": "recovery",
    "reconciliation.required": "reconciliation",
    "reconciliation.completed": "reconciliation",
    "reconciliation.failed": "reconciliation",
    "EffectReconciled": "reconciliation",
}


def stage_of(event_kind: str) -> str:
    """Consolidated protocol stage for a concrete event kind."""
    return _STAGE_OF.get(event_kind, "other")


def paper_events_for(event_kind: str) -> tuple[str, ...]:
    """Reverse mapping: which paper event(s) a concrete kind implements."""
    return tuple(name for name, kinds in PROTOCOL_EVENTS.items() if event_kind in kinds)
