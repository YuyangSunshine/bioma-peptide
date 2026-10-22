"""Canonical Scientific Transition Protocol (WP1).

This module is the paper-facing vocabulary for a protocol the runtime already
implements. It deliberately *aliases* the existing objects rather than
introducing a second, parallel data model, so there is exactly one source of
truth for every concept.

Layer A - decentralized collaboration (who accepts the work?)::

    ScientificTaskRequest  -> models.TaskRequest
    ScientificTaskOffer    -> models.TaskOffer
    ScientificCommitment   -> models.Commitment

Layer B - verifiable scientific state transition (may this become state?)::

    ScientificTransitionProposal -> transitions.TransitionProposal
    ScientificExecutionMandate   -> transitions.ExecutionPermit
    ScientificTransitionReceipt  -> transitions.ExecutionResult

The chain ``S_t -> tau -> tau+ -> O -> E -> V -> S_{t+1}`` is enforced by
``runtime.admission`` (proposal, admission, mandate, execution, receipt),
``core.verification`` (independent verification) and ``runtime.ledger``
(compare-and-swap commit). Invariants and their checks live in
``core.protocol_invariants``; the event vocabulary in ``core.protocol_events``.

Runtime responsibility boundary: the runtime validates, authorizes, leases,
dispatches, executes, reconciles, verifies, commits and recovers. It never
generates a scientific hypothesis, mutation, method or target. Those come from
an agent/backend/skill/tool through ``TaskOffer`` and ``TransitionProposal``.
"""
from __future__ import annotations

from bioma.core.models import Commitment, TaskOffer, TaskRequest
from bioma.core.transitions import (
    CommitDecision,
    ExecutionPermit,
    ExecutionResult,
    TransitionProposal,
)

PROTOCOL_SCHEMA_VERSION = "scientific-transition-protocol-v1"

# Paper name -> the single code object that already implements it.
ALIASES = {
    "ScientificTaskRequest": TaskRequest,
    "ScientificTaskOffer": TaskOffer,
    "ScientificCommitment": Commitment,
    "ScientificTransitionProposal": TransitionProposal,
    "ScientificExecutionMandate": ExecutionPermit,
    "ScientificTransitionReceipt": ExecutionResult,
    "CommitDecision": CommitDecision,
}

# Convenience names for paper-facing code and tests.
ScientificTaskRequest = TaskRequest
ScientificTaskOffer = TaskOffer
ScientificCommitment = Commitment
ScientificTransitionProposal = TransitionProposal
ScientificExecutionMandate = ExecutionPermit
ScientificTransitionReceipt = ExecutionResult

LAYER_A = (
    "ScientificTaskRequest",
    "ScientificTaskOffer",
    "ScientificCommitment",
)
LAYER_B = (
    "ScientificTransitionProposal",
    "ScientificExecutionMandate",
    "ScientificTransitionReceipt",
)

# The seven formal stages, in order, with the module that owns each.
TRANSITION_STAGES = (
    ("proposal", "bioma.core.transitions.TransitionProposal"),
    ("admission", "bioma.runtime.admission.TransitionAdmission"),
    ("mandate", "bioma.core.transitions.ExecutionPermit"),
    ("execution", "bioma.runtime.participant.RuntimeParticipant.handle"),
    ("receipt", "bioma.core.transitions.ExecutionResult"),
    ("evidence", "bioma.evidence.packages"),
    ("verification", "bioma.core.verification.VerificationEngine"),
    ("commit", "bioma.runtime.ledger.RunLedger.commit_transition"),
)

__all__ = [
    "ALIASES",
    "CommitDecision",
    "LAYER_A",
    "LAYER_B",
    "PROTOCOL_SCHEMA_VERSION",
    "ScientificCommitment",
    "ScientificExecutionMandate",
    "ScientificTaskOffer",
    "ScientificTaskRequest",
    "ScientificTransitionProposal",
    "ScientificTransitionReceipt",
    "TRANSITION_STAGES",
]
