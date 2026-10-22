"""Fail-closed scientific decision compilation and explicit L1/L2 predictors."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Hashable, Mapping

from bioma.core.research_contract import AcceptanceObligation, ObligationRole, ResearchContract, canonical
from bioma.core.scientific_decision import (
    PredictorCapability, ScientificDecisionSpec, ScientificDecisionType)


class ScientificDecisionCompiler:
    """Compile explicit or canonical semantics and otherwise abstain."""

    def compile(self, contract: ResearchContract) -> ScientificDecisionSpec:
        scope = json.loads(contract.scope_json)
        declared = scope.get("scientific_decision")
        if isinstance(declared, dict):
            return self._declared(contract, declared)
        hypotheses = [c for c in contract.claims
                      if c.get("kind") == "hypothesis" and c.get("exclusive_group")]
        groups = {c.get("exclusive_group") for c in hypotheses}
        if len(hypotheses) >= 2 and len(groups) == 1:
            return ScientificDecisionSpec(decision_type=ScientificDecisionType.IDENTIFICATION,
                latent_target=str(next(iter(groups))), decision_space=tuple(str(c["id"]) for c in hypotheses),
                loss_semantics="zero_one", constraints_json=contract.hard_constraints_json,
                source_provenance=("ResearchContract.claims_json[*].kind=hypothesis",
                    "canonical:IDENTIFICATION.zero_one"), compiler_status="RESOLVED")
        roles = {item.effective_role for item in contract.requirements}
        if roles == {ObligationRole.DELIVERY}:
            return self._unresolved(ScientificDecisionType.DELIVERY_ONLY,
                                    "delivery has no scientific terminal decision")
        if not contract.claims and roles <= {ObligationRole.SUPPORTING}:
            return self._unresolved(ScientificDecisionType.OPEN_ENDED_RESEARCH,
                                    "open-ended/supporting research has no declared decision")
        return self._unresolved(ScientificDecisionType.UNDERSPECIFIED,
                                "contract lacks grounded scientific decision semantics")

    def _declared(self, contract, value):
        try:
            kind = ScientificDecisionType(value["decision_type"])
            target = str(value["latent_target"])
            decisions = tuple(str(v) for v in value["decision_space"])
        except (KeyError, TypeError, ValueError):
            return self._unresolved(ScientificDecisionType.UNDERSPECIFIED,
                                    "declared decision header is incomplete")
        provenance = ["ResearchContract.scope_json.scientific_decision"]
        constraints = json.loads(contract.hard_constraints_json)
        if kind is ScientificDecisionType.IDENTIFICATION:
            if len(set(decisions)) < 2:
                return self._unresolved(kind, "identification needs at least two candidates")
            return ScientificDecisionSpec(decision_type=kind, latent_target=target,
                decision_space=decisions, loss_semantics="zero_one",
                constraints_json=canonical(constraints), source_provenance=(*provenance,
                    "canonical:IDENTIFICATION.zero_one"), compiler_status="RESOLVED")
        if kind is ScientificDecisionType.ACCEPTANCE:
            predicates = value.get("acceptance_predicates")
            if not isinstance(predicates, list) or not predicates:
                return self._unresolved(kind, "acceptance needs explicit predicates")
            return ScientificDecisionSpec(decision_type=kind, latent_target=target,
                decision_space=decisions, loss_semantics="acceptance",
                acceptance_predicate_json=canonical(predicates), constraints_json=canonical(constraints),
                source_provenance=(*provenance,
                    "ResearchContract.scope_json.scientific_decision.acceptance_predicates"),
                compiler_status="RESOLVED", abstention_allowed=bool(value.get("abstention_allowed")))
        if kind is ScientificDecisionType.CONSTRAINED_SELECTION:
            objective = value.get("objective")
            declared_constraints = value.get("constraints")
            if not isinstance(objective, dict) or not objective or not isinstance(declared_constraints, list) or not declared_constraints:
                return self._unresolved(kind, "selection needs explicit objective and constraints")
            return ScientificDecisionSpec(decision_type=kind, latent_target=target,
                decision_space=decisions, loss_semantics="constrained",
                objective_json=canonical(objective), constraints_json=canonical([*constraints, *declared_constraints]),
                source_provenance=(*provenance,
                    "ResearchContract.scope_json.scientific_decision.objective",
                    "ResearchContract.hard_constraints_json"), compiler_status="RESOLVED",
                abstention_allowed=bool(value.get("abstention_allowed")))
        if kind is ScientificDecisionType.ESTIMATION:
            estimation = value.get("estimation")
            if not isinstance(estimation, dict) or not estimation.get("target") or not (
                    "tolerance" in estimation or "scoring" in estimation):
                return self._unresolved(kind, "estimation needs target plus tolerance/scoring")
            return ScientificDecisionSpec(decision_type=kind, latent_target=target,
                decision_space=decisions, loss_semantics="estimation",
                objective_json=canonical(estimation), constraints_json=canonical(constraints),
                source_provenance=(*provenance,
                    "ResearchContract.scope_json.scientific_decision.estimation"),
                compiler_status="RESOLVED", abstention_allowed=bool(value.get("abstention_allowed")))
        return self._unresolved(kind, "non-decision request cannot compile")

    @staticmethod
    def _unresolved(kind, reason):
        return ScientificDecisionSpec(decision_type=kind,
            source_provenance=("ResearchContract",), compiler_status="DECISION_SPEC_UNRESOLVED",
            unresolved_reason=reason)


@dataclass(frozen=True)
class TableScientificPredictor:
    predictor_id: str
    capability: PredictorCapability
    distributions: Mapping[str, Mapping[Hashable, Mapping[Hashable, float]]]

    def predict_evidence(self, *, hypothesis, grounded_action, biological_context,
                         evidence_schema):
        action_id = grounded_action.identity
        if self.capability < PredictorCapability.L1_HYPOTHESIS_CONDITIONED:
            raise ValueError("HYPOTHESIS_CONDITIONED_FORECAST_UNAVAILABLE")
        if action_id not in self.distributions or hypothesis not in self.distributions[action_id]:
            raise ValueError("predictor has no declared hypothesis/action distribution")
        values = dict(self.distributions[action_id][hypothesis])
        if abs(sum(values.values()) - 1) > 1e-9 or any(v < 0 for v in values.values()):
            raise ValueError("invalid evidence distribution")
        return values


class MechanisticScientificPredictor(TableScientificPredictor):
    """Deterministic rule/mechanistic L2 table for declared interventions."""


class EmpiricalScientificPredictor(TableScientificPredictor):
    """Versioned empirical L1 table; calibration provenance is caller-owned."""

