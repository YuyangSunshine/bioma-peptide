"""WP2 canonical link-relation vocabulary (commit-time guard E1c).

A ``claim_evidence_link`` asserts exactly one relation between a claim and the
evidence it points at. WP2 recognises exactly three relation literals and
nothing else::

    "supports"   the linked record supports the claim
    "opposes"    the linked record opposes the claim
    "context"    the linked record is recorded as context only: it qualifies
                 the claim without moving its derived status

There is no default and no synonym reasoning. A link that omits its relation,
or that carries any other string, is rejected at commit time (E1c) and never
enters the committed channel. ``link_relation`` reads the same vocabulary
defensively at derivation time and returns ``None`` for a non-canonical
relation, so a derived status can never come from an implicit "supports".

Link relations are deliberately separate from derived claim-status literals
(:mod:`bioma.core.scientific_status`). ``supports``/``opposes``/``context`` are
what an author asserts and commits; ``insufficient``/``supported``/
``contradicted``/``conflicted`` are what the runtime computes from those
committed relations. The two vocabularies share no literal, so a relation can
never be mistaken for a status and vice versa.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bioma.core.models import Evidence
from bioma.core.protocol_invariants import ProtocolViolation

RELATION_KEY = "relation"
SUPPORTS = "supports"
OPPOSES = "opposes"
CONTEXT = "context"

#: The complete, frozen relation vocabulary. Order is stable for messaging.
LINK_RELATIONS = (SUPPORTS, OPPOSES, CONTEXT)
LINK_RELATION_SET = frozenset(LINK_RELATIONS)


class RelationVocabularyViolation(ProtocolViolation):
    """E1c: a claim/evidence link carries no recognised relation."""

    code, invariant = "relation_vocabulary", "E1c"


def _payload(link: Evidence) -> Mapping[str, Any] | None:
    """The link payload when it is a mapping, otherwise ``None``."""
    value = link.value
    return value if isinstance(value, Mapping) else None


def raw_relation(link: Evidence) -> Any:
    """The unvalidated ``relation`` entry of a link, or ``None`` when absent."""
    payload = _payload(link)
    if payload is None:
        return None
    return payload.get(RELATION_KEY)


def parse_relation(link: Evidence) -> str:
    """The link's canonical relation, or raise E1c.

    A missing relation and an unknown relation are both rejections: WP2 never
    assumes "supports" and never ignores a relation it does not recognise.
    """
    expected = ", ".join(LINK_RELATIONS)
    payload = _payload(link)
    if payload is None or RELATION_KEY not in payload:
        raise RelationVocabularyViolation(
            f"claim_evidence_link {link.id!r} declares no {RELATION_KEY!r}; "
            f"expected one of: {expected}"
        )
    relation = payload[RELATION_KEY]
    if not isinstance(relation, str) or relation not in LINK_RELATION_SET:
        raise RelationVocabularyViolation(
            f"claim_evidence_link {link.id!r} has unknown {RELATION_KEY} "
            f"{relation!r}; expected one of: {expected}"
        )
    return relation


def link_relation(link: Evidence) -> str | None:
    """Derivation-time read: the canonical relation, or ``None``.

    Production never sees a non-canonical relation because commit rejects it.
    The reader stays tolerant so that no status is ever derived from a missing
    or unknown relation; it never falls back to ``SUPPORTS``.
    """
    relation = raw_relation(link)
    if isinstance(relation, str) and relation in LINK_RELATION_SET:
        return relation
    return None


__all__ = [
    "CONTEXT",
    "LINK_RELATIONS",
    "LINK_RELATION_SET",
    "OPPOSES",
    "RELATION_KEY",
    "RelationVocabularyViolation",
    "SUPPORTS",
    "link_relation",
    "parse_relation",
    "raw_relation",
]
