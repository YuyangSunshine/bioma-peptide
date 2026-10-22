"""WP2 typed semantic views and commit-time guards (checkpoint 1).

WP2 adds no container, no ``StateDelta`` field and no ``Evidence`` field. It
reads *reserved roles* out of the existing committed evidence channel::

    C_t   scientific claims        Evidence.kind == "scientific_claim"
    L_t   claim/evidence links     Evidence.kind == "claim_evidence_link"
    E_t   substantive evidence     every other kind (open, extensible)

so the partition ``C_t + E_t + L_t == ScientificState.evidence`` holds by
construction and all other ``Evidence.kind`` values stay unconstrained.

This module implements the five commit-time guards::

    E1a  reserved-role schema          the reserved role is well formed
    E1c  relation vocabulary           a link names one canonical relation
    E3   link integrity                a link resolves, and links a claim
    E4a  within-delta duplicates       one delta never asserts a role twice
    E5a  scope shape validation        a declared scope is interpretable

The guards are *pure*: they return human-readable reasons and never raise, so
``VerificationEngine.verify`` can append them to its existing reason list. A
delta with no reserved kinds produces an empty list, which is why verification
reasons for every non-WP2 delta are byte-for-byte unchanged.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from bioma.core.models import Evidence
from bioma.core.protocol_invariants import ProtocolViolation
from bioma.core.protocol_policy import wp2_ablation_active
from bioma.core.scientific_relations import (
    RelationVocabularyViolation,
    parse_relation,
    raw_relation,
)
from bioma.core.scientific_scope import Scope, parse_scope

SCIENTIFIC_CLAIM_KIND = "scientific_claim"
CLAIM_EVIDENCE_LINK_KIND = "claim_evidence_link"
RESERVED_KINDS: frozenset[str] = frozenset(
    {SCIENTIFIC_CLAIM_KIND, CLAIM_EVIDENCE_LINK_KIND}
)


class ReservedRoleSchemaViolation(ProtocolViolation):
    """E1a: a reserved kind is used without its required role schema."""

    code, invariant = "reserved_role_schema", "E1a"


class ClaimLinkIntegrityViolation(ProtocolViolation):
    """E3: a claim/evidence link does not resolve to real evidence."""

    code, invariant = "claim_link_integrity", "E3"


class WithinDeltaDuplicateViolation(ProtocolViolation):
    """E4a: one delta asserts the same reserved role twice."""

    code, invariant = "within_delta_duplicate", "E4a"


@dataclass(frozen=True)
class ScientificClaimView:
    """A committed claim read out of its existing Evidence record."""

    evidence_id: str
    statement: str
    scope: Scope


@dataclass(frozen=True)
class ClaimEvidenceLinkView:
    """A committed claim/evidence link read out of its Evidence record."""

    evidence_id: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class EvidencePartition:
    """The WP2 partition of one committed evidence collection."""

    claims: tuple[Evidence, ...] = ()
    substantive: tuple[Evidence, ...] = ()
    links: tuple[Evidence, ...] = ()


def is_reserved_kind(kind: str) -> bool:
    return kind in RESERVED_KINDS


def _payload(evidence: Evidence, role: str) -> Mapping[str, Any] | None:
    """The reserved role may carry a mapping; anything else is E1a."""
    value = evidence.value
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReservedRoleSchemaViolation(
            f"{role} value must be a mapping, got {type(value).__name__}"
        )
    return value


def claim_scope(evidence: Evidence) -> Scope:
    """The declared scope of a scientific claim (E1a shape, E5a validation)."""
    if evidence.kind != SCIENTIFIC_CLAIM_KIND:
        raise ReservedRoleSchemaViolation(
            f"not a scientific claim: {evidence.kind!r}"
        )
    return parse_scope(_payload(evidence, SCIENTIFIC_CLAIM_KIND))


def parse_claim(evidence: Evidence) -> ScientificClaimView:
    """Typed view of a ``scientific_claim`` record."""
    return ScientificClaimView(
        evidence_id=evidence.id,
        statement=evidence.claim,
        scope=claim_scope(evidence),
    )


def parse_link(evidence: Evidence) -> ClaimEvidenceLinkView:
    """Typed view of a ``claim_evidence_link`` record."""
    if evidence.kind != CLAIM_EVIDENCE_LINK_KIND:
        raise ReservedRoleSchemaViolation(
            f"not a claim evidence link: {evidence.kind!r}"
        )
    _payload(evidence, CLAIM_EVIDENCE_LINK_KIND)
    if not evidence.parent_ids:
        raise ReservedRoleSchemaViolation(
            "claim_evidence_link must reference at least one evidence record"
        )
    return ClaimEvidenceLinkView(
        evidence_id=evidence.id, parents=tuple(evidence.parent_ids)
    )


def partition_evidence(evidence: Iterable[Evidence]) -> EvidencePartition:
    """Split committed evidence into ``C_t`` (claims), ``E_t`` and ``L_t`` (links)."""
    claims: list[Evidence] = []
    substantive: list[Evidence] = []
    links: list[Evidence] = []
    for item in evidence:
        if item.kind == SCIENTIFIC_CLAIM_KIND:
            claims.append(item)
        elif item.kind == CLAIM_EVIDENCE_LINK_KIND:
            links.append(item)
        else:
            substantive.append(item)
    return EvidencePartition(
        claims=tuple(claims), substantive=tuple(substantive), links=tuple(links)
    )


def _semantic_key(evidence: Evidence) -> tuple[Any, ...]:
    """The exact-string identity used only for within-delta duplicate detection."""
    if evidence.kind == SCIENTIFIC_CLAIM_KIND:
        view = parse_claim(evidence)
        return (SCIENTIFIC_CLAIM_KIND, view.statement, repr(view.scope))
    # Two links with the same parents but different relations assert different
    # facts (e.g. one supports and one opposes the same target), so the
    # relation is part of a link's within-delta identity.
    return (
        CLAIM_EVIDENCE_LINK_KIND,
        tuple(sorted(evidence.parent_ids)),
        repr(raw_relation(evidence)),
    )


def _reason(invariant: str, message: str) -> str:
    return f"[WP2 {invariant}] {message}"


def _guard_reason(exc: ProtocolViolation) -> str:
    return _reason(exc.invariant, str(exc))


def reserved_role_reasons(delta: Any, committed: Iterable[Evidence] = ()) -> list[str]:
    """Commit-time reasons for the reserved WP2 roles in one delta.

    Returns ``[]`` unless the delta introduces a reserved kind, so verification
    reasons for non-WP2 deltas are unchanged by the WP2 hook.
    """
    reserved = [item for item in delta.evidence if item.kind in RESERVED_KINDS]
    if not reserved:
        return []
    if wp2_ablation_active("epistemic_role_conflation"):
        # Experimental gating ablation (never production): the reserved roles
        # are treated as ordinary evidence, so the reserved-role schema is not
        # enforced and this guard stays silent.
        return []
    by_id: dict[str, Evidence] = {item.id: item for item in committed}
    for item in delta.evidence:
        by_id.setdefault(item.id, item)
    reasons: list[str] = []

    parsed: list[Evidence] = []
    for item in reserved:
        try:
            if item.kind == SCIENTIFIC_CLAIM_KIND:
                parse_claim(item)
            else:
                parse_link(item)
        except ProtocolViolation as exc:
            reasons.append(_guard_reason(exc))
            continue
        parsed.append(item)

    for item in parsed:
        if item.kind != CLAIM_EVIDENCE_LINK_KIND:
            continue
        try:
            parse_relation(item)
        except RelationVocabularyViolation as exc:
            reasons.append(_guard_reason(exc))

    for item in parsed:
        if item.kind != CLAIM_EVIDENCE_LINK_KIND:
            continue
        unknown = sorted(parent for parent in item.parent_ids if parent not in by_id)
        if unknown:
            reasons.append(
                _reason(
                    "E3",
                    f"Claim evidence link {item.id!r} has unknown parents: {unknown}.",
                )
            )
        elif not any(
            by_id[parent].kind == SCIENTIFIC_CLAIM_KIND for parent in item.parent_ids
        ):
            reasons.append(
                _reason(
                    "E3",
                    f"Claim evidence link {item.id!r} must reference at least one scientific_claim.",
                )
            )

    # Computational predictions can resolve only their checked, typed scope.
    # Enforce the same boundary on WP2 links as on contract assessments.
    from bioma.core.prediction_checks import validate_prediction_claim_scope
    for item in parsed:
        if item.kind != CLAIM_EVIDENCE_LINK_KIND or raw_relation(item) not in {'supports','opposes'}:
            continue
        parents=[by_id[p] for p in item.parent_ids if p in by_id]
        claims=[p.value or {} for p in parents if p.kind == SCIENTIFIC_CLAIM_KIND]
        for parent in parents:
            try:
                validate_prediction_claim_scope(claims, parent)
            except ValueError as exc:
                reasons.append(_reason('E3', str(exc)))

    seen: dict[tuple[Any, ...], str] = {}
    for item in parsed:
        key = _semantic_key(item)
        if key in seen:
            reasons.append(
                _reason(
                    "E4a",
                    "Duplicate reserved-role evidence within one delta: "
                    f"{item.id!r} duplicates {seen[key]!r}.",
                )
            )
            continue
        seen[key] = item.id
    return reasons


__all__ = [
    "CLAIM_EVIDENCE_LINK_KIND",
    "ClaimEvidenceLinkView",
    "ClaimLinkIntegrityViolation",
    "EvidencePartition",
    "RESERVED_KINDS",
    "ReservedRoleSchemaViolation",
    "SCIENTIFIC_CLAIM_KIND",
    "ScientificClaimView",
    "WithinDeltaDuplicateViolation",
    "claim_scope",
    "is_reserved_kind",
    "parse_claim",
    "parse_link",
    "partition_evidence",
    "reserved_role_reasons",
]
