"""Executable methods and result obligations, separate from role responsibilities.

A responsibility selects who owns a result. A method describes one supported
way to produce that result. Neither a domain label nor an algorithm name grants
permission to claim that another role's result has been established.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from bioma.core.community import Contract, ExecutionProfile, ExecutionRequirements
from bioma.core.action_contracts import ScientificActionContract


class ResultContract(Contract):
    """Acceptable evidence alternatives, with jointly required artifact kinds."""
    responsibility: str = Field(min_length=1)
    result_channel: Literal["evidence", "work_result"] = "evidence"
    evidence_kinds: list[str] = Field(min_length=1)
    data_kinds: list[str] = Field(default_factory=list)
    artifact_kinds: list[str] = Field(default_factory=list)
    optional_artifact_kinds: list[str] = Field(default_factory=list)
    named_output_extensions: list[str] | None = None
    verification_obligations: list[str] = Field(min_length=1)
    stage: Literal["work", "review", "delivery"] = "work"


class ExecutionMethod(Contract):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    message_type: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    result: ResultContract
    profiles: list[ExecutionProfile] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    required_input_kinds: list[str] = Field(default_factory=list)
    required_evidence_refs: list[str] = Field(default_factory=list)
    execution_environment: list[str] = Field(default_factory=list)
    required_input_qualification: str = ""
    required_artifact_suffixes: list[str] = Field(default_factory=list)
    min_input_count: int = Field(default=1, ge=0)
    max_input_count: int | None = Field(default=None, ge=1)
    available: bool = True
    unavailable_reason: str = ""
    limitations: list[str] = Field(default_factory=list)
    entrypoint: bool = True
    action_contract: ScientificActionContract = Field(default_factory=ScientificActionContract)

    @model_validator(mode="after")
    def coherent_declaration(self):
        Draft202012Validator.check_schema(self.input_schema)
        if len({p.id for p in self.profiles}) != len(self.profiles):
            raise ValueError("Duplicate profile identity within a method")
        if not self.available and not self.unavailable_reason:
            raise ValueError("An unavailable method must explain its missing dependency")
        return self

    def rejection(self, message_type: str, payload: dict, required: ExecutionRequirements) -> list[str]:
        reasons = []
        if not self.available:
            reasons.append(self.unavailable_reason)
        if message_type != self.message_type:
            reasons.append("message type is not accepted by this method")
        if required.responsibility and required.responsibility != self.result.responsibility:
            reasons.append("method does not own the requested result responsibility")
        if required.method_id and required.method_id != self.id:
            reasons.append("a different method was explicitly required")
        schema_error = next(iter(Draft202012Validator(self.input_schema).iter_errors(payload)), None)
        if schema_error:
            reasons.append("method input rejected: " + schema_error.message)
        # The question defines acceptable results; providers choose a method
        # meeting them. Catalog examples are not mandatory future actions.
        obligations = []
        if payload.get('result_requirements'):
            obligations.append(payload['result_requirements'])
        # WorkResult production does not itself establish qualified Evidence.
        # Scientific obligations remain enforced by Runtime acceptance; explicit
        # result_requirements still constrain the producer's declared output.
        if self.result.result_channel != 'work_result':
            obligations.extend(r for r in (payload.get('research_assignment') or {}).get('acceptance', [])
                if r.get('capability') == self.result.responsibility and r.get('message_type') == message_type)
        obligations.extend({'evidence_kinds':[r['evidence']], 'artifact_kinds':r.get('artifacts', [])}
            for r in payload.get('delivery_contracts', []) if r.get('capability') == self.result.responsibility)
        for obligation in obligations:
            for key in ('evidence_kinds','data_kinds'):
                expected = obligation.get(key) or []
                if expected and not set(expected).intersection(getattr(self.result, key)):
                    reasons.append('method cannot establish required ' + key)
            if not set(obligation.get('artifact_kinds') or []) <= set(self.result.artifact_kinds + self.result.optional_artifact_kinds):
                # Generic analysis produces requested artifacts at runtime; its
                # contract deliberately has no fixed domain-specific file kinds.
                if self.result.artifact_kinds or self.result.responsibility != 'scientific.data_analysis':
                    reasons.append('method cannot produce required artifact kinds')
            if self.result.named_output_extensions is not None:
                from pathlib import Path
                if any(Path(name).suffix.lower() not in self.result.named_output_extensions
                       for name in obligation.get('artifact_names', [])):
                    reasons.append('method cannot produce the requested named output format')
        if self.profiles:
            if not any(not profile.rejection(required) for profile in self.profiles):
                reasons.append("no method profile supports the requested inputs, outputs and execution conditions")
        elif required.engine or required.modalities or required.output_modalities or required.operations:
            reasons.append("method declares no profile for the requested execution conditions")
        return reasons


def implementation_identity(capability, method: ExecutionMethod) -> str:
    """Content identity of one registered provider/capability/method binding."""
    return hashlib.sha256(json.dumps({"provider": capability.agent_id,
        "capability": capability.id, "method": method.model_dump(mode="json"),
        "runtime_profile": capability.runtime_profile or {}}, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def method_candidates(capability, message_type: str, payload: dict):
    """Return supported methods and explain rejected alternatives without guessing."""
    required = ExecutionRequirements.model_validate(payload.get("execution_requirements") or {})
    selected = payload.get("method_id")
    if selected and required.method_id and selected != required.method_id:
        return [], [{"method_id": selected, "reasons": ["conflicting method selection"]}]
    if selected:
        required = required.model_copy(update={"method_id": selected})
    accepted, rejected = [], []
    for method in capability.methods:
        reasons = method.rejection(message_type, payload, required)
        if reasons:
            rejected.append({"method_id": method.id, "reasons": reasons})
        else:
            accepted.append(method)
    return accepted, rejected


def entry_result_contract(gateway):
    """Read an explicitly bound result contract; never map a role to one kind."""
    result = gateway.get('selected_result_contract')
    if result:
        return ResultContract.model_validate(result).model_dump(mode='json')
    # Only the actual selected provider can supply this contract. A static Role
    # catalog cannot infer installed methods, their version or result authority.
    return {}


def validate_method_binding(agent, message):
    """Validate the accepted method before any tool or external action runs."""
    offer = message.payload.get('accepted_offer')
    if not offer:
        return  # Direct low-level worker messages have no broker commitment.
    specs = [c for c in agent.capabilities if c.id == offer['capability_id']
             and c.agent_id == message.recipient]
    if not specs:
        raise ValueError('Accepted capability is not provided by this executor')
    if not specs[0].methods:
        return
    if (message.payload.get('method_id') != offer.get('method_id')
            or offer.get('provider_id') != message.recipient
            or offer.get('request_id') != message.task_request_id):
        raise ValueError('Assignment method/provider identity differs from accepted offer')
    eligible, rejected = method_candidates(specs[0], message.message_type, message.payload)
    method = next((m for m in eligible if m.id == offer.get('method_id')), None)
    if method is None or method.result.model_dump(mode='json') != offer.get('result_contract'):
        raise ValueError('Assignment no longer satisfies the registered method contract: ' + str(rejected))


def validate_method_result(message, delta):
    """Check owner output obligations, independently of global task completion."""
    offer = message.payload.get('accepted_offer') or {}
    raw = offer.get('result_contract')
    if not raw:
        return
    result = ResultContract.model_validate(raw)
    if message.payload.get('method_id') != offer.get('method_id'):
        raise ValueError('Execution method differs from accepted offer')
    if result.result_channel == 'work_result' and (
            delta.evidence or delta.control_records or delta.messages or delta.task_requests
            or set(delta.updates) - {'observations'}):
        raise ValueError('Scientific providers return results, not authority or peer handoffs')
    if delta.outcome in {'failed', 'blocked'}:
        return
    if result.responsibility == 'research.problem_formulation':
        if not any(row.kind == 'query_spec' for row in delta.control_records):
            raise ValueError('Query method produced no verified QuerySpec control record')
        return
    if any(e.kind == 'execution_failure' and e.method == 'research_repair_attempt'
           and isinstance(e.value, dict) and e.value.get('outcome') in {'blocked', 'failed'} for e in delta.evidence):
        return  # Runtime committed the failed local attempt for Com to assess.
    if result.result_channel == 'work_result':
        from bioma.core.scientific_capabilities import WorkResult
        if set(delta.updates) != {'observations'}:
            raise ValueError('Provider result cannot mutate scientific state')
        rows = delta.updates['observations']
        if not rows:
            raise ValueError('Method produced no work result')
        for row in rows:
            row = row.model_dump(mode='json') if hasattr(row, 'model_dump') else row
            if row.get('kind') != 'work_result':
                raise ValueError('Method must return a non-authoritative work result')
            work = WorkResult.model_validate(row.get('value'))
            if work.family != result.responsibility or work.kind not in result.evidence_kinds:
                raise ValueError('Method returned a result owned by another contract')
        return
    matching = [e for e in delta.evidence if e.kind in result.evidence_kinds]
    if not matching:
        raise ValueError('Method produced none of its contracted result evidence kinds')
    if result.data_kinds and not any(isinstance(e.value, dict) and
            (e.value.get('evidence_package', {}).get('data_kind') or e.value.get('data_kind')) in result.data_kinds
            for e in matching):
        raise ValueError('Method result does not establish its contracted scientific data kind')
    evidence_ids = {e.id for e in matching}
    kinds = {a.kind for a in delta.artifacts if evidence_ids.intersection(a.evidence_ids)}
    if not set(result.artifact_kinds) <= kinds:
        raise ValueError('Method result is missing a contracted evidence-linked artifact')


def assess_capability(self, request, capability):
    """Make this provider's own bounded offer after runtime qualification.

    Resource owners may add domain preconditions. Admission cannot invent a
    scientific method choice or consume scientific budget.
    """
    from bioma.core.community import (
        ExecutionProfile,
        OfferProposal,
        requirements_from,
    )

    required = requirements_from(request.payload)
    reasons = []
    selected = None
    method = None
    if capability.methods:
        from bioma.core.methods import method_candidates
        candidates, rejected = method_candidates(capability, request.message_type, request.payload)
        registry = getattr(self, "tool_registry", None)
        builtin_tools = getattr(self, 'runtime_tool_names', set())
        executable = []
        for candidate in candidates:
            absent = [name for name in candidate.required_tools
                      if name not in builtin_tools and (registry is None or registry.get(name) is None)]
            if absent:
                rejected.append({'method_id': candidate.id, 'reasons': ['Required method tools unavailable: ' + ', '.join(absent)]})
                continue
            executable.append(candidate)
        if len(executable) == 1:
            method = executable[0]
            selected = next((p.id for p in method.profiles if not p.rejection(required)), None)
        elif len(executable) > 1:
            reasons.append("Multiple applicable methods require explicit selection: "
                           + ", ".join(sorted(candidate.id for candidate in executable)))
        if method is None:
            reasons.extend(f"{r['method_id']}: {'; '.join(r['reasons'])}" for r in rejected)
            if not reasons:
                reasons.append("No executable method is registered for this responsibility")
    elif required.method_id or request.payload.get('method_id'):
        reasons.append("Provider does not declare the requested method")
    elif capability.execution_profiles:
        for raw in capability.execution_profiles:
            profile = ExecutionProfile.model_validate(raw)
            errors = profile.rejection(required)
            if not errors:
                selected = profile.id
                break
            reasons.extend(errors)
        if selected:
            reasons = []
    elif (
        required.engine
        or required.modalities
        or required.output_modalities
        or required.operations
    ):
        reasons.append(
            "No execution profile proves the requested execution conditions"
        )
    registry = getattr(self, "tool_registry", None)
    missing = [
        name
        for name in required.required_tools
        if name not in getattr(self, 'runtime_tool_names', set())
        and (registry is None or registry.get(name) is None)
    ]
    if missing:
        reasons.append("Required tools unavailable: " + ", ".join(missing))
    return OfferProposal(
        request_id=request.id,
        provider_id=capability.agent_id,
        capability_id=capability.id,
        accepted=not reasons,
        profile_id=selected,
        method_id=method.id if method else None,
        result_contract=method.result.model_dump(mode='json') if method else {},
        reason="; ".join(dict.fromkeys(reasons))
        or "Provider accepts this input and execution contract.",
        verification_obligations=list(method.result.verification_obligations) if method else [capability.verification_strategy],
    )
