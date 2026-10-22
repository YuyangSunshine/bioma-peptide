"""Scientific strategy candidates, separate from evidence and execution binding."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Protocol

from pydantic import Field, model_serializer, model_validator

from .relations import FrozenModel, Reference


class OptionStep(FrozenModel):
    step_id: Reference
    description: str = Field(min_length=1)
    capability: Reference
    message_type: Reference
    input_bindings_json: str = "{}"
    required_input_classes: tuple[Reference, ...] = ()
    expected_result_classes: tuple[Reference, ...] = Field(min_length=1)
    expected_artifact_classes: tuple[Reference, ...] = ()
    depends_on_step_ids: tuple[Reference, ...] = ()
    required_resources: tuple[Reference, ...] = ()
    method_hint: Reference | None = None

    @model_validator(mode="after")
    def canonical_step(self):
        bindings = json.loads(self.input_bindings_json)
        if not isinstance(bindings, dict):
            raise ValueError("input bindings must be a JSON object")
        object.__setattr__(self, "input_bindings_json", json.dumps(
            bindings, sort_keys=True, separators=(",", ":"), allow_nan=False))
        for field in ("required_input_classes", "expected_result_classes",
                      "expected_artifact_classes", "depends_on_step_ids", "required_resources"):
            values = getattr(self, field)
            if len(values) != len(set(values)):
                raise ValueError("duplicate option step reference")
            object.__setattr__(self, field, tuple(sorted(values)))
        return self


class OptionIntent(str, Enum):
    DIRECT_PROGRESS = "DIRECT_PROGRESS"
    EVIDENCE_ACQUISITION = "EVIDENCE_ACQUISITION"
    EVIDENCE_TRANSFORMATION = "EVIDENCE_TRANSFORMATION"
    SYSTEM_PROBE = "SYSTEM_PROBE"
    FALSIFICATION = "FALSIFICATION"
    PREPARATION = "PREPARATION"


class ScientificOption(FrozenModel):
    """A bounded scientific intent; it grants no execution or truth authority."""
    schema_version: Literal["scientific-option-v1"] = "scientific-option-v1"
    version: Literal[1] = 1
    option_id: Reference
    contract_ref: Reference
    contract_version: int = Field(ge=1, strict=True)
    plan_ref: Reference
    plan_version: int = Field(ge=1, strict=True)
    plan_identity_hash: Reference
    target_plan_node_refs: tuple[Reference, ...] = Field(min_length=1)
    target_contract_obligation_refs: tuple[Reference, ...] = Field(min_length=1)
    scientific_intent: str = Field(min_length=1)
    initiation_conditions: tuple[str, ...] = ()
    expected_result_classes: tuple[Reference, ...] = Field(min_length=1)
    required_evidence_classes: tuple[Reference, ...] = ()
    required_artifact_classes: tuple[Reference, ...] = ()
    budget: float | None = Field(default=None, ge=0)
    required_resources: tuple[Reference, ...] = ()
    termination_condition: str = Field(min_length=1)
    proposer_id: Reference
    provenance_refs: tuple[Reference, ...] = Field(min_length=1)
    steps: tuple[OptionStep, ...] = Field(min_length=1)
    source_snapshot_identity: Reference | None = None
    intent_class: OptionIntent | None = None
    rationale: str | None = None
    duplicate_justification: str | None = None

    @model_validator(mode="after")
    def valid_bounded_dag(self):
        for field in ("target_plan_node_refs", "target_contract_obligation_refs",
                      "expected_result_classes", "required_evidence_classes",
                      "required_artifact_classes", "required_resources", "provenance_refs"):
            values = getattr(self, field)
            if len(values) != len(set(values)):
                raise ValueError("duplicate option reference")
            object.__setattr__(self, field, tuple(sorted(values)))
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate option step")
        position = {step_id: index for index, step_id in enumerate(ids)}
        for index, step in enumerate(self.steps):
            if any(parent not in position for parent in step.depends_on_step_ids):
                raise ValueError("unknown option step dependency")
            if any(position[parent] >= index for parent in step.depends_on_step_ids):
                raise ValueError("option steps must form an explicit forward acyclic chain")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_legacy_proposal_fields(self, handler):
        data = handler(self)
        for field in ("source_snapshot_identity", "intent_class", "rationale",
                      "duplicate_justification"):
            if getattr(self, field) is None:
                data.pop(field, None)
        return data

    @property
    def identity(self) -> str:
        return "option:" + hashlib.sha256(self.model_dump_json(exclude_none=True).encode()).hexdigest()


def build_option(**values) -> ScientificOption:
    """Construction seam for role, heuristic, or future model proposers."""
    return ScientificOption.model_validate(values)
