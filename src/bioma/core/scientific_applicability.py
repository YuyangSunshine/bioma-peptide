"""Read-side, use-specific applicability. Never mutates historical support.

The checker is an independently selected trusted scientific validator, not an
Agent callback or a payload flag. It must verify source/result identities and
complete dependencies for the exact claim, support link, baseline and use.
Returning None means that this obligation could not be established. Generic
provenance or a successful process exit cannot establish completeness.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from bioma.core.models import Evidence, ScientificState
from bioma.core.scientific_status import derive_claim_status
from bioma.core.scientific_scope import covers
from bioma.core.scientific_views import claim_scope


@dataclass(frozen=True)
class Applicability:
    claim_id: str
    support_link_id: str
    use: str
    historical_status: str
    status: Literal["applicable", "reassessment_required", "unknown"]
    affected_dependencies: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()


# Return exact context keys only after independently checking the full support
# bundle. Nested Claim dependencies must be checked too, or return None.
DependencyCheck = Callable[
    [Evidence, Evidence, tuple[Evidence, ...], str, Mapping[str, str]],
    frozenset[str] | None,
]


def assess_support_use(
    state: ScientificState, *, claim_id: str, support_link_id: str,
    use: str, baseline: Mapping[str, str], current: Mapping[str, str],
    check_dependencies: DependencyCheck,
) -> Applicability:
    """Compare versioned condition identities after independent qualification.

    Context values are immutable identity tokens (e.g. hashes), not semantic
    guesses. Missing keys are unknown, including when a known change exists.
    This is an assessment of one selected support use, not delivery approval,
    a global Claim status, or authority to execute/reuse artifacts.
    """
    # Assess a stable snapshot, even if a checker retains caller references.
    evidence = [item.model_copy(deep=True) for item in state.evidence]
    baseline, current = dict(baseline), dict(current)
    records = {item.id: item for item in evidence}
    historical = "unknown"

    def result(status, *, affected=(), gaps=()):
        return Applicability(claim_id, support_link_id, use, historical, status,
                             tuple(sorted(affected)), tuple(gaps))

    if len(records) != len(evidence):
        return result("unknown", gaps=("ambiguous_evidence_identity",))
    claim, link = records.get(claim_id), records.get(support_link_id)
    if claim is None or claim.kind != "scientific_claim":
        return result("unknown", gaps=("missing_scientific_claim",))
    historical = derive_claim_status(evidence, claim_id)
    if (not isinstance(use, str) or not use.strip() or link is None or
        link.kind != "claim_evidence_link" or
        link.parent_ids[:1] != [claim_id] or len(link.parent_ids) < 2 or
        not isinstance(link.value, dict) or link.value.get("relation") != "supports"):
        return result("unknown", gaps=("selected_support_not_established",))
    targets = tuple(records.get(ref) for ref in link.parent_ids[1:])
    if any(target is None for target in targets):
        return result("unknown", gaps=("missing_support_target",))
    if (claim_id in link.parent_ids[1:] or
            len(set(link.parent_ids)) != len(link.parent_ids)):
        return result("unknown", gaps=("invalid_support_targets",))
    if any(target.kind == "claim_evidence_link" for target in targets):
        return result("unknown", gaps=("invalid_support_targets",))
    if any(target.kind == "scientific_claim" and
           not covers(claim_scope(claim), claim_scope(target)) for target in targets):
        return result("unknown", gaps=("support_scope_incompatible",))
    # Copies prevent a checker from accidentally changing committed history.
    try:
        dependencies = check_dependencies(
            claim.model_copy(deep=True), link.model_copy(deep=True),
            tuple(target.model_copy(deep=True) for target in targets),
            use, dict(baseline),
        )
    except (OSError, TimeoutError) as exc:
        # Missing source files / unavailable verification must not become reuse.
        # Programming errors still propagate rather than being hidden here.
        return result("unknown", gaps=(f"dependency_check_unavailable:{type(exc).__name__}",))
    if not isinstance(dependencies, frozenset) or not dependencies or any(
        not isinstance(key, str) or not key for key in dependencies
    ):
        return result("unknown", gaps=("complete_dependencies_not_verified",))
    missing = dependencies - baseline.keys() | dependencies - current.keys()
    if missing:
        return result("unknown", gaps=tuple(f"missing_condition:{key}" for key in sorted(missing)))
    invalid = [key for key in dependencies if
               not isinstance(baseline[key], str) or not baseline[key] or
               not isinstance(current[key], str) or not current[key]]
    if invalid:
        return result("unknown", gaps=tuple(f"invalid_identity:{key}" for key in sorted(invalid)))
    changed = {key for key in dependencies if baseline[key] != current[key]}
    return result("reassessment_required" if changed else "applicable", affected=changed)
