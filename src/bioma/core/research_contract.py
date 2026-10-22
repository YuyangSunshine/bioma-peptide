"""Stable v2 research obligations and explicit, caller-authorized amendments.

No execution strategy lives here. JSON payloads are canonical immutable strings;
accessors return fresh values so nested mutation cannot alter contract identity.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class StableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ObligationRole(str, Enum):
    SUPPORTING = "SUPPORTING"
    QUESTION = "QUESTION"
    ACHIEVEMENT = "ACHIEVEMENT"
    DELIVERY = "DELIVERY"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class MethodRestriction(StableModel):
    obligation_ref: str | None = None
    method_id: str | None = None
    engine: str | None = None
    capability: str | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not any((self.method_id, self.engine, self.capability)):
            raise ValueError("empty user method restriction")
        return self


class AcceptanceObligation(StableModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    claim_ids: tuple[str, ...] = ()
    mandatory: bool = True
    mode: Literal["evidence", "claim_assessment", "delivery"] = "evidence"
    evidence_kinds: tuple[str, ...] = ()
    data_kinds: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    artifact_names: tuple[str, ...] = ()
    predicates_json: str = "[]"
    artifact_predicates_json: str = "[]"
    min_count: int = Field(default=1, ge=1)
    input_bindings_json: str = "{}"
    input_hash: str = ""
    # Required scientific consumption/lineage, NOT inferred execution order.
    depends_on: tuple[str, ...] = ()
    delivery_id: str | None = None
    stage: Literal["work", "review", "delivery"] = "work"
    allow_insufficient: bool = False
    semantic_review: bool = True
    source: str = "user_request"
    # Optional only for backward readability of historical v2 snapshots.
    # New contracts materialize this field explicitly.
    role: ObligationRole | None = None

    @model_validator(mode="after")
    def acceptance_schema(self):
        from bioma.core.research import EvidenceRequirement
        for field in ('claim_ids', 'evidence_kinds', 'data_kinds', 'artifact_kinds', 'artifact_names', 'depends_on'):
            value = getattr(self, field)
            if len(set(value)) != len(value):
                raise ValueError('duplicate acceptance reference: ' + field)
            object.__setattr__(self, field, tuple(sorted(value)))
        for field in ("predicates_json", "artifact_predicates_json", "input_bindings_json"):
            object.__setattr__(self, field, canonical(json.loads(getattr(self, field))))
        # Reuse established predicate/data-kind validation without binding a method.
        EvidenceRequirement.model_validate(self.acceptance_payload())
        return self

    @model_serializer(mode="wrap")
    def omit_absent_legacy_role(self, handler):
        data = handler(self)
        if self.role is None:
            data.pop("role", None)
        return data

    @property
    def effective_role(self) -> ObligationRole:
        if self.role is not None:
            return self.role
        if self.mode == "claim_assessment":
            return ObligationRole.QUESTION
        if self.mode == "delivery":
            return ObligationRole.DELIVERY
        return ObligationRole.ACHIEVEMENT

    def acceptance_payload(self):
        data = self.model_dump(mode="json", exclude={
            "predicates_json", "artifact_predicates_json", "input_bindings_json", "role"})
        data.update(predicates=json.loads(self.predicates_json),
                    artifact_predicates=json.loads(self.artifact_predicates_json),
                    input_bindings=json.loads(self.input_bindings_json))
        return data

    @property
    def predicates(self):
        from bioma.core.research import EvidencePredicate
        return tuple(EvidencePredicate.model_validate(p) for p in json.loads(self.predicates_json))

    @property
    def artifact_predicates(self):
        from bioma.core.research import ArtifactPredicate
        return tuple(ArtifactPredicate.model_validate(p) for p in json.loads(self.artifact_predicates_json))

    @property
    def input_bindings(self):
        return json.loads(self.input_bindings_json)


class ResearchContract(StableModel):
    schema_version: Literal["research-contract-v2"] = "research-contract-v2"
    version: int = Field(default=1, ge=1, strict=True)
    previous_contract_ref: str | None = None
    query: str
    input_hash: str
    claims_json: str = "[]"
    requirements: tuple[AcceptanceObligation, ...]
    scope_json: str = "{}"
    hard_constraints_json: str = "[]"
    resource_budget_json: str = "{}"
    execution_constraints_json: str = "{}"
    method_restrictions: tuple[MethodRestriction, ...] = ()
    amendment_authority: tuple[str, ...] = ("user",)

    @model_validator(mode="after")
    def valid_contract(self):
        from bioma.core.research import validate_requirement_graph, EvidenceRequirement
        for field in ("claims_json", "scope_json", "hard_constraints_json",
                      "resource_budget_json", "execution_constraints_json"):
            object.__setattr__(self, field, canonical(json.loads(getattr(self, field))))
        for field in ("scope_json", "resource_budget_json", "execution_constraints_json"):
            if not isinstance(json.loads(getattr(self, field)), dict):
                raise ValueError(field + " must encode an object")
        if not isinstance(json.loads(self.hard_constraints_json), list):
            raise ValueError("hard constraints must encode an array")
        for key, value in json.loads(self.resource_budget_json).items():
            if key in {'max_total_repairs', 'max_repairs_per_requirement'} and (type(value) is not int or value < 0):
                raise ValueError('repair budget must be a nonnegative integer')
        validate_requirement_graph([EvidenceRequirement.model_validate(r.acceptance_payload()) for r in self.requirements], self.claims)
        ids = {r.id for r in self.requirements}
        if any(r.obligation_ref is not None and r.obligation_ref not in ids for r in self.method_restrictions):
            raise ValueError("unknown method restriction obligation")
        if not self.amendment_authority or any(not a for a in self.amendment_authority):
            raise ValueError("contract needs amendment authority")
        object.__setattr__(self, 'amendment_authority', tuple(sorted(set(self.amendment_authority))))
        if (self.version == 1) != (self.previous_contract_ref is None):
            raise ValueError("amended contracts require previous identity")
        object.__setattr__(self, "requirements", tuple(sorted(self.requirements, key=lambda r: r.id)))
        object.__setattr__(self, "method_restrictions", tuple(sorted(
            self.method_restrictions, key=lambda r: r.model_dump_json())))
        return self

    @property
    def claims(self):
        return json.loads(self.claims_json)

    @property
    def identity(self):
        from bioma.core.research import fingerprint
        return fingerprint(self.model_dump(mode="json"))

    def canonical_json(self):
        return canonical(self.model_dump(mode="json"))


class ContractAmendment(StableModel):
    schema_version: Literal['contract-amendment-v1'] = 'contract-amendment-v1'
    amendment_id: str = Field(min_length=1)
    previous_contract_ref: str
    previous_contract_version: int = Field(ge=1)
    authorized_changed_fields: tuple[str, ...]
    changes_json: str
    rationale: str = Field(min_length=1)


def amend_contract(contract: ResearchContract, amendment: ContractAmendment, *, authorized_actor: str):
    """The caller authenticates the actor; an amendment cannot self-authorize."""
    if authorized_actor not in contract.amendment_authority:
        raise ValueError("unauthorized contract amendment")
    if (amendment.previous_contract_ref != contract.identity or
            amendment.previous_contract_version != contract.version):
        raise ValueError("stale contract amendment")
    changes = json.loads(amendment.changes_json)
    allowed = set(ResearchContract.model_fields) - {
        "schema_version", "version", "previous_contract_ref"}
    if (not isinstance(changes, dict) or not changes or set(changes) - allowed or
            set(changes) != set(amendment.authorized_changed_fields) or
            len(set(amendment.authorized_changed_fields)) != len(amendment.authorized_changed_fields)):
        raise ValueError("amendment must name exactly the authorized changed fields")
    return ResearchContract.model_validate({**contract.model_dump(mode="json"), **changes,
        "version": contract.version + 1, "previous_contract_ref": contract.identity})
