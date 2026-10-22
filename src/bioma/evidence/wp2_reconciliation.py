"""Read-only reconciliation between Com proposals and authoritative WP2 views."""
from __future__ import annotations
from typing import Any
from bioma.core.scientific_relations import CONTEXT, OPPOSES, SUPPORTS
from bioma.core.scientific_scope import DeclaredScope
from bioma.core.scientific_status import derive_claim_status, link_tally
from bioma.core.scientific_views import SCIENTIFIC_CLAIM_KIND, parse_claim

COM_TO_WP2_RELATION = {"supports": SUPPORTS, "contradicts": OPPOSES,
    "opposes": OPPOSES, "context": CONTEXT, "insufficient": None,
    "evidence_gap": None}

def map_com_relation(relation: str) -> str | None:
    try:
        return COM_TO_WP2_RELATION[relation.casefold()]
    except KeyError as exc:
        raise ValueError(f"Unknown Com relation: {relation!r}") from exc

def canonical_scope(scope: Any) -> dict[str, Any]:
    return ({"declared": True, "values": sorted(scope.values)}
            if isinstance(scope, DeclaredScope) else {"declared": False, "values": []})

def wp2_claim_projection(state, claim_key: str, statement: str) -> dict[str, Any] | None:
    claim = next((item for item in state.evidence if item.kind == SCIENTIFIC_CLAIM_KIND
                  and (item.id == claim_key or item.claim == statement)), None)
    if claim is None:
        return None
    tally = link_tally(state.evidence, claim.id)
    return {"claim_identity": claim.id, "canonical_scope": canonical_scope(parse_claim(claim).scope),
        "authoritative_status": derive_claim_status(state.evidence, claim.id),
        "relation_tally": {"supports": tally.supports, "opposes": tally.opposes,
            "context": tally.context, "excluded_by_scope": tally.excluded_by_scope,
            "excluded_unknown_relation": tally.excluded_unknown_relation},
        "commit_state_version": state.version}
