"""Immutable decision snapshots and non-evidentiary outcome forecasts."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from bioma.core.research import fingerprint
from bioma.core.research_contract import ResearchContract
from .grounding import GroundedOption, runtime_fingerprint
from .plan import Lifecycle, ResearchPlan
from .progress import ObligationProgress, contract_progress
from .relations import FrozenModel, Reference
from .semantics import derive_readiness


class ForecastSource(str, Enum):
    ORACLE = "ORACLE"
    EMPIRICAL = "EMPIRICAL"
    MODEL = "MODEL"
    RULE = "RULE"


class RuntimeOutcome(str, Enum):
    QUALIFIED_POSITIVE = "QUALIFIED_POSITIVE"
    QUALIFIED_NEGATIVE = "QUALIFIED_NEGATIVE"
    INCONCLUSIVE = "INCONCLUSIVE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"


class ProjectedStatus(str, Enum):
    SATISFIED = "SATISFIED"
    RESOLVED = "RESOLVED"
    UNCHANGED = "UNCHANGED"
    BLOCKED = "BLOCKED"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTED = "CONFLICTED"


class DecisionSnapshot(FrozenModel):
    schema_version: Literal["decision-snapshot-v1"] = "decision-snapshot-v1"
    contract_ref: Reference
    contract_version: int = Field(ge=1)
    plan_ref: Reference
    plan_version: int = Field(ge=1)
    plan_identity_hash: Reference
    committed_state_ref: Reference
    committed_state_version: int = Field(ge=0)
    runtime_fingerprint: Reference
    unresolved_effect_refs: tuple[Reference, ...] = ()
    hard_constraint_refs: tuple[Reference, ...] = ()
    violated_constraint_refs: tuple[Reference, ...] = ()
    remaining_budget: float | None = Field(default=None, ge=0)
    available_resources: tuple[Reference, ...] = ()
    ready_plan_node_refs: tuple[Reference, ...] = ()
    blocked_plan_node_refs: tuple[Reference, ...] = ()
    obligation_progress: tuple[ObligationProgress, ...]
    progress_value: float = Field(ge=0, le=1)
    timestamp: datetime
    provenance_refs: tuple[Reference, ...] = ()

    @model_validator(mode="after")
    def canonical_snapshot(self):
        for field in ("unresolved_effect_refs", "hard_constraint_refs", "violated_constraint_refs",
                      "available_resources", "ready_plan_node_refs",
                      "blocked_plan_node_refs", "provenance_refs"):
            object.__setattr__(self, field, tuple(sorted(set(getattr(self, field)))))
        object.__setattr__(self, "obligation_progress", tuple(sorted(
            self.obligation_progress, key=lambda row: row.obligation_ref)))
        return self

    @property
    def identity(self) -> str:
        return "snapshot:" + hashlib.sha256(self.model_dump_json(exclude_none=True).encode()).hexdigest()


def decision_snapshot(*, contract: ResearchContract, plan: ResearchPlan, state,
                      registry, active_agents: set[str], available_resources: set[str] | None,
                      unresolved_effect_refs=(), remaining_budget=None,
                      timestamp: datetime, provenance_refs=()) -> DecisionSnapshot:
    if plan.contract_ref != contract.identity:
        raise ValueError("plan and contract identity differ")
    progress = contract_progress(contract, state)
    credited = {row.obligation_ref for row in progress.obligations if row.progress_credit}
    completed_nodes = {node.node_id for node in plan.nodes
        if node.contract_obligation_refs and set(node.contract_obligation_refs) <= credited}
    readiness = derive_readiness(plan, completed_nodes)
    blocked_nodes = {node.node_id for node in plan.nodes if node.lifecycle is Lifecycle.BLOCKED}
    hard_constraints = json.loads(contract.hard_constraints_json)
    hard_refs = tuple("contract-constraint:" + fingerprint(item) for item in hard_constraints)
    violated = tuple(item.id for item in state.constraints
        if item.severity in {"error", "fatal"} and item.satisfied is not True)
    committed = fingerprint({"version": state.version,
        "evidence": [item.model_dump(mode="json") for item in state.evidence],
        "artifacts": [item.model_dump(mode="json") for item in state.artifacts],
        "constraints": [item.model_dump(mode="json") for item in state.constraints]})
    return DecisionSnapshot(contract_ref=contract.identity, contract_version=contract.version,
        plan_ref=plan.plan_id, plan_version=plan.version,
        plan_identity_hash=plan.version_identity[2], committed_state_ref=committed,
        committed_state_version=state.version,
        runtime_fingerprint=runtime_fingerprint(registry, active_agents, available_resources),
        unresolved_effect_refs=tuple(unresolved_effect_refs), hard_constraint_refs=hard_refs,
        violated_constraint_refs=violated, remaining_budget=remaining_budget,
        available_resources=tuple(available_resources or ()),
        ready_plan_node_refs=tuple(readiness.ready), blocked_plan_node_refs=tuple(blocked_nodes),
        obligation_progress=progress.obligations, progress_value=progress.value,
        timestamp=timestamp, provenance_refs=tuple(provenance_refs))


def decision_snapshot_is_current(snapshot: DecisionSnapshot, *, contract, plan, state,
                                 registry, active_agents, available_resources,
                                 unresolved_effect_refs=(), remaining_budget=None) -> bool:
    current = decision_snapshot(contract=contract, plan=plan, state=state,
        registry=registry, active_agents=active_agents,
        available_resources=available_resources,
        unresolved_effect_refs=unresolved_effect_refs, remaining_budget=remaining_budget,
        timestamp=snapshot.timestamp, provenance_refs=snapshot.provenance_refs)
    return current.identity == snapshot.identity


class ProjectedObligationEffect(FrozenModel):
    obligation_ref: Reference
    projected_status: ProjectedStatus
    scientific_outcome: str | None = None
    rationale: str = Field(min_length=1)


class ForecastBranch(FrozenModel):
    branch_id: Reference
    probability: float = Field(ge=0, le=1)
    outcome_class: RuntimeOutcome | str
    obligation_effects: tuple[ProjectedObligationEffect, ...] = ()
    enables_plan_node_refs: tuple[Reference, ...] = ()
    blocks_plan_node_refs: tuple[Reference, ...] = ()
    consumed_resources: tuple[Reference, ...] = ()
    cost: float = Field(default=0, ge=0)
    latency_seconds: float = Field(default=0, ge=0)
    risk: Literal["low", "medium", "high"] = "low"
    operationally_feasible: bool = True

    @model_validator(mode="after")
    def finite_and_canonical(self):
        for value in (self.probability, self.cost, self.latency_seconds):
            if not math.isfinite(value):
                raise ValueError("forecast numeric values must be finite")
        for field in ("enables_plan_node_refs", "blocks_plan_node_refs", "consumed_resources"):
            object.__setattr__(self, field, tuple(sorted(set(getattr(self, field)))))
        refs = [effect.obligation_ref for effect in self.obligation_effects]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate projected obligation effect")
        object.__setattr__(self, "obligation_effects", tuple(sorted(
            self.obligation_effects, key=lambda effect: effect.obligation_ref)))
        return self


HypotheticalOutcome = ForecastBranch


class OutcomeForecast(FrozenModel):
    schema_version: Literal["outcome-forecast-v1"] = "outcome-forecast-v1"
    forecast_id: Reference
    grounded_option_identity: Reference
    decision_snapshot_identity: Reference
    source: ForecastSource
    method: Reference
    method_version: str
    status: Literal["USABLE", "FORECAST_UNAVAILABLE"] = "USABLE"
    unavailable_reason: str = ""
    scientific_outcome_known: bool = False
    expected_deliverable_classes: tuple[Reference, ...] = ()
    covered_obligation_refs: tuple[Reference, ...] = ()
    requires_enabled_plan_node_refs: tuple[Reference, ...] = ()
    branches: tuple[ForecastBranch, ...] = ()
    provenance_refs: tuple[Reference, ...] = ()

    @model_validator(mode="after")
    def valid_distribution(self):
        object.__setattr__(self, "expected_deliverable_classes", tuple(sorted(
            set(self.expected_deliverable_classes))))
        object.__setattr__(self, "covered_obligation_refs", tuple(sorted(
            set(self.covered_obligation_refs))))
        object.__setattr__(self, "requires_enabled_plan_node_refs", tuple(sorted(
            set(self.requires_enabled_plan_node_refs))))
        if self.status == "FORECAST_UNAVAILABLE":
            if self.branches or not self.unavailable_reason:
                raise ValueError("unavailable forecast needs a reason and no branches")
            return self
        if not self.branches:
            raise ValueError("usable forecast requires finite outcome branches")
        ids = [branch.branch_id for branch in self.branches]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate forecast branch")
        total = math.fsum(branch.probability for branch in self.branches)
        if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("forecast probabilities must form a normalized distribution")
        object.__setattr__(self, "branches", tuple(sorted(self.branches, key=lambda b: b.branch_id)))
        return self

    @property
    def identity(self) -> str:
        return "forecast:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @property
    def expected_cost(self) -> float | None:
        return None if self.status != "USABLE" else math.fsum(
            branch.probability * branch.cost for branch in self.branches)

    @property
    def expected_latency_seconds(self) -> float | None:
        return None if self.status != "USABLE" else math.fsum(
            branch.probability * branch.latency_seconds for branch in self.branches)


class ForecastStaleError(ValueError):
    pass


def validate_forecast(forecast: OutcomeForecast, snapshot: DecisionSnapshot,
                      grounded: GroundedOption) -> OutcomeForecast:
    if forecast.status != "USABLE":
        raise ValueError("FORECAST_UNAVAILABLE: " + forecast.unavailable_reason)
    if forecast.decision_snapshot_identity != snapshot.identity:
        raise ForecastStaleError("forecast binds a stale DecisionSnapshot")
    if forecast.grounded_option_identity != grounded.identity:
        raise ForecastStaleError("forecast binds a different or stale GroundedOption")
    return forecast


class HypotheticalControlState(FrozenModel):
    schema_version: Literal["hypothetical-control-v1"] = "hypothetical-control-v1"
    root_snapshot_identity: Reference
    obligation_progress: tuple[ObligationProgress, ...]
    ready_plan_node_refs: tuple[Reference, ...] = ()
    blocked_plan_node_refs: tuple[Reference, ...] = ()
    remaining_budget: float | None = Field(default=None, ge=0)
    available_resources: tuple[Reference, ...] = ()
    unresolved_effect_refs: tuple[Reference, ...] = ()
    operationally_feasible: bool = True
    applied_branch_refs: tuple[Reference, ...] = ()

    @property
    def progress_value(self):
        terminal = [row for row in self.obligation_progress if row.mandatory and row.terminal]
        return sum(row.progress_credit for row in terminal) / len(terminal) if terminal else 0.0


def hypothetical_state(snapshot: DecisionSnapshot) -> HypotheticalControlState:
    return HypotheticalControlState(root_snapshot_identity=snapshot.identity,
        obligation_progress=snapshot.obligation_progress,
        ready_plan_node_refs=snapshot.ready_plan_node_refs,
        blocked_plan_node_refs=snapshot.blocked_plan_node_refs,
        remaining_budget=snapshot.remaining_budget,
        available_resources=snapshot.available_resources,
        unresolved_effect_refs=snapshot.unresolved_effect_refs,
        operationally_feasible=not bool(snapshot.unresolved_effect_refs))


def hypothetical_transition(snapshot: DecisionSnapshot, grounded: GroundedOption,
                            branch: ForecastBranch, *, state=None) -> HypotheticalControlState:
    """Pure hat_T. It cannot call runtime, ledger, WP1, or create Evidence."""
    current = state or hypothetical_state(snapshot)
    if current.root_snapshot_identity != snapshot.identity:
        raise ForecastStaleError("hypothetical state belongs to another snapshot")
    effects = {effect.obligation_ref: effect for effect in branch.obligation_effects}
    projected = []
    for row in current.obligation_progress:
        effect = effects.get(row.obligation_ref)
        if effect is None or effect.projected_status is ProjectedStatus.UNCHANGED:
            projected.append(row)
            continue
        if row.role.value == "QUESTION":
            credit = (effect.projected_status is ProjectedStatus.RESOLVED and
                branch.outcome_class in {RuntimeOutcome.QUALIFIED_POSITIVE,
                                         RuntimeOutcome.QUALIFIED_NEGATIVE})
        elif row.role.value in {"ACHIEVEMENT", "DELIVERY"}:
            noncrediting = {RuntimeOutcome.QUALIFIED_NEGATIVE, RuntimeOutcome.INCONCLUSIVE,
                            RuntimeOutcome.EXECUTION_FAILURE, RuntimeOutcome.EFFECT_UNKNOWN}
            credit = (effect.projected_status is ProjectedStatus.SATISFIED and
                      branch.outcome_class not in noncrediting)
        else:
            credit = effect.projected_status in {ProjectedStatus.SATISFIED, ProjectedStatus.RESOLVED}
        projected.append(row.model_copy(update={"status": effect.projected_status.value,
            "progress_credit": credit, "acceptance_status": "hypothetical",
            "scientific_outcome": effect.scientific_outcome,
            "rationale": effect.rationale, "evidence_refs": (), "artifact_refs": ()}))
    resources = set(current.available_resources) - set(branch.consumed_resources)
    remaining = current.remaining_budget
    if remaining is not None:
        remaining = max(0.0, remaining - branch.cost)
    unknown = branch.outcome_class == RuntimeOutcome.EFFECT_UNKNOWN
    unresolved = current.unresolved_effect_refs + ((
        f"hypothetical-effect:{grounded.identity}:{branch.branch_id}",) if unknown else ())
    ready = (set(current.ready_plan_node_refs) | set(branch.enables_plan_node_refs)) - set(branch.blocks_plan_node_refs)
    blocked = set(current.blocked_plan_node_refs) | set(branch.blocks_plan_node_refs)
    return HypotheticalControlState(root_snapshot_identity=snapshot.identity,
        obligation_progress=tuple(projected), ready_plan_node_refs=tuple(ready),
        blocked_plan_node_refs=tuple(blocked), remaining_budget=remaining,
        available_resources=tuple(resources), unresolved_effect_refs=tuple(unresolved),
        operationally_feasible=current.operationally_feasible and branch.operationally_feasible
            and not unknown and branch.outcome_class != RuntimeOutcome.EXECUTION_FAILURE,
        applied_branch_refs=(*current.applied_branch_refs, branch.branch_id))


# Mathematical notation used by the architecture document.
hat_T = hypothetical_transition
