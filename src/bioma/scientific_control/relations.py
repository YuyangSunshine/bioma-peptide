"""Control and acceptance relations; WP2 alone owns evidence relations."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Reference = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _SourceClause(FrozenModel):
    sources: tuple[Reference, ...] = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def canonical_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate source reference")
        return tuple(sorted(value))


class RequiresClause(_SourceClause):
    """Sources are AND; clauses sharing a target are OR. Readiness only."""

    relation: Literal["REQUIRES"] = "REQUIRES"
    target: Reference


class EnablesClause(_SourceClause):
    """AND sources enable a named condition, not a node or evidence relation.

    Condition references occupy a separate namespace and cannot be used as
    prerequisite sources. Alternative clauses for one condition are OR.
    """

    relation: Literal["ENABLES"] = "ENABLES"
    condition_ref: Reference


class SatisfactionLink(FrozenModel):
    """A link is not acceptance; its exact result must be explicitly accepted."""

    relation: Literal["SATISFIES"] = "SATISFIES"
    accepted_result_ref: Reference
    contract_obligation_ref: Reference
