"""Minimal scientific decision schema and predictor protocol.

These are non-authoritative interfaces: they neither select nor execute an
action, and they cannot qualify or commit evidence.
"""
from __future__ import annotations

import json
from enum import Enum, IntEnum
from typing import Hashable, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

Reference = str


class _FrozenCoreModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScientificDecisionType(str, Enum):
    IDENTIFICATION = "IDENTIFICATION"
    ACCEPTANCE = "ACCEPTANCE"
    CONSTRAINED_SELECTION = "CONSTRAINED_SELECTION"
    ESTIMATION = "ESTIMATION"
    OPEN_ENDED_RESEARCH = "OPEN_ENDED_RESEARCH"
    DELIVERY_ONLY = "DELIVERY_ONLY"
    UNDERSPECIFIED = "UNDERSPECIFIED"


class ScientificDecisionSpec(_FrozenCoreModel):
    schema_version: Literal["scientific-decision-spec-v1"] = "scientific-decision-spec-v1"
    decision_type: ScientificDecisionType
    latent_target: str | None = None
    decision_space: tuple[Reference, ...] = ()
    loss_semantics: Literal["zero_one", "acceptance", "constrained", "estimation"] | None = None
    acceptance_predicate_json: str = "[]"
    objective_json: str = "{}"
    constraints_json: str = "[]"
    abstention_allowed: bool = False
    source_provenance: tuple[str, ...] = Field(min_length=1)
    compiler_status: Literal["RESOLVED", "DECISION_SPEC_UNRESOLVED"]
    unresolved_reason: str = ""

    @model_validator(mode="after")
    def coherent(self):
        for field, expected in (("acceptance_predicate_json", list),
                                ("objective_json", dict), ("constraints_json", list)):
            value = json.loads(getattr(self, field))
            if not isinstance(value, expected):
                raise ValueError(field + " has the wrong JSON shape")
            object.__setattr__(self, field, json.dumps(value, sort_keys=True,
                                                       separators=(",", ":"), allow_nan=False))
        object.__setattr__(self, "decision_space", tuple(sorted(set(self.decision_space))))
        object.__setattr__(self, "source_provenance", tuple(sorted(set(self.source_provenance))))
        resolved = self.compiler_status == "RESOLVED"
        if resolved and (not self.latent_target or not self.decision_space or
                         not self.loss_semantics or self.unresolved_reason):
            raise ValueError("resolved decision spec lacks decision semantics")
        if not resolved and (self.loss_semantics is not None or not self.unresolved_reason):
            raise ValueError("unresolved decision spec must fail closed")
        return self


class PredictorCapability(IntEnum):
    L0_OUTCOME_ONLY = 0
    L1_HYPOTHESIS_CONDITIONED = 1
    L2_INTERVENTIONAL = 2


class ScientificPredictor(Protocol):
    predictor_id: str
    capability: PredictorCapability

    def predict_evidence(self, *, hypothesis: Hashable, grounded_action,
                         biological_context: Mapping[str, object],
                         evidence_schema: Mapping[str, object]) -> Mapping[Hashable, float]: ...
