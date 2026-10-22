"""Controller-neutral qualification snapshot; never an execution permit."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from bioma.core.community import ExecutionRequirements
from bioma.core.methods import ResultContract
from bioma.core.models import StrictModel, TaskRequest


class QualifiedCapabilityBinding(StrictModel):
    capability_family: str
    provider_id: str
    method_id: str
    profile_id: str | None = None
    result_contract: ResultContract
    execution_requirements: ExecutionRequirements
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    implementation_identity: str
    request: TaskRequest
    qualification: tuple[dict[str, Any], ...] = ()
    qualification_ref: str = Field(min_length=1)

    def content_reference(self) -> str:
        """Integrity/provenance reference, not a signature or authorization."""
        content = self.model_dump(mode='json', exclude={'qualification_ref', 'qualification'})
        return 'qualification:' + hashlib.sha256(json.dumps(
            content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @classmethod
    def from_qualification(cls, capability, method, request, offer, qualification):
        from bioma.core.methods import implementation_identity
        from bioma.core.scientific_capabilities import WorkResult
        if method.result.result_channel != 'work_result':
            raise ValueError('Scientific providers return non-authoritative WorkResult')
        binding = cls(capability_family=capability.family, provider_id=capability.agent_id,
            method_id=method.id, profile_id=offer.profile_id, result_contract=method.result,
            execution_requirements=ExecutionRequirements.model_validate(request.payload['execution_requirements']),
            input_schema=method.input_schema, output_schema=WorkResult.model_json_schema(),
            implementation_identity=implementation_identity(capability, method),
            request=request.model_copy(deep=True), qualification=qualification, qualification_ref='pending')
        binding.qualification_ref = binding.content_reference()
        return binding.model_copy(deep=True)


# Immutable recorded implementation bindings, not a planning or execution authority.
Reference = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class GroundedStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: Reference
    capability_id: Reference
    method_id: Reference
    provider_id: Reference
    implementation_identity: Reference
    implementation_version: str | None = None
    version_provenance: Literal["declared", "missing"] = "missing"
    message_type: Reference
    input_bindings_json: str
    result_contract_json: str
    required_resources: tuple[Reference, ...] = ()
    effect_class: str = "none"
    verification_strategy: Reference
    estimated_cost: float = Field(ge=0)
    estimated_latency_seconds: float = Field(ge=0)
    qualification_provenance_json: str
    depends_on_step_ids: tuple[Reference, ...] = ()
    capability_binding: QualifiedCapabilityBinding | None = None

    @model_serializer(mode='wrap')
    def preserve_historical_identity(self, handler):
        value = handler(self)
        if self.capability_binding is None:
            value.pop('capability_binding', None)
        return value


class GroundedOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["grounded-option-v1"] = "grounded-option-v1"
    option_identity: Reference
    contract_ref: Reference
    contract_version: int = Field(ge=1)
    plan_ref: Reference
    plan_version: int = Field(ge=1)
    plan_identity_hash: Reference
    runtime_fingerprint: Reference
    steps: tuple[GroundedStep, ...] = Field(min_length=1)

    @property
    def identity(self):
        return "grounding:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()
