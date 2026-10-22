"""Finite-horizon residual Bayes-risk control with endogenous STOP."""
from __future__ import annotations

from dataclasses import dataclass
from math import fsum
from random import Random
from typing import Callable, Hashable, Mapping, Sequence

from .kernel import BeliefState, DecisionProblem, EvidenceChannel, EGVoIController


@dataclass(frozen=True)
class BellmanDecision:
    status: str
    action: Hashable | None
    expected_terminal_risk: float
    horizon: int


class BellmanRiskController:
    """Exact finite-horizon Bayes-risk recursion over (belief, operational context).

    Budget/cost/risk affect admissibility and operational transitions, never the
    scientific objective. STOP is the incumbent current Bayes risk.
    """

    def __init__(self, problem: DecisionProblem, *,
                 actions: Callable[[Mapping[str, object]], Sequence[Hashable]],
                 channel: Callable[[Hashable, Mapping[str, object]], EvidenceChannel],
                 transition: Callable[[Mapping[str, object], Hashable, Hashable], Mapping[str, object]],
                 action_cost: Callable[[Hashable, Mapping[str, object]], float],
                 action_latency: Callable[[Hashable, Mapping[str, object]], float] = lambda _a, _r: 0.0):
        self.problem = problem
        self.actions = actions
        self.channel = channel
        self.transition = transition
        self.action_cost = action_cost
        self.action_latency = action_latency

    def decide(self, belief: BeliefState, context: Mapping[str, object], horizon: int) -> BellmanDecision:
        if horizon not in {1, 2, 3}:
            raise ValueError("supported horizons are 1, 2, 3")
        risk, action = self._value(belief, context, horizon)
        return BellmanDecision("STOP" if action is None else "SELECT", action, risk, horizon)

    def _value(self, belief, context, horizon):
        stop_risk = self.problem.bayes_risk(belief)
        best_risk, best_action = stop_risk, None
        best_tie = (float("inf"), float("inf"), "")
        for action in sorted(self.actions(context), key=repr):
            cost = self.action_cost(action, context)
            if cost > float(context.get("remaining_budget", float("inf"))):
                continue
            evidence_channel = self.channel(action, context)
            controller = EGVoIController(self.problem, evidence_channel)
            expected = 0.0
            for evidence, probability in evidence_channel.predictive_evidence(
                    belief, action, context).items():
                updated = controller.update(belief, action, evidence, context)
                next_context = self.transition(context, action, evidence)
                future = (self.problem.bayes_risk(updated) if horizon == 1
                          else self._value(updated, next_context, horizon - 1)[0])
                expected += probability * future
            tie = (cost, self.action_latency(action, context), repr(action))
            if expected < best_risk - 1e-12 or (
                    abs(expected - best_risk) <= 1e-12 and best_action is not None and tie < best_tie):
                best_risk, best_action, best_tie = expected, action, tie
        return best_risk, best_action

