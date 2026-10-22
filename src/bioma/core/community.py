"""Scientific role charters and task-specific execution commitments.

Families describe responsibility. Profiles declare supported combinations, never
the Cartesian product of broad domain/modality/engine lists.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionRequirements(Contract):
    responsibility: str | None = Field(default=None,
        description="Required scientific result responsibility, independent of domain, modality or algorithm.")
    method_id: str | None = Field(default=None,
        description="An explicitly selected registered execution method; null when method selection is open.")
    engine: str | None = Field(
        default=None,
        description="Explicit execution language requirement, e.g. R or python; null when the user leaves implementation open.",
    )
    modalities: list[str] = Field(
        default_factory=list,
        description="INPUT modalities only: table, json, text, sequence, structure, image. A requested output plot is NOT an image input. File suffixes are not modalities.",
    )
    output_modalities: list[str] = Field(
        default_factory=list,
        description="Required OUTPUT modalities: table, json, text, image. Plot generation is image output, never image input.",
    )
    operations: list[str] = Field(
        default_factory=list,
        description="Only hard operation restrictions, using advertised profile operation identifiers. Specific scientific methods belong to the research request, not invented operation IDs.",
    )
    required_tools: list[str] = Field(default_factory=list)


class ExecutionProfile(Contract):
    id: str
    engine: str | None = None
    modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    available: bool = True
    unavailable_reason: str = ""
    limitations: list[str] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)

    def rejection(self, required: ExecutionRequirements) -> list[str]:
        reasons = []
        if not self.available:
            reasons.append(self.unavailable_reason or "execution profile unavailable")
        if (
            required.engine
            and required.engine.casefold() != (self.engine or "").casefold()
        ):
            reasons.append(
                "requested execution engine is not supported by this profile"
            )
        if not set(required.modalities) <= set(self.modalities):
            reasons.append("input modalities are not supported by this profile")
        if not set(required.output_modalities) <= set(self.output_modalities):
            reasons.append("output modalities are not supported by this profile")
        if not set(required.operations) <= set(self.operations):
            reasons.append("requested operations are not supported by this profile")
        return reasons


class OfferProposal(Contract):
    request_id: str
    provider_id: str
    capability_id: str
    accepted: bool
    profile_id: str | None = None
    method_id: str | None = None
    result_contract: dict[str, Any] = Field(default_factory=dict)
    reason: str
    assumptions: list[str] = Field(default_factory=list)
    verification_obligations: list[str] = Field(default_factory=list)


class AgentFailure(Contract):
    code: str
    category: Literal[
        "input", "dependency", "infrastructure", "execution", "validation"
    ]
    message: str
    retryable: bool = False
    checkpoint_ref: str | None = None
    diagnostic_refs: list[str] = Field(default_factory=list)


def requirements_from(payload: dict) -> ExecutionRequirements:
    return ExecutionRequirements.model_validate(
        payload.get("execution_requirements") or {}
    )


def merge_execution_requirements(*requirements):
    """Conjoin restrictions; no downstream caller may silently erase a requirement."""
    result = ExecutionRequirements().model_dump(mode='json')
    for raw in requirements:
        current = ExecutionRequirements.model_validate(raw).model_dump(mode='json', exclude_defaults=True)
        for key, value in current.items():
            if isinstance(value, list):
                result[key] = list(dict.fromkeys([*result[key], *value]))
            elif result[key] and result[key].casefold() != value.casefold():
                raise ValueError('Conflicting execution requirement: ' + key)
            else:
                result[key] = value
    return result
