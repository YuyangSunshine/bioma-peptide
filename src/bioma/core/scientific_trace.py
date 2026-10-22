"""WP2 trace invariants: WP1-exclusive introduction (E2) and provenance (E9).

These are properties of the committed *trace*. They cannot be established by a
verifier looking at one delta: a delta can be internally well formed and still
have entered state through a side channel, or lose its attribution. E2 and E9
are therefore audits over the durable ledger and the committed state, and the
runtime does not pretend that pre-commit verification proves them.

E2  WP1-exclusive introduction
    Reserved-role evidence enters state only through a WP1 transition. A
    committed delta that introduces ``scientific_claim``/``claim_evidence_link``
    evidence must carry the WP1 commit decision that only the proposal ->
    admission -> mandate -> execution -> receipt path produces.

E9  provenance completeness
    Every committed reserved-role record is attributable: it has an author, its
    parents resolve inside the committed evidence, and the commit that
    introduced it recorded it in the state's provenance log.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bioma.core.models import Evidence, ScientificState
from bioma.core.protocol_invariants import ProtocolViolation
from bioma.core.scientific_views import RESERVED_KINDS


class EvidenceIntroductionViolation(ProtocolViolation):
    """E2: reserved-role evidence was introduced outside a WP1 transition."""

    code, invariant = "wp1_exclusive_introduction", "E2"


class ProvenanceIncompleteViolation(ProtocolViolation):
    """E9: a committed reserved-role record cannot be fully attributed."""

    code, invariant = "provenance_incomplete", "E9"


def reserved_evidence_ids(delta_payload: Any) -> list[str]:
    """Reserved-role evidence ids introduced by one serialized delta."""
    if not isinstance(delta_payload, dict):
        return []
    return [
        str(item.get("id"))
        for item in delta_payload.get("evidence") or ()
        if item.get("kind") in RESERVED_KINDS
    ]


def audit_wp1_exclusive_introduction(events: Iterable[Any]) -> None:
    """E2: every reserved-role record was introduced by a WP1 transition."""
    for event in events:
        if getattr(event, "kind", None) != "TransitionCommitted":
            continue
        payload = getattr(event, "payload", {}) or {}
        introduced = reserved_evidence_ids(payload.get("delta"))
        if introduced and not payload.get("commit_decision"):
            raise EvidenceIntroductionViolation(
                "reserved-role evidence committed without a WP1 transition: "
                + ", ".join(sorted(introduced))
            )


def provenance_index(state: ScientificState) -> dict[str, str]:
    """Map each committed evidence id to the author recorded in provenance."""
    index: dict[str, str] = {}
    for record in state.provenance:
        for evidence_id in record.evidence_ids:
            index.setdefault(evidence_id, record.agent_id)
    return index


def assert_provenance_complete(state: ScientificState) -> None:
    """E9: every committed reserved-role record is fully attributable."""
    by_id = {item.id: item for item in state.evidence}
    traced = provenance_index(state)
    for item in state.evidence:
        if item.kind not in RESERVED_KINDS:
            continue
        if not item.source:
            raise ProvenanceIncompleteViolation(
                f"reserved-role evidence {item.id!r} has no author"
            )
        if item.id not in traced:
            raise ProvenanceIncompleteViolation(
                f"reserved-role evidence {item.id!r} is absent from the provenance log"
            )
        missing = sorted(parent for parent in item.parent_ids if parent not in by_id)
        if missing:
            raise ProvenanceIncompleteViolation(
                f"reserved-role evidence {item.id!r} has unresolved parents: {missing}"
            )


def audit_wp2_trace(state: ScientificState, events: Iterable[Any]) -> None:
    """Run both WP2 trace invariants over a committed state and its ledger."""
    audit_wp1_exclusive_introduction(events)
    assert_provenance_complete(state)


__all__ = [
    "EvidenceIntroductionViolation",
    "ProvenanceIncompleteViolation",
    "assert_provenance_complete",
    "audit_wp1_exclusive_introduction",
    "audit_wp2_trace",
    "provenance_index",
    "reserved_evidence_ids",
]
