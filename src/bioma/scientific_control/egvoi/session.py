"""Decision-only wake gate: no executor, provider, or commit authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from typing import Mapping

from bioma.core.scientific_decision import PredictorCapability, ScientificDecisionType
from .kernel import DecisionProblem, EvidenceChannel


SUPPORTED = {ScientificDecisionType.IDENTIFICATION, ScientificDecisionType.ACCEPTANCE,
             ScientificDecisionType.CONSTRAINED_SELECTION, ScientificDecisionType.ESTIMATION}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def resolve_problem(spec, hypotheses, *, declared_losses=None):
    """Finite loss tables must be explicitly supplied for non-identification specs.

    A resolved predicate/objective alone is not a numeric inference model.
    No loss scale or acceptance interpretation is invented here.
    """
    if spec.compiler_status != 'RESOLVED' or spec.decision_type not in SUPPORTED:
        raise ValueError('DECISION_SPEC_UNRESOLVED')
    hypotheses = tuple(hypotheses)
    if not hypotheses or len(set(hypotheses)) != len(hypotheses):
        raise ValueError('finite unique latent domain required')
    if spec.loss_semantics == 'zero_one':
        if set(spec.decision_space) != set(hypotheses):
            raise ValueError('identification domain differs from decision space')
        expected = {d: {z: float(d != z) for z in hypotheses} for d in spec.decision_space}
        if declared_losses is not None and declared_losses != expected:
            raise ValueError('declared loss disagrees with identification semantics')
        return DecisionProblem(expected)
    if declared_losses is None or set(declared_losses) != set(spec.decision_space):
        raise ValueError('explicit finite loss interpretation required')
    if any(set(row) != set(hypotheses) or
           any(not isfinite(v) or v < 0 for v in row.values())
           for row in declared_losses.values()):
        raise ValueError('invalid finite loss interpretation')
    return DecisionProblem(declared_losses)


def predictor_channel(predictor, grounded, evidence_schema):
    """The predictor protocol returns qualified evidence, never raw forecasts."""
    if predictor.capability not in (PredictorCapability.L1_HYPOTHESIS_CONDITIONED,
                                   PredictorCapability.L2_INTERVENTIONAL):
        raise ValueError('HYPOTHESIS_CONDITIONED_FORECAST_UNAVAILABLE')
    return EvidenceChannel(lambda _a, z, context: predictor.predict_evidence(
        hypothesis=z, grounded_action=grounded, biological_context=context,
        evidence_schema=evidence_schema), lambda symbol, _a, _c: {symbol: 1.0})


@dataclass
class DecisionSession:
    """One episode; caller supplies only decision-relevant operational fields.

    Persist ``checkpoint()`` alongside the ledger to resume a wake decision.
    WORK never enters decision compilation or calls the Bellman controller.
    """
    spec: object
    controller: object
    previous_fingerprint: str | None = None
    controller_calls: int = 0
    terminal_resolved: bool = False

    def decide(self, *, task_class, belief, operational_state: Mapping,
               qualified_capabilities, unresolved_effects=(), objective_amendments=None,
               horizon=2):
        if task_class != 'SCIENTIFIC_CONTROL' or self.terminal_resolved:
            return None
        if self.spec.compiler_status != 'RESOLVED' or self.spec.decision_type not in SUPPORTED:
            return None
        expected = resolve_problem(self.spec, next(iter(self.controller.problem.losses.values())),
                                   declared_losses=self.controller.problem.losses)
        if expected.losses != self.controller.problem.losses:
            raise ValueError('controller loss does not implement decision spec')
        identity = fingerprint({'spec': self.spec.model_dump(mode='json'),
            'belief': belief.version, 'evidence_refs': belief.evidence_refs,
            'operational': operational_state, 'capabilities': qualified_capabilities,
            'unresolved_effects': unresolved_effects, 'amendments': objective_amendments})
        if identity == self.previous_fingerprint:
            return None
        decision = self.controller.decide(belief.state, operational_state, horizon)
        self.previous_fingerprint = identity
        self.controller_calls += 1
        return decision

    def checkpoint(self):
        return {'previous_fingerprint': self.previous_fingerprint,
                'controller_calls': self.controller_calls,
                'terminal_resolved': self.terminal_resolved}
