"""Exact evidence-gated Bayes decision-risk kernel.

This is deliberately independent of the production runtime.  In particular,
``observe_raw`` does not mutate belief: only a qualified evidence symbol can be
passed to ``posterior_update``.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import fsum, log, isfinite
from typing import Callable, Hashable, Mapping, Sequence

Hypothesis = Hashable
Action = Hashable
Raw = Hashable
Evidence = Hashable
Context = Hashable


def _normalized(values: Mapping[Hypothesis, float]) -> dict[Hypothesis, float]:
    if not values or any(not isfinite(v) or v < 0 for v in values.values()):
        raise ValueError("probabilities must be a non-empty non-negative mapping")
    total = fsum(values.values())
    if total <= 0:
        raise ValueError("probabilities have zero mass")
    # A probability distribution's support excludes impossible symbols.  This
    # prevents exact enumeration from attempting a posterior for a zero-mass
    # branch supplied explicitly by a qualification model.
    return {key: value / total for key, value in values.items() if value > 0}


@dataclass(frozen=True)
class BeliefState:
    probabilities: Mapping[Hypothesis, float]

    def __post_init__(self):
        object.__setattr__(self, "probabilities", _normalized(self.probabilities))

    def entropy(self) -> float:
        return -fsum(p * log(p) for p in self.probabilities.values() if p)

    def kl_divergence(self, older: "BeliefState") -> float:
        total = 0.0
        for z, p in self.probabilities.items():
            q = older.probabilities.get(z, 0.0)
            if p and not q:
                return float("inf")
            if p:
                total += p * log(p / q)
        return total

    def predictive_probability(self, likelihood: Mapping[Hypothesis, float]) -> float:
        return fsum(self.probabilities[z] * likelihood.get(z, 0.0)
                    for z in self.probabilities)

    def posterior_update(self, likelihood: Mapping[Hypothesis, float]) -> "BeliefState":
        return BeliefState({z: p * likelihood.get(z, 0.0)
                            for z, p in self.probabilities.items()})


@dataclass(frozen=True)
class DecisionProblem:
    """Declared loss L(d,z); evaluator truth is intentionally absent."""

    losses: Mapping[Hashable, Mapping[Hypothesis, float]]

    def expected_loss(self, decision: Hashable, belief: BeliefState) -> float:
        return fsum(belief.probabilities[z] * self.losses[decision][z]
                    for z in belief.probabilities)

    def best_decision(self, belief: BeliefState) -> Hashable:
        return min(self.losses, key=lambda d: (self.expected_loss(d, belief), repr(d)))

    def bayes_risk(self, belief: BeliefState) -> float:
        return self.expected_loss(self.best_decision(belief), belief)


@dataclass(frozen=True)
class EvidenceChannel:
    """p(R|do(a),z) plus Q(E|R,a,context), kept as distinct channels."""

    raw_model: Callable[[Action, Hypothesis, Context], Mapping[Raw, float]]
    qualification: Callable[[Raw, Action, Context], Mapping[Evidence, float]]

    def raw_distribution(self, action: Action, z: Hypothesis, context: Context) -> dict[Raw, float]:
        return _normalized(self.raw_model(action, z, context))

    def evidence_distribution(self, action: Action, z: Hypothesis,
                              context: Context) -> dict[Evidence, float]:
        result: dict[Evidence, float] = {}
        for raw, p_raw in self.raw_distribution(action, z, context).items():
            for evidence, p_q in _normalized(self.qualification(raw, action, context)).items():
                result[evidence] = result.get(evidence, 0.0) + p_raw * p_q
        return _normalized(result)

    def evidence_likelihood(self, evidence: Evidence, action: Action,
                            hypotheses: Sequence[Hypothesis], context: Context) -> dict[Hypothesis, float]:
        return {z: self.evidence_distribution(action, z, context).get(evidence, 0.0)
                for z in hypotheses}

    def predictive_evidence(self, belief: BeliefState, action: Action,
                            context: Context) -> dict[Evidence, float]:
        result: dict[Evidence, float] = {}
        for z, prior in belief.probabilities.items():
            for evidence, probability in self.evidence_distribution(action, z, context).items():
                result[evidence] = result.get(evidence, 0.0) + prior * probability
        return _normalized(result)


@dataclass(frozen=True)
class EGVoIController:
    problem: DecisionProblem
    channel: EvidenceChannel
    context_transition: Callable[[Context, Action, Evidence], Context] = lambda c, _a, _e: c
    action_cost: Callable[[Action, Context], float] = lambda _a, _c: 0.0

    def update(self, belief: BeliefState, action: Action, evidence: Evidence,
               context: Context) -> BeliefState:
        likelihood = self.channel.evidence_likelihood(
            evidence, action, tuple(belief.probabilities), context)
        return belief.posterior_update(likelihood)

    def diagnostics(self, before: BeliefState, after: BeliefState, action: Action,
                    evidence: Evidence, context: Context) -> dict[str, float]:
        likelihood = self.channel.evidence_likelihood(
            evidence, action, tuple(before.probabilities), context)
        predictive = before.predictive_probability(likelihood)
        return {"predictive_surprise": -log(predictive),
                "belief_shift": after.kl_divergence(before),
                "posterior_entropy": after.entropy()}

    def expected_future_risk(self, belief: BeliefState, action: Action,
                             context: Context) -> float:
        return fsum(p * self.problem.bayes_risk(self.update(belief, action, evidence, context))
                    for evidence, p in self.channel.predictive_evidence(
                        belief, action, context).items())

    def egvoi(self, belief: BeliefState, action: Action, context: Context) -> float:
        return self.problem.bayes_risk(belief) - self.expected_future_risk(belief, action, context)

    def eig(self, belief: BeliefState, action: Action, context: Context) -> float:
        expected_entropy = fsum(
            p * self.update(belief, action, evidence, context).entropy()
            for evidence, p in self.channel.predictive_evidence(belief, action, context).items())
        return belief.entropy() - expected_entropy
