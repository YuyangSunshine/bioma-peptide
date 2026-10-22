"""Immutable, versioned control snapshots, never a scientific truth store."""
import hashlib
import json
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .relations import EnablesClause, FrozenModel, Reference, RequiresClause


class NodeKind(str, Enum):
    QUESTION = "QUESTION"
    HYPOTHESIS = "HYPOTHESIS"
    WORK_ITEM = "WORK_ITEM"
    CHECKPOINT = "CHECKPOINT"


class Lifecycle(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    SUPERSEDED = "SUPERSEDED"


class PlanNodeV1(FrozenModel):
    node_id: Reference
    kind: NodeKind
    description: str
    contract_obligation_refs: tuple[Reference, ...] = ()
    lifecycle: Lifecycle = Lifecycle.PENDING

    @model_validator(mode="after")
    def canonical_obligations(self):
        refs = self.contract_obligation_refs
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate obligation reference")
        object.__setattr__(self, "contract_obligation_refs", tuple(sorted(refs)))
        return self


Dependency = Annotated[RequiresClause | EnablesClause, Field(discriminator="relation")]


class ResearchPlanV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    plan_id: Reference
    version: int = Field(ge=1, strict=True)
    contract_ref: Reference
    evidence_snapshot_ref: Reference
    nodes: tuple[PlanNodeV1, ...] = ()
    dependencies: tuple[Dependency, ...] = ()

    @model_validator(mode="after")
    def validate_graph(self):
        ids = {node.node_id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("duplicate node reference")
        seen = set()
        successors = {node: set() for node in ids}
        for clause in self.dependencies:
            if set(clause.sources) - ids:
                raise ValueError("unknown source node reference")
            if isinstance(clause, RequiresClause):
                if clause.target not in ids:
                    raise ValueError("unknown target node reference")
                if clause.target in clause.sources:
                    raise ValueError("self dependency")
                for source in clause.sources:
                    successors[source].add(clause.target)
            key = clause.model_dump_json()
            if key in seen:
                raise ValueError("duplicate dependency clause")
            seen.add(key)
        # Reject all structural REQUIRES cycles, even with an alternative exit.
        indegree = dict.fromkeys(ids, 0)
        for targets in successors.values():
            for target in targets:
                indegree[target] += 1
        frontier = [node for node in ids if not indegree[node]]
        visited = 0
        while frontier:
            source = frontier.pop()
            visited += 1
            for target in successors[source]:
                indegree[target] -= 1
                if not indegree[target]:
                    frontier.append(target)
        if visited != len(ids):
            raise ValueError("dependency cycle")
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda n: n.node_id)))
        object.__setattr__(self, "dependencies", tuple(sorted(
            self.dependencies, key=lambda d: d.model_dump_json()
        )))
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def version_identity(self) -> tuple[str, int, str]:
        """Content-bound identity; no registry or revision authority is implied."""
        digest = hashlib.sha256(self.canonical_json().encode()).hexdigest()
        return self.plan_id, self.version, digest


class MethodBinding(FrozenModel):
    binding_id: Reference
    capability: Reference
    method_id: Reference | None = None
    provider_id: Reference | None = None
    message_type: Reference = "task.submitted"
    engine: Reference | None = None
    # Canonical JSON keeps arbitrary execution parameters deeply immutable.
    execution_requirements_json: str = "{}"

    @model_validator(mode="after")
    def canonical_parameters(self):
        value = json.loads(self.execution_requirements_json)
        if not isinstance(value, dict):
            raise ValueError("execution requirements must be a JSON object")
        for key, actual in (('engine', self.engine), ('method_id', self.method_id), ('responsibility', self.capability)):
            if value.get(key) is not None and value[key] != actual:
                raise ValueError('binding parameters disagree with ' + key)
        object.__setattr__(self, "execution_requirements_json", json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return self


class PlanNode(PlanNodeV1):
    supersedes_node_ref: Reference | None = None
    question_refs: tuple[Reference, ...] = ()
    evidence_gap_refs: tuple[Reference, ...] = ()
    candidate_bindings: tuple[MethodBinding, ...] = ()
    selected_binding_id: Reference | None = None
    repair_enabled: bool = True

    @model_validator(mode="after")
    def valid_bindings(self):
        ids = [binding.binding_id for binding in self.candidate_bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate binding id")
        if self.selected_binding_id is not None and self.selected_binding_id not in ids:
            raise ValueError("unknown selected binding")
        object.__setattr__(self, "candidate_bindings", tuple(sorted(
            self.candidate_bindings, key=lambda b: b.binding_id)))
        for field in ("question_refs", "evidence_gap_refs"):
            value = getattr(self, field)
            if len(set(value)) != len(value):
                raise ValueError("duplicate work reference")
            object.__setattr__(self, field, tuple(sorted(value)))
        return self


class ResearchPlan(ResearchPlanV1):
    schema_version: Literal["2"] = "2"
    nodes: tuple[PlanNode, ...] = ()
    entry_binding: MethodBinding | None = None
    max_repairs_per_requirement: int = Field(default=2, ge=0, le=10)
    max_total_repairs: int = Field(default=8, ge=0, le=50)


def read_plan(raw) -> ResearchPlan | ResearchPlanV1:
    """Read each schema as written; never synthesize historical strategy."""
    if raw.get("schema_version") == "1":
        return ResearchPlanV1.model_validate(raw)
    return ResearchPlan.model_validate(raw)
