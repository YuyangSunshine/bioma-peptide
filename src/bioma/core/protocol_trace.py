"""Deterministic id-chain trace over the Scientific Transition Protocol.

The trace is derived from durable events only, so it is reproducible without
re-running science. It exposes the identifier chain the paper requires:

    request -> offer -> commitment -> proposal -> mandate -> receipt
    -> evidence -> verification -> commit

plus the counter set needed for later ablation (admission rejections,
evidence-gate rejections, stale-proposal rejections, duplicate suppression,
recovery, reconciliation, successful commits).

WP1.5 adds `analyze_cardinality`, which proves the cardinality the protocol
claims: one proposal has one mandate identity, each admission of that proposal
is a distinct attempt, and every attempt has at most one terminal outcome.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from bioma.core.protocol_events import (
    OPTIONAL_STAGES,
    paper_events_for,
    stage_of,
)
from bioma.core.research import fingerprint
from bioma.core.transitions import ExecutionPermit, ExecutionResult

# Paper stages that must all appear before a transition may be called complete.
REQUIRED_STAGES = (
    "request",
    "offer",
    "commitment",
    "proposal",
    "admission",
    "execution",
    "verification",
    "commit",
)

# Canonical order of every stage a trace may exhibit: mandatory then optional.
CANONICAL_STAGES = (*REQUIRED_STAGES, *OPTIONAL_STAGES)

RECONCILIATION_KINDS = (
    "reconciliation.required",
    "reconciliation.completed",
    "reconciliation.failed",
)


@dataclass(frozen=True)
class ProtocolChains:
    """All traceable identifiers of one protocol run, in ledger order."""

    request_ids: tuple[str, ...] = ()
    offer_ids: tuple[str, ...] = ()
    commitment_ids: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    mandate_ids: tuple[str, ...] = ()
    attempt_ids: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    verification_ids: tuple[str, ...] = ()
    commit_versions: tuple[tuple[int, int], ...] = ()
    event_kinds: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def stages(self) -> tuple[str, ...]:
        """Known protocol stages in canonical order, then any extras."""
        seen = {stage_of(kind) for kind in self.event_kinds}
        known = tuple(stage for stage in CANONICAL_STAGES if stage in seen)
        extra = tuple(sorted(seen - set(CANONICAL_STAGES) - {"other"}))
        return known + extra

    @property
    def observed_stages(self) -> tuple[str, ...]:
        """Protocol stages in the order the events actually arrived."""
        seen: list[str] = []
        for kind in self.event_kinds:
            stage = stage_of(kind)
            if stage != "other" and stage not in seen:
                seen.append(stage)
        return tuple(seen)

    @property
    def paper_events(self) -> tuple[str, ...]:
        seen: list[str] = []
        for kind in self.event_kinds:
            for name in paper_events_of(kind):
                if name not in seen:
                    seen.append(name)
        return tuple(seen)

    @property
    def missing_stages(self) -> tuple[str, ...]:
        present = set(self.stages)
        return tuple(stage for stage in REQUIRED_STAGES if stage not in present)

    def complete(self) -> bool:
        """True when every required stage and every identifier link is present."""
        links = (self.request_ids, self.offer_ids, self.commitment_ids,
                 self.proposal_ids, self.mandate_ids, self.receipt_ids,
                 self.verification_ids, self.commit_versions)
        return not self.missing_stages and all(links)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_ids": list(self.request_ids),
            "offer_ids": list(self.offer_ids),
            "commitment_ids": list(self.commitment_ids),
            "proposal_ids": list(self.proposal_ids),
            "mandate_ids": list(self.mandate_ids),
            "attempt_ids": list(self.attempt_ids),
            "receipt_ids": list(self.receipt_ids),
            "evidence_ids": list(self.evidence_ids),
            "verification_ids": list(self.verification_ids),
            "commit_versions": [list(item) for item in self.commit_versions],
            "stages": list(self.stages),
            "missing_stages": list(self.missing_stages),
            "complete": self.complete(),
            "counts": dict(sorted(self.counts.items())),
        }


@dataclass(frozen=True)
class ProtocolCardinality:
    """Proposal -> mandate -> attempt -> outcome cardinality, with problems.

    ``ok`` means the journal is uniquely traceable: every failure, retry and
    reconciliation can be attributed to exactly one attempt of one proposal.
    """

    proposals: tuple[str, ...] = ()
    mandates: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)
    receipts: dict[str, str] = field(default_factory=dict)
    reconciliation: dict[str, tuple[str, ...]] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposals": list(self.proposals),
            "mandates": dict(sorted(self.mandates.items())),
            "attempts": {key: list(value) for key, value in sorted(self.attempts.items())},
            "outcomes": dict(sorted(self.outcomes.items())),
            "receipts": dict(sorted(self.receipts.items())),
            "reconciliation": {key: list(value)
                               for key, value in sorted(self.reconciliation.items())},
            "problems": list(self.problems),
            "ok": self.ok,
        }


def paper_events_of(event_kind: str) -> tuple[str, ...]:
    return paper_events_for(event_kind)


def _records(events: Iterable[Any], sink_events: Iterable[Any]):
    """Unify ledger events and runtime sink events into (kind, payload, key)."""
    for event in events:
        yield (str(getattr(event, "kind", "")), getattr(event, "payload", None) or {},
               getattr(event, "idempotency_key", None))
    for event in sink_events:
        if isinstance(event, dict):
            yield (str(event.get("type", "")), event, None)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _proposal_id_from_key(kind: str, payload: dict, key: str | None) -> str:
    if kind == "TransitionProposed":
        return key[9:] if key and key.startswith("proposal:") else fingerprint(payload)
    return str(payload.get("proposal_id") or "")


def build_chains(events: Iterable[Any], *, sink_events: Iterable[Any] = ()) -> ProtocolChains:
    """Derive the identifier chain from durable events, never from agent prose.

    Layer A identifiers ride the runtime event stream (`task.requested`,
    `capability.offers`, `commitment.created`); Layer B identifiers ride the
    ledger. The engine mirrors its own stream into the ledger, so an
    engine-driven ledger already carries both layers.
    """
    requests: list[str] = []
    offers: list[str] = []
    commitments: list[str] = []
    proposals: list[str] = []
    mandates: list[str] = []
    attempts: list[str] = []
    receipts: list[str] = []
    evidence: list[str] = []
    verifications: list[str] = []
    versions: list[tuple[int, int]] = []
    kinds: list[str] = []
    counts: Counter[str] = Counter()

    for kind, payload, key in _records(events, sink_events):
        kinds.append(kind)
        counts[kind] += 1
        if kind == "task.requested":
            request = payload.get("task_request") or {}
            if request.get("id"):
                requests.append(request["id"])
        elif kind == "capability.offers":
            offers.extend(offer.get("id", "") for offer in payload.get("offers", []))
        elif kind == "commitment.created":
            commitment = payload.get("commitment") or {}
            if commitment.get("id"):
                commitments.append(commitment["id"])
        elif kind == "TransitionProposed":
            proposals.append(_proposal_id_from_key(kind, payload, key))
        elif kind == "ExecutionPermitted":
            permit = ExecutionPermit.model_validate(payload)
            mandates.append(permit.mandate_id)
            attempts.append(permit.attempt_id)
        elif kind == "ExecutionResultRecorded":
            result = ExecutionResult.model_validate(payload)
            receipts.append(result.receipt_id)
            evidence.extend(result.evidence_ids)
        elif kind == "TransitionRejected":
            counts["rejection:" + str(payload.get("code", "unknown"))] += 1
        elif kind == "ExecutionAttemptFailed":
            counts["execution:" + str(payload.get("status", "failed"))] += 1
        elif kind == "TransitionCommitted":
            after = payload.get("after", {})
            requests.extend(item.get("id", "") for item in after.get("task_requests", []))
            offers.extend(item.get("id", "") for item in after.get("offers", []))
            commitments.extend(item.get("id", "") for item in after.get("commitments", []))
            evidence.extend(item.get("id", "") for item in payload.get("delta", {}).get("evidence", []))
            receipt = payload.get("receipt", {})
            if receipt.get("receipt_hash"):
                verifications.append(receipt["receipt_hash"])
            decision = payload.get("commit_decision") or {}
            if decision:
                versions.append((decision["from_version"], decision["to_version"]))

    return ProtocolChains(
        request_ids=_unique(requests),
        offer_ids=_unique(offers),
        commitment_ids=_unique(commitments),
        proposal_ids=_unique(proposals),
        mandate_ids=_unique(mandates),
        attempt_ids=_unique(attempts),
        receipt_ids=_unique(receipts),
        evidence_ids=_unique(evidence),
        verification_ids=_unique(verifications),
        commit_versions=tuple(dict.fromkeys(versions)),
        event_kinds=tuple(kinds),
        counts=dict(counts),
    )


def analyze_cardinality(events: Iterable[Any], *, sink_events: Iterable[Any] = ()) -> ProtocolCardinality:
    """Check proposal : mandate : attempt : outcome cardinality over a trace.

    The contract this enforces:

    * one proposal has exactly one mandate identity (``mandate:<proposal>``);
    * a proposal may be admitted more than once, and each admission is a
      distinct attempt numbered 1..N with no gaps or duplicates;
    * one attempt has at most one terminal outcome (a receipt or a failure);
    * a receipt or a failure can never reference an attempt that was never
      admitted.
    """
    proposals: list[str] = []
    mandates: dict[str, str] = {}
    attempts: dict[str, list[str]] = {}
    attempt_of: dict[str, int] = {}
    outcomes: dict[str, str] = {}
    receipts: dict[str, str] = {}
    reconciliation: dict[str, list[str]] = {}
    problems: list[str] = []

    for kind, payload, key in _records(events, sink_events):
        if kind == "TransitionProposed":
            proposal_id = _proposal_id_from_key(kind, payload, key)
            if proposal_id not in proposals:
                proposals.append(proposal_id)
        elif kind == "ExecutionPermitted":
            permit = ExecutionPermit.model_validate(payload)
            proposal_id = permit.proposal_id
            mandates.setdefault(proposal_id, permit.mandate_id)
            if mandates[proposal_id] != permit.mandate_id:
                problems.append(
                    f"proposal {proposal_id} has conflicting mandate identities"
                )
            if proposal_id not in proposals:
                problems.append(f"mandate for unattested proposal {proposal_id}")
            attempts.setdefault(proposal_id, []).append(permit.attempt_id)
            attempt_of[permit.attempt_id] = permit.attempt
        elif kind == "ExecutionResultRecorded":
            result = ExecutionResult.model_validate(payload)
            attempt_id = result.attempt_id
            if attempt_id not in attempt_of:
                problems.append(f"receipt {result.receipt_id} references unadmitted {attempt_id}")
            if attempt_id in outcomes:
                problems.append(f"attempt {attempt_id} already terminated as {outcomes[attempt_id]}")
            outcomes[attempt_id] = "receipt"
            receipts[attempt_id] = result.receipt_id
        elif kind == "ExecutionAttemptFailed":
            proposal_id = str(payload.get("proposal_id") or "")
            attempt_id = str(payload.get("attempt_id") or
                             f"attempt:{proposal_id}:{payload.get('attempt', 1)}")
            if attempt_id not in attempt_of:
                problems.append(f"failure references unadmitted {attempt_id}")
            if attempt_id in outcomes:
                problems.append(f"attempt {attempt_id} already terminated as {outcomes[attempt_id]}")
            outcomes[attempt_id] = "failed"
        elif kind in RECONCILIATION_KINDS:
            attempt_id = str(payload.get("attempt_id") or "")
            if attempt_id:
                reconciliation.setdefault(attempt_id, []).append(kind)

    for proposal_id, attempt_ids in attempts.items():
        numbers = sorted(attempt_of[attempt_id] for attempt_id in attempt_ids)
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(
                f"proposal {proposal_id} has non-contiguous attempts {numbers}"
            )

    return ProtocolCardinality(
        proposals=tuple(proposals),
        mandates=mandates,
        attempts={key: tuple(value) for key, value in attempts.items()},
        outcomes=outcomes,
        receipts=receipts,
        reconciliation={key: tuple(value) for key, value in reconciliation.items()},
        problems=tuple(problems),
    )
