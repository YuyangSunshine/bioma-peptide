"""Pure views: readiness != completion != satisfaction != epistemic support.

Callers supply trusted completion/acceptance sets for the referenced snapshot.
These functions neither verify execution nor grant acceptance/commit authority.
"""
from collections.abc import Iterable
from dataclasses import dataclass

from .plan import Lifecycle, ResearchPlan
from .relations import EnablesClause, RequiresClause, SatisfactionLink


@dataclass(frozen=True)
class ReadinessView:
    ready: frozenset[str]
    completed: frozenset[str]
    available_conditions: frozenset[str]


def derive_readiness(
    plan: ResearchPlan,
    completed_node_ids: Iterable[str] = (),
    *,
    blocked_node_ids: Iterable[str] = (),
    superseded_node_ids: Iterable[str] = (),
) -> ReadinessView:
    """Only pending, incomplete, unblocked nodes can be ready.

    No prerequisites means ready. Explicit completion remains historical fact
    even if that node is later blocked/superseded, but such a node cannot serve
    as a usable source. ACTIVE nodes are not candidates for another start.
    ENABLES conditions never implicitly gate REQUIRES or start work.
    """
    completed = frozenset(completed_node_ids)
    blocked = frozenset(blocked_node_ids)
    superseded = frozenset(superseded_node_ids)
    ids = {node.node_id for node in plan.nodes}
    if (completed | blocked | superseded) - ids:
        raise ValueError("unknown progress/state node reference")
    unavailable = blocked | superseded | {
        node.node_id for node in plan.nodes
        if node.lifecycle in (Lifecycle.BLOCKED, Lifecycle.SUPERSEDED)
    }
    usable = completed - unavailable
    requirements: dict[str, list[RequiresClause]] = {}
    available = set()
    for clause in plan.dependencies:
        if isinstance(clause, RequiresClause):
            requirements.setdefault(clause.target, []).append(clause)
        elif isinstance(clause, EnablesClause) and set(clause.sources) <= usable:
            available.add(clause.condition_ref)
    ready = frozenset(
        node.node_id for node in plan.nodes
        if node.lifecycle == Lifecycle.PENDING
        and node.node_id not in completed | unavailable
        and (node.node_id not in requirements or any(
            set(clause.sources) <= usable for clause in requirements[node.node_id]
        ))
    )
    return ReadinessView(ready, completed, frozenset(available))


def satisfied_obligations(
    links: Iterable[SatisfactionLink],
    accepted_result_refs: Iterable[str],
    *,
    contract_obligation_refs: Iterable[str],
) -> frozenset[str]:
    """Exact matching only; obligation identities come from ResearchContract.

    The supplied refs must belong to that contract snapshot. No artifacts,
    plan progress, forecasts, or evidence relations are scanned for acceptance.
    """
    accepted = frozenset(accepted_result_refs)
    obligations = frozenset(contract_obligation_refs)
    result = set()
    for link in links:
        if not isinstance(link, SatisfactionLink):
            raise TypeError("expected an explicit SatisfactionLink")
        if link.contract_obligation_ref not in obligations:
            raise ValueError("unknown contract obligation reference")
        if link.accepted_result_ref in accepted:
            result.add(link.contract_obligation_ref)
    return frozenset(result)
