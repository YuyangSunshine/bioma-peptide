"""Ground options against registered, currently qualified implementations."""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from bioma.core.capability_binding import QualifiedCapabilityBinding, GroundedStep, GroundedOption
from bioma.core.scientific_capabilities import CapabilityRequirement, FAMILIES
from bioma.core.methods import implementation_identity, method_candidates
from bioma.core.models import ScientificState, TaskRequest
from bioma.core.research_contract import ResearchContract
from .options import ScientificOption
from .plan import ResearchPlan
from .relations import FrozenModel, Reference


class GroundingFailure(str, Enum):
    NO_CAPABILITY = "NO_CAPABILITY"
    PROVIDER_UNQUALIFIED = "PROVIDER_UNQUALIFIED"
    METHOD_RESTRICTED = "METHOD_RESTRICTED"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
    INCOMPATIBLE_DATAFLOW = "INCOMPATIBLE_DATAFLOW"
    VERIFICATION_UNDEFINED = "VERIFICATION_UNDEFINED"
    GROUNDING_STALE = "GROUNDING_STALE"


class GroundingResult(FrozenModel):
    status: Literal["GROUNDED", "BLOCKED"]
    alternatives: tuple[GroundedOption, ...] = ()
    failures: tuple[GroundingFailure, ...] = ()
    details: tuple[str, ...] = ()

    @model_validator(mode="after")
    def coherent(self):
        if (self.status == "GROUNDED") != bool(self.alternatives):
            raise ValueError("grounding status and alternatives disagree")
        if self.status == "BLOCKED" and not self.failures:
            raise ValueError("blocked grounding requires a typed failure")
        return self


def runtime_fingerprint(registry, active_agents, resources):
    rows = [item.model_dump(mode="json") for item in registry.all()
            if item.agent_id in active_agents]
    return hashlib.sha256(json.dumps({"capabilities": sorted(rows, key=lambda r:(r["agent_id"],r["id"])),
        "resources": sorted(resources or [])}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ground_option(option: ScientificOption, *, contract: ResearchContract, plan: ResearchPlan,
                  state: ScientificState, registry, assessors: dict[str, Any],
                  active_agents: set[str], available_resources: set[str] | None) -> GroundingResult:
    """Return every bounded exact implementation; never select scientific value."""
    snapshots = (contract.model_dump_json(), plan.model_dump_json(), state.model_dump_json())
    if (option.contract_ref, option.contract_version) != (contract.identity, contract.version) or (
            option.plan_ref, option.plan_version, option.plan_identity_hash) != plan.version_identity:
        return GroundingResult(status="BLOCKED", failures=(GroundingFailure.GROUNDING_STALE,),
                               details=("option snapshot identity is stale",))
    if set(option.target_plan_node_refs) - {n.node_id for n in plan.nodes} or set(
            option.target_contract_obligation_refs) - {r.id for r in contract.requirements}:
        return GroundingResult(status="BLOCKED", failures=(GroundingFailure.GROUNDING_STALE,),
                               details=("option targets are absent",))
    available_classes = {e.kind for e in state.evidence}
    failures, details, choices_by_step, produced = set(), [], [], {}
    inventory = set(available_resources or ())
    required_option_resources = set(option.required_resources)
    if (available_resources is None and required_option_resources) or (
            available_resources is not None and required_option_resources - inventory):
        failures.add(GroundingFailure.RESOURCE_UNAVAILABLE)
        details.append(f"option: unavailable resources {sorted(required_option_resources - inventory)}")
    for step in option.steps:
        inherited = {kind for parent in step.depends_on_step_ids for kind in produced.get(parent, ())}
        missing = set(step.required_input_classes) - available_classes - inherited
        if missing:
            failures.add(GroundingFailure.INPUT_UNAVAILABLE)
            details.append(f"{step.step_id}: unavailable inputs {sorted(missing)}")
        for parent in step.depends_on_step_ids:
            if not set(step.required_input_classes).intersection(produced.get(parent, ())):
                failures.add(GroundingFailure.INCOMPATIBLE_DATAFLOW)
                details.append(f"{parent}->{step.step_id}: incompatible dataflow")
        bindings = json.loads(step.input_bindings_json)
        required_step_resources = required_option_resources | set(step.required_resources)
        if (available_resources is None and required_step_resources) or (
                available_resources is not None and required_step_resources - inventory):
            failures.add(GroundingFailure.RESOURCE_UNAVAILABLE)
            details.append(f"{step.step_id}: unavailable resources {sorted(required_step_resources - inventory)}")
        payload = {**bindings, "inputs": bindings,
            "result_requirements": {"evidence_kinds": list(step.expected_result_classes),
                                    "artifact_kinds": list(step.expected_artifact_classes)},
            "execution_requirements": {"responsibility": step.capability,
                                       "method_id": step.method_hint}}
        if step.capability in FAMILIES:
            requirement = CapabilityRequirement(capability_family=step.capability,
                objective=step.description, constraints={'method_id': step.method_hint},
                result_requirements=payload['result_requirements'],
                required_resources=sorted(required_step_resources), budget=option.budget)
            payload['execution_requirements'] = requirement.effective_execution_requirements().model_dump(mode='json')
        request_id = "grounding:" + hashlib.sha256(
            f"{option.identity}:{step.step_id}".encode()).hexdigest()
        request = TaskRequest(id=request_id, requester=option.proposer_id, required_capability=step.capability,
            objective=step.description, message_type=step.message_type, payload=payload,
            budget=option.budget, selection_mode="all",
            created_at=datetime(1970, 1, 1, tzinfo=timezone.utc))
        qualified, audit = registry.qualify(request, active_agents,
            available_resources=available_resources)
        step_choices = []
        if not qualified:
            resource_rejected = any("resources unavailable" in str(row.get("reason")) or
                                    "no_runtime_inventory" in str(row) for row in audit)
            failures.add(GroundingFailure.RESOURCE_UNAVAILABLE if resource_rejected
                         else GroundingFailure.NO_CAPABILITY)
        for capability in qualified:
            assessor = assessors.get(capability.agent_id)
            if assessor is None:
                failures.add(GroundingFailure.PROVIDER_UNQUALIFIED)
                continue
            proposal = assessor(request, capability)
            methods, _ = method_candidates(capability, step.message_type, payload)
            method = next((m for m in methods if m.id == proposal.method_id), None)
            if not proposal.accepted or method is None or proposal.provider_id != capability.agent_id or (
                    proposal.result_contract != method.result.model_dump(mode="json")):
                failures.add(GroundingFailure.PROVIDER_UNQUALIFIED)
                continue
            from bioma.core.method_inputs import method_input_reasons
            missing_inputs = method_input_reasons(method, state, bindings.get('input_refs', ()), available_resources)
            if missing_inputs:
                failures.add(GroundingFailure.INPUT_UNAVAILABLE)
                details.extend(f"{step.step_id}/{method.id}: {reason}" for reason in missing_inputs)
                continue
            restrictions = [r for r in contract.method_restrictions
                if r.obligation_ref is None or r.obligation_ref in option.target_contract_obligation_refs]
            if any((r.method_id and r.method_id != method.id) or
                   (r.capability and r.capability != capability.id) or
                   (r.engine and not any(p.engine == r.engine for p in method.profiles)) for r in restrictions):
                failures.add(GroundingFailure.METHOD_RESTRICTED)
                continue
            if not method.result.verification_obligations or not capability.verification_strategy:
                failures.add(GroundingFailure.VERIFICATION_UNDEFINED)
                continue
            profile = capability.runtime_profile or {}
            version = profile.get("implementation_version")
            implementation = implementation_identity(capability, method)
            step_choices.append(GroundedStep(step_id=step.step_id, capability_id=capability.id,
                method_id=method.id, provider_id=capability.agent_id,
                implementation_identity=implementation,
                implementation_version=str(version) if version is not None else None,
                version_provenance="declared" if version is not None else "missing",
                message_type=step.message_type, input_bindings_json=step.input_bindings_json,
                result_contract_json=json.dumps(method.result.model_dump(mode="json"), sort_keys=True,
                                                separators=(",", ":")),
                required_resources=tuple(sorted(set(capability.resources)|required_step_resources)),
                # Current method contracts do not declare a closed side-effect
                # class. Preserve uncertainty instead of guessing from a role.
                effect_class="UNKNOWN",
                verification_strategy=capability.verification_strategy,
                estimated_cost=capability.estimated_cost,
                estimated_latency_seconds=capability.estimated_latency_seconds,
                qualification_provenance_json=json.dumps({"registry":audit,"offer":proposal.model_dump(mode="json")},
                                                         sort_keys=True, separators=(",", ":"), default=str),
                depends_on_step_ids=step.depends_on_step_ids,
                capability_binding=(QualifiedCapabilityBinding.from_qualification(
                    capability, method, request, proposal, audit) if capability.family in FAMILIES else None)))
        choices_by_step.append(step_choices)
        produced[step.step_id] = step.expected_result_classes
    alternatives = []
    blocking = {GroundingFailure.INCOMPATIBLE_DATAFLOW,
                GroundingFailure.INPUT_UNAVAILABLE,
                GroundingFailure.RESOURCE_UNAVAILABLE}
    if all(choices_by_step) and not blocking.intersection(failures):
        runtime = runtime_fingerprint(registry, active_agents, available_resources)
        alternatives = [GroundedOption(option_identity=option.identity, contract_ref=contract.identity,
            contract_version=contract.version, plan_ref=plan.plan_id, plan_version=plan.version,
            plan_identity_hash=plan.version_identity[2], runtime_fingerprint=runtime, steps=tuple(combo))
            for combo in itertools.product(*choices_by_step)]
    assert snapshots == (contract.model_dump_json(), plan.model_dump_json(), state.model_dump_json())
    if alternatives:
        return GroundingResult(status="GROUNDED", alternatives=tuple(alternatives))
    return GroundingResult(status="BLOCKED", failures=tuple(sorted(failures, key=lambda f:f.value)),
                           details=tuple(details))


def grounding_is_current(grounded: GroundedOption, *, contract, plan, registry,
                         active_agents, available_resources) -> bool:
    return ((grounded.contract_ref, grounded.contract_version) == (contract.identity, contract.version)
        and (grounded.plan_ref, grounded.plan_version, grounded.plan_identity_hash) == plan.version_identity
        and grounded.runtime_fingerprint == runtime_fingerprint(registry, active_agents, available_resources))
