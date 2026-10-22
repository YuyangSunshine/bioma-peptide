from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from bioma.core.models import CapabilitySpec, TaskRequest, utc_now

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class CapabilityRegistry:
    """Registry plus deterministic, inspectable eligibility checks."""

    def __init__(self, capabilities: Iterable[CapabilitySpec] = ()) -> None:
        self._capabilities: dict[tuple[str, str], CapabilitySpec] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: CapabilitySpec) -> None:
        from bioma.core.community import ExecutionProfile

        profiles = [
            ExecutionProfile.model_validate(p) for p in capability.execution_profiles
        ]
        if len({p.id for p in profiles}) != len(profiles):
            raise ValueError("Duplicate execution profile identity")
        if len({m.id for m in capability.methods}) != len(capability.methods):
            raise ValueError("Duplicate method identity within a responsibility")
        for method in capability.methods:
            if method.result.responsibility not in {capability.id, capability.family}:
                raise ValueError("Method result responsibility differs from its capability")
            if method.message_type not in capability.accepts_message_types:
                raise ValueError("Method message is absent from its responsibility contract")
        key = (capability.agent_id, capability.id)
        if key in self._capabilities:
            raise ValueError(
                f"Duplicate capability {capability.id!r} for {capability.agent_id!r}."
            )
        try:
            Draft202012Validator.check_schema(capability.input_schema)
        except SchemaError as exc:
            raise ValueError(
                f"Invalid input schema for {capability.agent_id!r}/"
                f"{capability.id!r}: {exc.message}"
            ) from exc
        self._capabilities[key] = capability

    def match(
        self, capability_id: str, active_agents: set[str]
    ) -> list[CapabilitySpec]:
        """Capability-ID lookup; use qualification for request-specific checks."""
        matches = [
            item
            for item in self._capabilities.values()
            if item.enabled
            and capability_id in {item.id, item.family}
            and item.agent_id in active_agents
        ]
        return sorted(
            matches, key=lambda item: (-item.priority, item.agent_id, item.id)
        )

    def qualify(
        self,
        request: TaskRequest,
        active_agents: set[str],
        *,
        available_resources: set[str] | None = None,
        now: datetime | None = None,
    ) -> tuple[list[CapabilitySpec], tuple[dict[str, object], ...]]:
        """Return eligible providers and an audit record for every known peer.

        String preconditions cannot authorize execution. Scientific predicates
        are evaluated against the authorized observation at admission.
        """

        now = now or utc_now()
        by_agent: dict[str, list[CapabilitySpec]] = {}
        for item in self._capabilities.values():
            by_agent.setdefault(item.agent_id, []).append(item)

        eligible: list[CapabilitySpec] = []
        records: list[dict[str, object]] = []
        known_agents = set(by_agent) | set(active_agents)
        for agent_id in sorted(known_agents):
            advertised = sorted(
                item.id for item in by_agent.get(agent_id, []) if item.enabled
            )
            candidates = [
                item
                for item in by_agent.get(agent_id, [])
                if request.required_capability in {item.id, item.family}
            ]
            if not candidates:
                records.append(
                    {
                        "agent_id": agent_id,
                        "eligible": False,
                        "reason": "required capability not advertised",
                        "advertised_capabilities": advertised,
                        "checks": {
                            "active": agent_id in active_agents,
                            "capability": False,
                        },
                    }
                )
                continue

            for capability in candidates:
                from bioma.core.community import ExecutionProfile, requirements_from

                required = requirements_from(request.payload)
                profiles = [
                    ExecutionProfile.model_validate(p)
                    for p in capability.execution_profiles
                ]
                profile_ok = (
                    any(not p.rejection(required) for p in profiles)
                    if profiles
                    else not (
                        required.engine
                        or required.modalities
                        or required.output_modalities
                        or required.operations
                    )
                )
                method_details = []
                if capability.methods:
                    from bioma.core.methods import method_candidates
                    methods, method_details = method_candidates(capability, request.message_type, request.payload)
                    profile_ok = bool(methods)
                schema_error = next(
                    iter(
                        Draft202012Validator(capability.input_schema).iter_errors(
                            request.payload
                        )
                    ),
                    None,
                )
                message_type_ok = (
                    not capability.accepts_message_types
                    or request.message_type in capability.accepts_message_types
                )
                budget_ok = (
                    request.budget is None
                    or capability.estimated_cost <= request.budget
                )
                deadline_ok = request.deadline is None or (
                    request.deadline >= now
                    and capability.estimated_latency_seconds
                    <= (request.deadline - now).total_seconds()
                )
                risk_ok = (
                    RISK_ORDER[request.risk_level]
                    <= RISK_ORDER[capability.max_risk_level]
                )
                missing_resources = (
                    []
                    if available_resources is None
                    else sorted(set(capability.resources) - available_resources)
                )
                resources_ok = not missing_resources and not (capability.resources and available_resources is None)
                checks = {
                    "active": agent_id in active_agents,
                    "enabled": capability.enabled,
                    "capability": True,
                    "message_type": message_type_ok,
                    "input_schema": schema_error is None,
                    "execution_profile": profile_ok,
                    "budget": budget_ok,
                    "deadline": deadline_ok,
                    "risk": risk_ok,
                    "resources": (
                        "rejected:no_runtime_inventory"
                        if available_resources is None and capability.resources
                        else resources_ok
                    ),
                    "preconditions": (
                        "rejected:untyped_preconditions"
                        if capability.preconditions
                        else True
                    ),
                }
                enforced = (
                    bool(checks["active"])
                    and bool(checks["enabled"])
                    and message_type_ok
                    and schema_error is None
                    and budget_ok
                    and deadline_ok
                    and risk_ok
                    and resources_ok
                    and profile_ok
                    and not capability.preconditions
                )
                reasons: list[str] = []
                if not checks["active"]:
                    reasons.append("agent is outside the active task scope")
                if not checks["enabled"]:
                    reasons.append("capability is disabled")
                if not message_type_ok:
                    reasons.append("message type is not accepted")
                if schema_error is not None:
                    reasons.append(
                        f"input schema rejected payload: {schema_error.message}"
                    )
                if not budget_ok:
                    reasons.append("estimated cost exceeds request budget")
                if not deadline_ok:
                    reasons.append("deadline cannot be met")
                if not risk_ok:
                    reasons.append("request risk exceeds capability policy")
                if missing_resources:
                    reasons.append(
                        f"resources unavailable: {', '.join(missing_resources)}"
                    )
                if not profile_ok:
                    reasons.append(
                        "No available execution profile meets the requested conditions"
                    )
                if enforced:
                    eligible.append(capability)
                records.append(
                    {
                        "agent_id": agent_id,
                        "eligible": enforced,
                        "reason": "; ".join(reasons)
                        or "all enforced eligibility checks passed",
                        "advertised_capabilities": advertised,
                        "checks": checks,
                        "method_rejections": method_details,
                    }
                )
        return (
            sorted(eligible, key=lambda item: (-item.priority, item.agent_id, item.id)),
            tuple(records),
        )

    def qualification(
        self, capability_id: str, active_agents: set[str]
    ) -> tuple[dict[str, object], ...]:
        """Explain capability-only lookup; request qualification adds profile checks."""
        by_agent: dict[str, list[CapabilitySpec]] = {}
        for item in self._capabilities.values():
            by_agent.setdefault(item.agent_id, []).append(item)
        records = []
        for agent_id in sorted(set(by_agent) | set(active_agents)):
            advertised = sorted(
                item.id for item in by_agent.get(agent_id, []) if item.enabled
            )
            eligible = agent_id in active_agents and capability_id in advertised
            records.append(
                {
                    "agent_id": agent_id,
                    "eligible": eligible,
                    "reason": (
                        "capability and active-scope checks passed"
                        if eligible
                        else "required capability is unavailable in active scope"
                    ),
                    "advertised_capabilities": advertised,
                    "checks": {
                        "active": agent_id in active_agents,
                        "capability": capability_id in advertised,
                        "request_contract": "deferred:no_TaskRequest",
                    },
                }
            )
        return tuple(records)

    def all(self) -> tuple[CapabilitySpec, ...]:
        return tuple(
            sorted(
                self._capabilities.values(), key=lambda item: (item.agent_id, item.id)
            )
        )


def advertise_capabilities(self, *capabilities: CapabilitySpec, role_charter="") -> None:
    import hashlib
    import json

    for capability in capabilities:
        if capability.agent_id not in self.peer_ids:
            raise ValueError("Capability agent_id must be exposed by this Agent.")
        profile = {
            **capability.runtime_profile,
            "implementation_version": "community-6.0."
            + str(capability.runtime_profile.get("implementation_version", "1"))
            + ".contract-" + hashlib.sha256(json.dumps(capability.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()[:16],
            "role_charter": role_charter,
            "community_protocol": "1",
        }
        self.capabilities.append(
            capability.model_copy(
                update={
                    "family": capability.family
                    or capability.id,
                    "runtime_profile": profile,
                }
            )
        )
