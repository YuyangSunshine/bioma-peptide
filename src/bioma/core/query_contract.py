"""Query provenance validation shared by compiler and gateway."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bioma.gateway import QuerySpec

class QueryInterpretationError(ValueError):
    """A query cannot be bound to a supported, unambiguous execution contract."""


def verify_query_spec(query: str, payload: dict[str, Any]) -> QuerySpec:
    """Revalidate immutable provenance at the runtime admission boundary."""
    from bioma.gateway import QuerySpec, source_fragments
    spec = QuerySpec.model_validate(payload)
    expected = source_fragments(query)
    if spec.source_fragments != expected:
        raise QueryInterpretationError("QuerySpec fragments do not match the original query")
    known = {row.ref for row in expected}
    for field, refs in spec.source_refs.items():
        if not refs or set(refs) - known:
            raise QueryInterpretationError(f"Invalid QuerySpec provenance for {field}")
    return spec


def query_input_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project gateway hypotheses for observation, never attest resource absence.

    Query has no tool receipts. In particular its legacy ``blocking`` flag is
    an analysis dependency proposal, not execution/permission evidence. Actual
    Method input validation, WP1 and scoped file access remain authoritative.
    The original hypotheses are retained even when observation later resolves
    them; they are not a live list of verified missing resources.
    """
    return [{"field": row["field"], "reason": row["reason"],
             "proposed_blocking": row.get("blocking", True),
             "origin": "query_interpretation", "verification": "not_observed",
             "scope": "dependent_analysis", "resolution": "observe_or_clarify"}
            for row in payload.get("missing_required_user_inputs", [])
            if row.get("verification", "unverified") == "unverified"]
