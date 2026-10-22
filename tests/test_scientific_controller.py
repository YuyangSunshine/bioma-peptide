"""Phase C2 exact finite-horizon scientific-control invariants."""
from datetime import datetime, timezone

import pytest

from bioma.core.research_contract import ObligationRole
from bioma.scientific_control import (
    CandidateEvaluation, ControllerPolicy, DecisionSnapshot, FiniteHorizonController,
    ForecastBranch, ForecastSource, GroundedOption, GroundedStep, ObligationProgress,
    OneStepForecastPolicy, OutcomeForecast, ProjectedObligationEffect, ProjectedStatus,
    RuntimeOutcome, SelectionBaseline,
)


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def progress(ref, role=ObligationRole.ACHIEVEMENT, *, credited=False):
    return ObligationProgress(obligation_ref=ref, role=role, mandatory=True,
        terminal=role is not ObligationRole.SUPPORTING,
        status="SATISFIED" if credited else "UNRESOLVED", progress_credit=credited,
        acceptance_status="satisfied" if credited else "missing",
        rationale="fixture")


def snapshot(*rows, ready=("root",), budget=10, resources=(), unresolved=(), violated=()):
    terminal = [row for row in rows if row.mandatory and row.terminal]
    value = sum(row.progress_credit for row in terminal) / len(terminal) if terminal else 0
    return DecisionSnapshot(contract_ref="C", contract_version=1, plan_ref="P", plan_version=1,
        plan_identity_hash="PH", committed_state_ref="E", committed_state_version=0,
        runtime_fingerprint="R", unresolved_effect_refs=unresolved,
        violated_constraint_refs=violated, remaining_budget=budget,
        available_resources=resources, ready_plan_node_refs=ready,
        obligation_progress=rows, progress_value=value, timestamp=NOW)


def candidate(name, *, resources=()):
    step = GroundedStep(step_id="S", capability_id="cap", method_id="M",
        provider_id="worker", implementation_identity="impl:" + name,
        message_type="work.requested", input_bindings_json="{}",
        result_contract_json='{"responsibility":"cap","evidence_kinds":["result"],"data_kinds":[],"artifact_kinds":[],"optional_artifact_kinds":[],"named_output_extensions":null,"verification_obligations":["check"],"stage":"work"}',
        required_resources=resources, verification_strategy="integrity",
        estimated_cost=0, estimated_latency_seconds=0,
        qualification_provenance_json="{}")
    return GroundedOption(option_identity="option:" + name, contract_ref="C",
        contract_version=1, plan_ref="P", plan_version=1, plan_identity_hash="PH",
        runtime_fingerprint="R", steps=(step,))


def effect(ref, status=ProjectedStatus.SATISFIED, outcome=None):
    return ProjectedObligationEffect(obligation_ref=ref, projected_status=status,
        scientific_outcome=outcome, rationale="conditional fixture outcome")


def forecast(snap, option, *, effects=(), outcome=RuntimeOutcome.QUALIFIED_POSITIVE,
             probability=1, cost=0, latency=0, risk="low", requires=(), enables=(),
             feasible=True, branch_id="b"):
    branch = ForecastBranch(branch_id=branch_id, probability=probability,
        outcome_class=outcome, obligation_effects=effects,
        enables_plan_node_refs=enables, cost=cost, latency_seconds=latency,
        risk=risk, operationally_feasible=feasible)
    return OutcomeForecast(forecast_id="F:" + option.option_identity,
        grounded_option_identity=option.identity, decision_snapshot_identity=snap.identity,
        source=ForecastSource.RULE, method="fixture", method_version="1",
        requires_enabled_plan_node_refs=requires, branches=(branch,))


def decide(snap, options, forecasts, horizon=1, policy=None, controller=None, current=None):
    return (controller or FiniteHorizonController()).decide(snap, tuple(options), tuple(forecasts),
        horizon=horizon, policy=policy or ControllerPolicy(max_risk="high"),
        current_snapshot_identity=current or snap.identity)


def test_immediate_terminal_progress_beats_irrelevant_supporting_work():
    snap = snapshot(progress("A"), progress("S", ObligationRole.SUPPORTING))
    terminal, supporting = candidate("terminal"), candidate("support")
    decision = decide(snap, (supporting, terminal), (
        forecast(snap, supporting, effects=(effect("S"),)),
        forecast(snap, terminal, effects=(effect("A"),))))
    assert decision.selected_grounded_option_identity == terminal.identity
    scores = {row.grounded_option_identity: row.expected_contract_progress for row in decision.candidates}
    assert scores[terminal.identity] == 1 and scores[supporting.identity] == 0


def test_qualified_falsification_has_positive_question_progress():
    snap = snapshot(progress("Q", ObligationRole.QUESTION))
    option = candidate("falsify")
    decision = decide(snap, (option,), (forecast(snap, option,
        effects=(effect("Q", ProjectedStatus.RESOLVED, "contradicted"),),
        outcome=RuntimeOutcome.QUALIFIED_NEGATIVE),))
    assert decision.candidates[0].expected_contract_progress == 1


def test_negative_result_never_rewards_achievement():
    snap = snapshot(progress("A"))
    option = candidate("negative")
    decision = decide(snap, (option,), (forecast(snap, option,
        effects=(effect("A"),), outcome=RuntimeOutcome.QUALIFIED_NEGATIVE),))
    assert decision.candidates[0].expected_contract_progress == 0


def test_h2_selects_zero_immediate_preparation_over_greedy_h1():
    snap = snapshot(progress("A"), progress("B"), ready=("prep", "greedy"))
    prep, terminal, greedy = candidate("prep"), candidate("terminal"), candidate("greedy")
    forecasts = (forecast(snap, prep, enables=("terminal",), cost=.1),
        forecast(snap, terminal, requires=("terminal",), effects=(effect("A"), effect("B"))),
        forecast(snap, greedy, effects=(effect("A"),)))
    h1 = decide(snap, (prep, terminal, greedy), forecasts, horizon=1)
    h2 = decide(snap, (prep, terminal, greedy), forecasts, horizon=2)
    assert h1.selected_grounded_option_identity == greedy.identity
    assert h2.selected_grounded_option_identity == prep.identity
    assert next(row for row in h2.candidates if row.grounded_option_identity == prep.identity).expected_contract_progress == 1


def test_higher_progress_hard_risk_invalid_option_is_excluded():
    snap = snapshot(progress("A"), progress("B"))
    unsafe, safe = candidate("unsafe"), candidate("safe")
    decision = decide(snap, (unsafe, safe), (
        forecast(snap, unsafe, effects=(effect("A"), effect("B")), risk="high"),
        forecast(snap, safe, effects=(effect("A"),), risk="low")),
        policy=ControllerPolicy(max_risk="low"))
    assert decision.selected_grounded_option_identity == safe.identity
    rejected = next(row for row in decision.candidates if row.grounded_option_identity == unsafe.identity)
    assert not rejected.eligible and "risk policy exceeded" in rejected.rejection_reasons


@pytest.mark.parametrize("first,second,expected", [
    ({"cost": 1, "latency": 10, "risk": "low"}, {"cost": 2, "latency": 1, "risk": "low"}, "first"),
    ({"cost": 1, "latency": 1, "risk": "low"}, {"cost": 1, "latency": 2, "risk": "low"}, "first"),
    ({"cost": 1, "latency": 1, "risk": "low"}, {"cost": 1, "latency": 1, "risk": "medium"}, "first"),
])
def test_equal_scientific_progress_uses_cost_latency_then_risk(first, second, expected):
    snap = snapshot(progress("A"))
    a, b = candidate("first"), candidate("second")
    decision = decide(snap, (b, a), (forecast(snap, a, effects=(effect("A"),), **first),
        forecast(snap, b, effects=(effect("A"),), **second)))
    assert decision.selected_grounded_option_identity == {"first": a, "second": b}[expected].identity


def test_stable_identity_is_final_tie_break():
    snap = snapshot(progress("A"))
    a, b = candidate("a"), candidate("b")
    forecasts = (forecast(snap, a, effects=(effect("A"),)),
                 forecast(snap, b, effects=(effect("A"),)))
    decision = decide(snap, (b, a), reversed(forecasts))
    assert decision.selected_grounded_option_identity == min(a.identity, b.identity)


def test_stale_snapshot_returns_explicit_defer_without_selection():
    snap = snapshot(progress("A"))
    option = candidate("x")
    decision = decide(snap, (option,), (forecast(snap, option),), current="snapshot:new")
    assert decision.status == "DEFER" and decision.selected_grounded_option_identity is None
    assert "STALE_SNAPSHOT" in decision.reason


def test_effect_unknown_path_never_projects_blind_continuation():
    snap = snapshot(progress("A"), ready=("prep",))
    prep, terminal = candidate("prep"), candidate("terminal")
    decision = decide(snap, (prep, terminal), (
        forecast(snap, prep, outcome=RuntimeOutcome.EFFECT_UNKNOWN, enables=("terminal",)),
        forecast(snap, terminal, requires=("terminal",), effects=(effect("A"),))), horizon=2)
    prep_row = next(row for row in decision.candidates if row.grounded_option_identity == prep.identity)
    assert prep_row.expected_contract_progress == 0


def test_execution_failure_changes_feasibility_without_scientific_evidence():
    snap = snapshot(progress("A"))
    option = candidate("failure")
    item = forecast(snap, option, outcome=RuntimeOutcome.EXECUTION_FAILURE, feasible=False)
    decision = decide(snap, (option,), (item,), horizon=3)
    assert decision.candidates[0].expected_contract_progress == 0
    assert not {"evidence", "state_delta", "receipt"}.intersection(type(decision).model_fields)


def test_one_step_policy_is_exact_h1_algorithm_not_another_formula():
    snap = snapshot(progress("A"))
    a, b = candidate("a"), candidate("b")
    forecasts = (forecast(snap, a, effects=(effect("A"),), cost=2),
                 forecast(snap, b, effects=(effect("A"),), cost=1))
    full = decide(snap, (a, b), forecasts, horizon=1)
    one = decide(snap, (a, b), forecasts, horizon=1, controller=OneStepForecastPolicy())
    assert one.selected_grounded_option_identity == full.selected_grounded_option_identity
    assert one.candidates == full.candidates and one.baseline is SelectionBaseline.ONE_STEP_FORECAST


def test_no_valid_option_returns_defer_not_fabricated_completion():
    snap = snapshot(progress("A"))
    option = candidate("x", resources=("gpu",))
    unavailable = OutcomeForecast(forecast_id="U", grounded_option_identity=option.identity,
        decision_snapshot_identity=snap.identity, source=ForecastSource.EMPIRICAL,
        method="none", method_version="1", status="FORECAST_UNAVAILABLE",
        unavailable_reason="no history")
    decision = decide(snap, (option,), (unavailable,))
    assert decision.status == "DEFER" and decision.selected_grounded_option_identity is None


def test_unresolved_real_effect_defers_every_candidate():
    snap = snapshot(progress("A"), unresolved=("effect:unknown",))
    option = candidate("x")
    decision = decide(snap, (option,), (forecast(snap, option),))
    assert decision.status == "DEFER" and "Reconciliation" in decision.reason


def test_hard_constraint_violation_stops_control():
    snap = snapshot(progress("A"), violated=("constraint:safety",))
    option = candidate("x")
    decision = decide(snap, (option,), (forecast(snap, option),))
    assert decision.status == "STOP" and not decision.selected_grounded_option_identity


def test_reversed_candidate_and_forecast_order_is_deterministic():
    snap = snapshot(progress("A"))
    a, b = candidate("a"), candidate("b")
    fa, fb = forecast(snap, a, effects=(effect("A"),)), forecast(snap, b, effects=(effect("A"),))
    first = decide(snap, (a, b), (fa, fb), horizon=3)
    second = decide(snap, (b, a), (fb, fa), horizon=3)
    assert first == second and first.identity == second.identity


def test_controller_rejects_unsupported_unbounded_horizon():
    snap = snapshot(progress("A"))
    with pytest.raises(ValueError, match="H=1"):
        decide(snap, (), (), horizon=4)
