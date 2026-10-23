"""WP2 state/structure invariants over the committed epistemic channel.

These invariants are *not* pre-commit guards. WP1 verification decides whether
a delta may become state; the properties below are properties of the committed
history itself, so they are established structurally (and checked by an audit
over a committed state) rather than claimed by commit-time verification.

E1b  no re-kind            one evidence id never carries two different kinds
E4b  append-only history   committed evidence is only ever appended to, and a
                           contradiction is preserved instead of collapsed
"""
from __future__ import annotations

from collections.abc import Iterable

from bioma.core.models import Evidence, ScientificState
from bioma.core.protocol_invariants import ProtocolViolation
from bioma.core.scientific_views import (
    SCIENTIFIC_CLAIM_KIND,
    claim_scope,
)


class KindStabilityViolation(ProtocolViolation):
    """E1b: one evidence id was observed carrying two different kinds."""

    code, invariant = "evidence_re_kind", "E1b"


class AppendOnlyViolation(ProtocolViolation):
    """E4b: committed evidence was removed, reordered or rewritten."""

    code, invariant = "append_only_history", "E4b"


def kind_index(evidence: Iterable[Evidence]) -> dict[str, str]:
    """Map every evidence id to the single kind it was committed under."""
    index: dict[str, str] = {}
    for item in evidence:
        existing = index.get(item.id)
        if existing is not None and existing != item.kind:
            raise KindStabilityViolation(
                f"evidence {item.id!r} is committed as both {existing!r} and {item.kind!r}"
            )
        index[item.id] = item.kind
    return index


def assert_single_kind_per_id(evidence: Iterable[Evidence]) -> None:
    """E1b: committed evidence ids never change kind."""
    kind_index(evidence)


def assert_append_only_history(before: ScientificState, after: ScientificState) -> None:
    """E4b: the successor state extends the predecessor; it never edits it."""
    prior = list(before.evidence)
    current = list(after.evidence)
    if len(current) < len(prior):
        raise AppendOnlyViolation(
            f"committed evidence shrank from {len(prior)} to {len(current)} records"
        )
    for index, record in enumerate(prior):
        if current[index] != record:
            raise AppendOnlyViolation(
                f"committed evidence {record.id!r} at index {index} was rewritten"
            )


def assert_contradictions_preserved(
    before: ScientificState, after: ScientificState
) -> None:
    """E4b: a claim that contradicts another is kept, never collapsed away."""
    surviving = {(item.id, item.kind, item.claim): item for item in after.evidence}
    for record in before.evidence:
        if record.kind != SCIENTIFIC_CLAIM_KIND:
            continue
        key = (record.id, record.kind, record.claim)
        if key not in surviving:
            raise AppendOnlyViolation(
                f"scientific claim {record.id!r} was dropped or rewritten"
            )
        if surviving[key].model_dump(mode="json") != record.model_dump(mode="json"):
            raise AppendOnlyViolation(
                f"scientific claim {record.id!r} was mutated after commit"
            )
        # The scope is part of a claim's identity, so a naive "keep the
        # statement" merge that rewrites the scope is also a violation.
        if claim_scope(surviving[key]) != claim_scope(record):
            raise AppendOnlyViolation(
                f"scientific claim {record.id!r} had its scope rewritten"
            )


def preserved_claims(evidence: Iterable[Evidence]) -> list[Evidence]:
    """Return every committed claim, including contradictory claims."""
    return [item for item in evidence if item.kind == SCIENTIFIC_CLAIM_KIND]


def audit_structural_invariants(state: ScientificState) -> None:
    """Run every WP2 state/structure invariant a committed state must satisfy."""
    assert_single_kind_per_id(state.evidence)


__all__ = [
    "AppendOnlyViolation",
    "KindStabilityViolation",
    "assert_append_only_history",
    "assert_contradictions_preserved",
    "assert_single_kind_per_id",
    "audit_structural_invariants",
    "kind_index",
    "preserved_claims",
]
