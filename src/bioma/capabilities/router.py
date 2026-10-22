from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from bioma.core.models import AgentMessage, Commitment, TaskOffer, TaskRequest

from .registry import CapabilityRegistry


@dataclass(frozen=True)
class RouteDecision:
    request: TaskRequest
    offers: tuple[TaskOffer, ...]
    commitments: tuple[Commitment, ...]
    messages: tuple[AgentMessage, ...]
    qualification: tuple[dict[str, object], ...]


class CapabilityRouter:
    """Resolve task requests without a planner or hard-coded recipient."""

    def __init__(self, registry: CapabilityRegistry, assessors=None) -> None:
        self.registry = registry
        self.assessors = assessors or {}

    def route(
        self,
        request: TaskRequest,
        *,
        active_agents: set[str],
        ttl: int,
        available_resources: set[str] | None = None,
        capability_binding: str | None = None,
        provider_binding: str | None = None,
        method_binding: str | None = None,
        implementation_binding: str | None = None,
    ) -> RouteDecision:
        candidates, qualification = self.registry.qualify(
            request,
            active_agents,
            available_resources=available_resources,
        )
        if capability_binding is not None:
            candidates = [item for item in candidates if item.id == capability_binding]
            qualification = tuple((*qualification, {
                "phase": "capability_binding",
                "capability_id": capability_binding,
                "eligible": bool(candidates),
                "reason": (
                    "bound to the Method V3 selected scientific action"
                    if candidates else
                    "selected scientific action has no qualified provider"
                ),
            }))
        if provider_binding is not None:
            candidates = [item for item in candidates if item.agent_id == provider_binding]
        if method_binding is not None:
            candidates = [item for item in candidates if any(
                method.id == method_binding for method in item.methods)]
        if implementation_binding is not None:
            from bioma.core.methods import implementation_identity
            candidates = [item for item in candidates if any(
                method.id == method_binding and implementation_identity(item, method) == implementation_binding
                for method in item.methods)]
        if any(value is not None for value in (
                provider_binding, method_binding, implementation_binding)):
            qualification = tuple((*qualification, {
                "phase": "exact_grounded_binding",
                "provider_id": provider_binding,
                "capability_id": capability_binding,
                "method_id": method_binding,
                "implementation_identity": implementation_binding,
                "eligible": bool(candidates),
                "reason": "exact grounded binding remains qualified" if candidates else
                          "GROUNDING_STALE: exact grounded binding is unavailable",
            }))
        proposals = {}
        accepted = []
        qualification = list(qualification)
        for item in candidates:
            assessor = self.assessors.get(item.agent_id)
            if assessor is None and item.methods:
                qualification.append({'agent_id': item.agent_id, 'eligible': False,
                    'reason': 'Executable method offers require a provider assessor'})
                continue
            if assessor is not None:
                proposal = assessor(request, item)
                if (
                    proposal.provider_id != item.agent_id
                    or proposal.request_id != request.id
                    or proposal.capability_id != item.id
                ):
                    raise ValueError(
                        "Offer proposal identity does not match the qualified provider"
                    )
                qualification.append(
                    {
                        "agent_id": item.agent_id,
                        "phase": "provider_assessment",
                        "eligible": proposal.accepted,
                        "reason": proposal.reason,
                        "proposal": proposal.model_dump(mode="json"),
                    }
                )
                if not proposal.accepted:
                    continue
                if item.methods:
                    from bioma.core.methods import method_candidates
                    eligible, _ = method_candidates(item, request.message_type, request.payload)
                    chosen = next((m for m in eligible if m.id == proposal.method_id), None)
                    if chosen is None or proposal.result_contract != chosen.result.model_dump(mode='json'):
                        raise ValueError('Provider offer changed the registered method/result contract')
                proposals[(item.agent_id, item.id)] = proposal
            accepted.append(item)
        offers = tuple(
            sorted(
                (
                    TaskOffer(
                        task_request_id=request.id,
                        agent_id=item.agent_id,
                        capability_id=item.id,
                        method_id=proposals[(item.agent_id, item.id)].method_id if (item.agent_id, item.id) in proposals else None,
                        score=_offer_score(item),
                        reason=(
                            f"{proposals[(item.agent_id, item.id)].reason if (item.agent_id, item.id) in proposals else 'registry-qualified'}; "
                            f"qualified for {item.id}; reliability={item.reliability:.2f}, "
                            f"priority={item.priority}, cost={item.estimated_cost:.2f}, "
                            f"latency={item.estimated_latency_seconds:.1f}s"
                        ),
                        estimated_cost=item.estimated_cost,
                        estimated_latency_seconds=item.estimated_latency_seconds,
                        reliability=item.reliability,
                    )
                    for item in accepted
                ),
                key=lambda offer: (-offer.score, offer.agent_id),
            )
        )
        if request.selection_mode == "single":
            selected = offers[:1]
        elif request.selection_mode == "all":
            selected = offers
        else:
            selected = offers[: request.max_providers]

        # The requester's declared selection policy (never a scientific ranking
        # by the runtime) chooses which offers become commitments. Each
        # commitment records the exact offer it accepted.
        commitments = tuple(
            Commitment(
                task_request_id=request.id,
                agent_id=offer.agent_id,
                capability_id=offer.capability_id,
                method_id=offer.method_id,
                lease_expires_at=request.created_at + timedelta(minutes=5),
                retry_policy={"on_expiry": "reopen_request", "max_retries": 2},
                offer_id=offer.id,
                accepted_obligations=(
                    list(proposals[(offer.agent_id, offer.capability_id)].verification_obligations)
                    if (offer.agent_id, offer.capability_id) in proposals
                    else []
                ),
                resource_budget=request.budget,
            )
            for offer in selected
        )
        messages = tuple(
            AgentMessage(
                sender=request.requester,
                recipient=offer.agent_id,
                message_type=request.message_type,
                payload={
                    **request.payload,
                    "task_request_id": request.id,
                    "selected_capability": offer.capability_id,
                    "provider_binding": next({
                        'provider_kind': item.runtime_profile.get('provider_kind', 'role'),
                        'implementation_version': str(item.runtime_profile.get('implementation_version', '1')),
                        'generation': item.runtime_profile.get('lifecycle', {}).get('generation', 1)
                    } for item in accepted if item.agent_id == offer.agent_id and item.id == offer.capability_id),
                    **({"method_id": offer.method_id} if offer.method_id else {}),
                    **(
                        {
                            "accepted_offer": proposals[
                                (offer.agent_id, offer.capability_id)
                            ].model_dump(mode="json")
                        }
                        if (offer.agent_id, offer.capability_id) in proposals
                        else {}
                    ),
                },
                task_request_id=request.id,
                correlation_id=request.correlation_id or request.id,
                causation_id=request.causation_id,
                idempotency_key=request.id,
                ttl=ttl,
            )
            for offer in selected
        )
        return RouteDecision(
            request, offers, commitments, messages, tuple(qualification)
        )


def _offer_score(capability) -> float:
    """Rank qualified offers from explicit, reproducible runtime metadata."""
    profile = capability.runtime_profile or {}
    parse_errors = int(profile.get("skill_parse_errors") or 0)
    load_errors = int(profile.get("tool_load_errors") or 0)
    readiness = max(0.0, 1.0 - min(parse_errors + load_errors, 10) / 10)
    priority = min(max(capability.priority, 0), 100) / 100
    cost_penalty = min(capability.estimated_cost, 10.0) / 10
    latency_penalty = min(capability.estimated_latency_seconds, 600.0) / 600
    score = (
        0.60 * capability.reliability
        + 0.20 * readiness
        + 0.15 * priority
        - 0.03 * cost_penalty
        - 0.02 * latency_penalty
    )
    return round(max(0.0, min(score, 1.0)), 6)
