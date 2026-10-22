"""Bounded exact finite-horizon control over caller-supplied grounded options."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .forecast import (DecisionSnapshot, HypotheticalControlState, OutcomeForecast,
                       RuntimeOutcome, hypothetical_state, hypothetical_transition,
                       validate_forecast)
from .grounding import GroundedOption
from .relations import FrozenModel, Reference


RISK_VALUE = {"low": 0, "medium": 1, "high": 2}


class SelectionBaseline(str, Enum):
    D0_LLM = "D0_LLM"
    GOAL_RELEVANCE = "GOAL_RELEVANCE"
    STRUCTURAL_SHAPLEY = "STRUCTURAL_SHAPLEY"
    ONE_STEP_FORECAST = "ONE_STEP_FORECAST"
    FINITE_HORIZON = "FINITE_HORIZON"


class ControllerPolicy(FrozenModel):
    max_risk: Literal["low", "medium", "high"] = "low"
    forbidden_grounding_refs: tuple[Reference, ...] = ()
    required_hard_constraint_refs: tuple[Reference, ...] = ()

    @model_validator(mode="after")
    def canonical(self):
        object.__setattr__(self, "forbidden_grounding_refs", tuple(sorted(
            set(self.forbidden_grounding_refs))))
        object.__setattr__(self, "required_hard_constraint_refs", tuple(sorted(
            set(self.required_hard_constraint_refs))))
        return self


class CandidateEvaluation(FrozenModel):
    grounded_option_identity: Reference
    eligible: bool
    rejection_reasons: tuple[str, ...] = ()
    forecast_ref: Reference | None = None
    expected_contract_progress: float | None = Field(default=None, ge=0, le=1)
    expected_cost: float | None = Field(default=None, ge=0)
    expected_latency_seconds: float | None = Field(default=None, ge=0)
    expected_risk: float | None = Field(default=None, ge=0)
    tie_break_key: tuple[str, ...] = ()


class ControllerDecision(FrozenModel):
    schema_version: Literal["controller-decision-v1"] = "controller-decision-v1"
    decision_id: Reference
    decision_snapshot_identity: Reference
    candidate_set_identity: Reference
    forecast_refs: tuple[Reference, ...]
    baseline: SelectionBaseline
    horizon: Literal[1, 2, 3]
    candidates: tuple[CandidateEvaluation, ...]
    selected_grounded_option_identity: Reference | None = None
    status: Literal["SELECT", "DEFER", "STOP"]
    reason: str
    tie_break_trace: tuple[str, ...] = ()
    provenance_refs: tuple[Reference, ...] = ()

    @property
    def identity(self):
        return "decision:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class CandidateSelectionPolicy(Protocol):
    def decide(self, snapshot: DecisionSnapshot, candidates, forecasts, *, horizon,
               policy, current_snapshot_identity) -> ControllerDecision: ...


class _RolloutValue(tuple):
    __slots__ = ()

    def __new__(cls, progress, cost, latency, risk):
        return tuple.__new__(cls, (progress, cost, latency, risk))

    progress = property(lambda self: self[0])
    cost = property(lambda self: self[1])
    latency = property(lambda self: self[2])
    risk = property(lambda self: self[3])


def _candidate_set_identity(candidates):
    values = sorted(candidate.identity for candidate in candidates)
    return "candidates:" + hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _binding_reasons(snapshot, grounded, forecast, state, policy):
    reasons = []
    if ((grounded.contract_ref, grounded.contract_version) !=
            (snapshot.contract_ref, snapshot.contract_version)):
        reasons.append("GROUNDING_STALE: contract")
    if ((grounded.plan_ref, grounded.plan_version, grounded.plan_identity_hash) !=
            (snapshot.plan_ref, snapshot.plan_version, snapshot.plan_identity_hash)):
        reasons.append("GROUNDING_STALE: plan")
    if grounded.runtime_fingerprint != snapshot.runtime_fingerprint:
        reasons.append("GROUNDING_STALE: runtime")
    if grounded.identity in policy.forbidden_grounding_refs:
        reasons.append("hard policy forbids candidate")
    if state.unresolved_effect_refs:
        reasons.append("unresolved conflicting effect")
    if not set(forecast.requires_enabled_plan_node_refs) <= set(state.ready_plan_node_refs):
        reasons.append("projected plan prerequisite is not enabled")
    required = {resource for step in grounded.steps for resource in step.required_resources}
    if not required <= set(state.available_resources):
        reasons.append("required resource unavailable")
    if forecast.status != "USABLE":
        reasons.append("FORECAST_UNAVAILABLE")
        return reasons
    if state.remaining_budget is not None and any(
            branch.cost > state.remaining_budget for branch in forecast.branches):
        reasons.append("hard budget infeasible")
    if any(not set(branch.consumed_resources) <= set(state.available_resources)
           for branch in forecast.branches):
        reasons.append("forecast resource effect infeasible")
    if any(RISK_VALUE[branch.risk] > RISK_VALUE[policy.max_risk]
           for branch in forecast.branches):
        reasons.append("risk policy exceeded")
    return reasons


def _better(value, identity, incumbent, incumbent_identity):
    key = (-value.progress, value.cost, value.latency, value.risk, identity)
    other = (-incumbent.progress, incumbent.cost, incumbent.latency,
             incumbent.risk, incumbent_identity)
    return key < other


class FiniteHorizonController:
    """Enumerated H<=3 controller. It returns only the first option identity."""

    baseline = SelectionBaseline.FINITE_HORIZON

    def decide(self, snapshot: DecisionSnapshot, candidates: tuple[GroundedOption, ...],
               forecasts: tuple[OutcomeForecast, ...], *, horizon: int,
               policy: ControllerPolicy | None = None,
               current_snapshot_identity: str) -> ControllerDecision:
        if horizon not in {1, 2, 3}:
            raise ValueError("supported controller horizons are H=1, H=2, H=3")
        policy = policy or ControllerPolicy()
        candidate_set = _candidate_set_identity(candidates)
        forecast_by_grounding = {item.grounded_option_identity: item for item in forecasts}
        if len(forecast_by_grounding) != len(forecasts):
            raise ValueError("one forecast per grounded candidate is required")
        if current_snapshot_identity != snapshot.identity:
            return self._decision(snapshot, candidate_set, forecasts, horizon, (), None,
                "DEFER", "STALE_SNAPSHOT: rebuild DecisionSnapshot", ())
        if snapshot.unresolved_effect_refs:
            evaluations = tuple(CandidateEvaluation(grounded_option_identity=item.identity,
                eligible=False, rejection_reasons=("unresolved conflicting effect",))
                for item in sorted(candidates, key=lambda row: row.identity))
            return self._decision(snapshot, candidate_set, forecasts, horizon, evaluations,
                None, "DEFER", "Reconciliation required before scientific continuation", ())
        if snapshot.violated_constraint_refs or not set(policy.required_hard_constraint_refs) <= set(
                snapshot.hard_constraint_refs):
            reason = ("Committed hard constraint is unresolved" if snapshot.violated_constraint_refs
                      else "Required ResearchContract constraint is absent")
            evaluations = tuple(CandidateEvaluation(grounded_option_identity=item.identity,
                eligible=False, rejection_reasons=(reason,))
                for item in sorted(candidates, key=lambda row: row.identity))
            return self._decision(snapshot, candidate_set, forecasts, horizon, evaluations,
                None, "STOP", reason, ())
        by_identity = {item.identity: item for item in candidates}
        if len(by_identity) != len(candidates):
            raise ValueError("duplicate grounded candidate")
        if set(forecast_by_grounding) - set(by_identity):
            raise ValueError("forecast supplied for a candidate outside the decision set")
        root = hypothetical_state(snapshot)
        evaluations = []
        best = best_identity = None
        trace = []
        for identity in sorted(by_identity):
            grounded = by_identity[identity]
            forecast = forecast_by_grounding.get(identity)
            reasons = ["forecast missing"] if forecast is None else []
            if forecast is not None:
                try:
                    validate_forecast(forecast, snapshot, grounded)
                except ValueError as exc:
                    reasons.append(str(exc))
                reasons.extend(_binding_reasons(snapshot, grounded, forecast, root, policy))
            if reasons:
                evaluations.append(CandidateEvaluation(grounded_option_identity=identity,
                    eligible=False, rejection_reasons=tuple(dict.fromkeys(reasons)),
                    forecast_ref=forecast.identity if forecast else None))
                continue
            value = self._rollout(snapshot, root, identity, by_identity,
                                  forecast_by_grounding, horizon, frozenset(), policy)
            evaluation = CandidateEvaluation(grounded_option_identity=identity, eligible=True,
                forecast_ref=forecast.identity, expected_contract_progress=value.progress,
                expected_cost=value.cost, expected_latency_seconds=value.latency,
                expected_risk=value.risk, tie_break_key=(f"progress={value.progress:.12g}",
                f"cost={value.cost:.12g}", f"latency={value.latency:.12g}",
                f"risk={value.risk:.12g}", identity))
            evaluations.append(evaluation)
            trace.append(identity + ":" + "|".join(evaluation.tie_break_key))
            if best is None or _better(value, identity, best, best_identity):
                best, best_identity = value, identity
        if best_identity is None:
            return self._decision(snapshot, candidate_set, forecasts, horizon,
                tuple(evaluations), None, "DEFER", "No eligible forecasted option", tuple(trace))
        return self._decision(snapshot, candidate_set, forecasts, horizon,
            tuple(evaluations), best_identity, "SELECT",
            "Maximal expected finite-horizon ResearchContract progress", tuple(trace))

    def _rollout(self, snapshot, state, first_identity, candidates, forecasts,
                 depth, used, policy):
        grounded = candidates[first_identity]
        forecast = forecasts[first_identity]
        expected_progress = expected_cost = expected_latency = expected_risk = 0.0
        for branch in forecast.branches:
            next_state = hypothetical_transition(snapshot, grounded, branch, state=state)
            future = _RolloutValue(next_state.progress_value, 0, 0, 0)
            terminal_path = (depth == 1 or next_state.progress_value >= 1.0 or
                             not next_state.operationally_feasible or
                             branch.outcome_class == RuntimeOutcome.EFFECT_UNKNOWN)
            if not terminal_path:
                future = self._best_future(snapshot, next_state, candidates, forecasts,
                                           depth - 1, used | {first_identity}, policy)
            expected_progress += branch.probability * future.progress
            expected_cost += branch.probability * (branch.cost + future.cost)
            expected_latency += branch.probability * (branch.latency_seconds + future.latency)
            expected_risk += branch.probability * (RISK_VALUE[branch.risk] + future.risk)
        return _RolloutValue(expected_progress, expected_cost, expected_latency, expected_risk)

    def _best_future(self, snapshot, state, candidates, forecasts, depth, used, policy):
        best = None
        best_identity = None
        for identity in sorted(set(candidates) - set(used)):
            forecast = forecasts.get(identity)
            if forecast is None:
                continue
            try:
                validate_forecast(forecast, snapshot, candidates[identity])
            except ValueError:
                continue
            reasons = _binding_reasons(snapshot, candidates[identity], forecast, state, policy)
            if reasons:
                continue
            value = self._rollout(snapshot, state, identity, candidates, forecasts,
                                  depth, used, policy)
            if best is None or _better(value, identity, best, best_identity):
                best, best_identity = value, identity
        return best or _RolloutValue(state.progress_value, 0, 0, 0)

    def _decision(self, snapshot, candidate_set, forecasts, horizon, evaluations,
                  selected, status, reason, trace):
        payload = {"snapshot": snapshot.identity, "candidates": candidate_set,
                   "baseline": self.baseline.value, "horizon": horizon,
                   "selected": selected, "status": status}
        decision_id = "controller:" + hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ControllerDecision(decision_id=decision_id,
            decision_snapshot_identity=snapshot.identity, candidate_set_identity=candidate_set,
            forecast_refs=tuple(sorted(item.identity for item in forecasts)),
            baseline=self.baseline, horizon=horizon, candidates=tuple(evaluations),
            selected_grounded_option_identity=selected, status=status, reason=reason,
            tie_break_trace=trace, provenance_refs=("finite-horizon-enumeration-v1",))


class OneStepForecastPolicy(FiniteHorizonController):
    """Baseline hook using exactly the H=1 path of the same algorithm."""
    baseline = SelectionBaseline.ONE_STEP_FORECAST

    def decide(self, snapshot, candidates, forecasts, *, horizon=1, policy=None,
               current_snapshot_identity):
        if horizon != 1:
            raise ValueError("one-step forecast baseline requires H=1")
        decision = super().decide(snapshot, candidates, forecasts, horizon=1,
            policy=policy, current_snapshot_identity=current_snapshot_identity)
        return decision.model_copy(update={"baseline": self.baseline})


BASELINE_HOOKS = {
    SelectionBaseline.ONE_STEP_FORECAST: OneStepForecastPolicy,
    SelectionBaseline.FINITE_HORIZON: FiniteHorizonController,
    # D0, goal relevance, and structural Shapley are evaluation interfaces only.
    SelectionBaseline.D0_LLM: None,
    SelectionBaseline.GOAL_RELEVANCE: None,
    SelectionBaseline.STRUCTURAL_SHAPLEY: None,
}
