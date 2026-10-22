from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupSummary(ComModel):
    group: str
    metric: str
    n: int = Field(ge=1)
    mean: float
    standard_deviation: float | None = Field(default=None, ge=0.0)


class CriterionAssessment(ComModel):
    direction: Literal["increase", "decrease"]
    criterion: str
    threshold: float
    observed_improvement: float
    meets_criteria: bool


class AnalysisResult(ComModel):
    observation_id: str
    metric: str
    candidate_group: str
    control_group: str
    summaries: list[GroupSummary]
    absolute_change: float
    relative_change: float | None = None
    assessment: CriterionAssessment
    warnings: list[str] = Field(default_factory=list)
