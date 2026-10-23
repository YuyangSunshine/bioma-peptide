"""WP2 epistemic status is *derived*, never stored (E6).

A claim's epistemic status is a pure function of the committed evidence
channel: the claims (``C_t``), the claim/evidence links (``L_t``) and the
substantive evidence (``E_t``). Nothing in the runtime writes a status field,
so a status can always be recomputed from the committed record alone and can
never drift out of sync with it.

Derivation reads each link's relation from the frozen vocabulary in
:mod:`bioma.core.scientific_relations` (``supports``/``opposes``/``context``)
and never from a status bracket. ``supports`` and ``opposes`` move the status,
``context`` qualifies the claim without moving it, and a link whose relation is
missing or unknown contributes nothing (it is never silently read as support).
A link whose target is a claim with an incompatible scope is likewise
excluded: it stays committed and visible, but it does not move the status.

A second link kind of exclusion is *not* derivation: the link relation is a
separate vocabulary from the derived status literals below. ``supports`` is a
relation the author commits; ``supported`` is the status the runtime computes.
The two never share a literal, so a relation can never be mistaken for a status.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from bioma.core.models import Evidence, ScientificState
from bioma.core.protocol_invariants import ProtocolViolation
from bioma.core.scientific_history import preserved_claims
from bioma.core.scientific_relations import (
    CONTEXT,
    OPPOSES,
    link_relation,
)
from bioma.core.scientific_scope import Scope, covers
from bioma.core.scientific_views import (
    CLAIM_EVIDENCE_LINK_KIND,
    SCIENTIFIC_CLAIM_KIND,
    claim_scope,
    parse_claim,
)

# Status is derived; these keys must never appear inside a committed record.
RESERVED_STATUS_KEYS = frozenset({"epistemic_status", "claim_status", "derived_status"})

INSUFFICIENT = "insufficient"
SUPPORTED = "supported"
CONTRADICTED = "contradicted"
CONFLICTED = "conflicted"

DERIVED_STATUSES = frozenset({INSUFFICIENT, SUPPORTED, CONTRADICTED, CONFLICTED})


class DerivedStatusViolation(ProtocolViolation):
    """E6: a derived epistemic status was persisted instead of computed."""

    code, invariant = "derived_status_only", "E6"


@dataclass(frozen=True)
class LinkTally:
    """Deterministic count of the committed links that bear on one claim.

    The tally is a read-only view of derivation: it is what
    :func:`derive_claim_status` maps onto the four derived statuses, exposed so
    a caller can report *why* a status came out the way it did. Every link
    either contributes to ``supports``/``opposes``/``context`` or is counted as
    excluded; nothing is dropped silently.
    """

    supports: int = 0
    opposes: int = 0
    context: int = 0
    excluded_by_scope: int = 0
    excluded_unknown_relation: int = 0

    @property
    def bearing(self) -> int:
        """Links that are recognised and not excluded by scope."""
        return self.supports + self.opposes + self.context


def _target_is_relevant(claim_scope_value: Scope, target: Evidence) -> bool:
    """Scope compatibility only applies between two claims.

    Substantive evidence carries no scope, so there is nothing to compare and
    the link is relevant. Two claims are compared with the frozen ``covers``
    rules: an incompatible scope keeps both records but is excluded here.
    """
    if target.kind != SCIENTIFIC_CLAIM_KIND:
        return True
    return covers(claim_scope_value, claim_scope(target))


def _committed_claim(evidence: Iterable[Evidence], claim_id: str) -> tuple[list[Evidence], Evidence]:
    records = list(evidence)
    by_id = {item.id: item for item in records}
    claim = by_id.get(claim_id)
    if claim is None or claim.kind != SCIENTIFIC_CLAIM_KIND:
        raise ValueError(f"not a committed scientific claim: {claim_id!r}")
    return records, claim


def link_tally(evidence: Iterable[Evidence], claim_id: str) -> LinkTally:
    """Count the committed links that bear on one claim.

    A link bears on the claim only when its first parent is the claim itself.
    Each remaining parent contributes at most once: a relevant target becomes
    ``supports``/``opposes``/``context`` by the link's relation, and an
    incompatible claim target is counted in ``excluded_by_scope``.
    """
    records, claim = _committed_claim(evidence, claim_id)
    by_id = {item.id: item for item in records}
    view = parse_claim(claim)
    supports = opposes = context = excluded_scope = excluded_unknown = 0
    for link in records:
        if link.kind != CLAIM_EVIDENCE_LINK_KIND:
            continue
        if not link.parent_ids or link.parent_ids[0] != claim_id:
            continue
        relation = link_relation(link)
        if relation is None:
            excluded_unknown += 1
            continue
        for target_id in link.parent_ids[1:]:
            target = by_id.get(target_id)
            if target is None or target.id == claim_id:
                continue
            if not _target_is_relevant(view.scope, target):
                excluded_scope += 1
                continue
            if relation == OPPOSES:
                opposes += 1
            elif relation == CONTEXT:
                context += 1
            else:
                supports += 1
    return LinkTally(
        supports=supports,
        opposes=opposes,
        context=context,
        excluded_by_scope=excluded_scope,
        excluded_unknown_relation=excluded_unknown,
    )


def derive_claim_status(evidence: Iterable[Evidence], claim_id: str) -> str:
    """Derive one claim's epistemic status from the committed evidence."""
    records, _ = _committed_claim(evidence, claim_id)
    tally = link_tally(records, claim_id)
    if tally.supports and tally.opposes:
        return CONFLICTED
    if tally.opposes:
        return CONTRADICTED
    if tally.supports:
        return SUPPORTED
    return INSUFFICIENT


def claim_statuses(evidence: Iterable[Evidence]) -> dict[str, str]:
    """Derive the status of every claim that takes part in derivation.

    Production derives over every committed claim.
    """
    records = list(evidence)
    return {
        item.id: derive_claim_status(records, item.id)
        for item in preserved_claims(records)
    }


def assert_status_is_derived(state: ScientificState) -> None:
    """E6: no committed record stores an epistemic status.

    Only the reserved status keys are rejected, so ordinary tool payloads that
    legitimately carry their own ``status`` field are untouched.
    """
    for item in state.evidence:
        value = item.value
        if not isinstance(value, Mapping):
            continue
        stored = sorted(RESERVED_STATUS_KEYS & set(value))
        if stored:
            raise DerivedStatusViolation(
                f"evidence {item.id!r} stores derived status keys: {stored}"
            )


__all__ = [
    "CONFLICTED",
    "CONTRADICTED",
    "DERIVED_STATUSES",
    "DerivedStatusViolation",
    "INSUFFICIENT",
    "LinkTally",
    "RESERVED_STATUS_KEYS",
    "SUPPORTED",
    "assert_status_is_derived",
    "claim_statuses",
    "derive_claim_status",
    "link_tally",
]
