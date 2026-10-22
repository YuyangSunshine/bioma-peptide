"""Reconstruct scientific belief from committed evidence."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Hashable, Mapping
from .kernel import BeliefState

@dataclass(frozen=True)
class DerivedBelief:
    state: BeliefState
    evidence_state_version: int
    evidence_refs: tuple[str, ...]
    inference_model_id: str

    @property
    def version(self) -> str:
        return f"belief:{self.inference_model_id}:{self.evidence_state_version}:" + ",".join(self.evidence_refs)


@dataclass(frozen=True)
class EvidenceLikelihoodModel:
    model_id: str
    likelihood_by_evidence_id: Mapping[str, Mapping[Hashable, float]]


class EvidenceBeliefBridge:
    """Derive belief from committed evidence without interpreting relation labels."""

    def derive(self, evidence_state, prior: BeliefState,
               model: EvidenceLikelihoodModel) -> DerivedBelief:
        belief = prior
        consumed = []
        committed_ids = {item.id for item in evidence_state.evidence}
        unknown = set(model.likelihood_by_evidence_id) - committed_ids
        if unknown:
            raise ValueError("inference model references non-committed evidence: " + repr(sorted(unknown)))
        for item in evidence_state.evidence:
            likelihood = model.likelihood_by_evidence_id.get(item.id)
            if likelihood is None:
                continue  # unrelated committed evidence is not assigned invented semantics
            if set(likelihood) != set(prior.probabilities):
                raise ValueError("likelihood hypothesis domain differs from prior")
            belief = belief.posterior_update(likelihood)
            consumed.append(item.id)
        return DerivedBelief(belief, evidence_state.version, tuple(consumed), model.model_id)


