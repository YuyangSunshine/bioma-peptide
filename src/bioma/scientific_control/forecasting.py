"""Backend-neutral production seam for non-evidentiary outcome forecasts."""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Protocol

from bioma.core.research_contract import ObligationRole, ResearchContract

from .forecast import (DecisionSnapshot, ForecastBranch, ForecastSource,
                       OutcomeForecast, ProjectedObligationEffect,
                       ProjectedStatus, RuntimeOutcome)
from .grounding import GroundedOption
from .options import ScientificOption


class OutcomeForecaster(Protocol):
    def forecast(self, snapshot: DecisionSnapshot, option: ScientificOption,
                 grounded: GroundedOption, contract: ResearchContract,
                 evidence_state) -> OutcomeForecast: ...


class RuleOutcomeForecaster:
    """Forecast declared delivery and cost without inventing scientific odds."""

    def forecast(self, snapshot, option, grounded, contract, evidence_state):
        # A method contract establishes what can be delivered, not whether a
        # scientific claim will be supported. With no calibrated predictor the
        # probability-one branch must leave epistemic status unchanged.
        effects = [ProjectedObligationEffect(obligation_ref=ref,
            projected_status=ProjectedStatus.UNCHANGED, scientific_outcome=None,
            rationale="Scientific outcome is unknown until a result is qualified and committed.")
            for ref in option.target_contract_obligation_refs]
        cost = sum(step.estimated_cost for step in grounded.steps)
        latency = sum(step.estimated_latency_seconds for step in grounded.steps)
        branch_id = "branch:" + hashlib.sha256(grounded.identity.encode()).hexdigest()
        return OutcomeForecast(forecast_id="rule:" + hashlib.sha256(
            (snapshot.identity + grounded.identity).encode()).hexdigest(),
            grounded_option_identity=grounded.identity,
            decision_snapshot_identity=snapshot.identity, source=ForecastSource.RULE,
            method="declared-deliverable-contract", method_version="2",
            scientific_outcome_known=False,
            expected_deliverable_classes=option.expected_result_classes,
            covered_obligation_refs=option.target_contract_obligation_refs,
            branches=(ForecastBranch(branch_id=branch_id, probability=1.0,
                outcome_class=RuntimeOutcome.INCONCLUSIVE,
                obligation_effects=tuple(effects), cost=cost,
                latency_seconds=latency, risk="low", operationally_feasible=True),),
            provenance_refs=(option.identity, grounded.identity))


class CallableOutcomeForecaster:
    """Adapter for model/LLM backends; the caller controls all invocation."""

    def __init__(self, forecast_fn: Callable[..., Any]):
        self.forecast_fn = forecast_fn

    def forecast(self, snapshot, option, grounded, contract, evidence_state):
        raw = self.forecast_fn(snapshot=snapshot.model_dump(mode="json"),
            option=option.model_dump(mode="json"), grounded=grounded.model_dump(mode="json"),
            contract=contract.model_dump(mode="json"),
            evidence_projection={"state_version": evidence_state.version,
                "evidence_refs": [row.id for row in evidence_state.evidence]})
        return OutcomeForecast.model_validate(raw)


ModelOutcomeForecaster = CallableOutcomeForecaster
