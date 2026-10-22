"""Versioned conditional prediction; authority stays with trusted resolvers.

No runtime registration occurs on import. No evidence, state, controller, or
qualification predicate is modified here. A caller must install independent
training and model-qualification resolvers; arbitrary payload labels are not an
API input. The numerical candidate is ordinary two-timescale model averaging.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite, log
from typing import Callable, Protocol

from bioma.core.scientific_decision import PredictorCapability


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class ScopeContract:
    scope_id: str
    source_version: str
    capability_version: str
    qualified_acquisition_probability: float
    identifiable: bool = True

    def __post_init__(self):
        if (not self.scope_id or not self.source_version or not self.capability_version
                or not isfinite(self.qualified_acquisition_probability)
                or not 0 < self.qualified_acquisition_probability <= 1):
            raise ValueError("invalid independently declared scope/acquisition contract")


@dataclass(frozen=True)
class VerifiedTrainingRecord:
    reference: str
    evidence_ref: str
    independent_label_ref: str
    scope_id: str
    hypothesis: str
    action: str
    outcome: str | None
    observed_at: int
    verified_at: int
    action_propensity: float
    observation_propensity: float
    label_propensity: float
    missing_at_random_within_stratum: bool
    phase: str = "training"


@dataclass(frozen=True)
class QualificationCertificate:
    model_version: str
    scope_id: str
    contract_hash: str
    validation_refs: tuple[str, ...]
    valid_from: int
    valid_until: int
    validator_id: str


class TrainingAuthority(Protocol):
    def resolve(self, reference: str, *, now: int) -> VerifiedTrainingRecord: ...


class TwoTimescalePosterior:
    """Cumulative/discounted Dirichlet mixture with online predictive weights.

    Discount is per observed stratum, not wall clock. Fixed share permits an
    expert to recover after long stationary periods. Context indexing retains
    old contexts; there is no inferred drift oracle or risk-sensitive sampling.
    """
    name = "two_timescale"

    def __init__(self, outcomes, *, decay=.92, share=.04, adaptive=True, retain_context=True):
        if not 0 < decay < 1 or not 0 <= share <= 1:
            raise ValueError("invalid discount/share")
        self.outcomes = tuple(outcomes)
        self.decay, self.share = decay, share
        self.adaptive, self.retain_context = adaptive, retain_context
        self.cells = {}

    def key(self, scope, z, action):
        return canonical([scope if self.retain_context else "shared", z, action])

    def cell(self, scope, z, action):
        key = self.key(scope, z, action)
        return self.cells.get(key, {"long": [0.] * len(self.outcomes),
                                    "short": [0.] * len(self.outcomes), "weight": .5})

    @staticmethod
    def posterior(counts):
        total = sum(counts) + .5 * len(counts)
        return [(n + .5) / total for n in counts]

    def predict(self, scope, z, action):
        cell = self.cell(scope, z, action)
        p, q = self.posterior(cell["long"]), self.posterior(cell["short"])
        w = cell["weight"] if self.adaptive else .5
        return [w * a + (1 - w) * b for a, b in zip(p, q)]

    def observe(self, row):
        key = self.key(row.scope_id, row.hypothesis, row.action)
        cell = self.cell(row.scope_id, row.hypothesis, row.action)
        i = self.outcomes.index(row.outcome)
        if self.adaptive:
            p, q = self.posterior(cell["long"]), self.posterior(cell["short"])
            w = cell["weight"]
            post = w * p[i] / (w * p[i] + (1 - w) * q[i])
            cell["weight"] = (1 - self.share) * post + self.share / 2
        cell["long"][i] += 1
        cell["short"] = [n * self.decay for n in cell["short"]]
        cell["short"][i] += 1
        self.cells[key] = cell

    def state(self):
        return {"name": self.name, "outcomes": self.outcomes, "decay": self.decay,
                "share": self.share, "adaptive": self.adaptive,
                "retain_context": self.retain_context, "cells": self.cells}


class ContinualScientificPredictor:
    """Removable ScientificPredictor, qualifying each immutable model identity.

    Resolver implementations are part of the trusted application boundary, not
    user-controlled tools. Checkpoints are data, never executable/pickle objects.
    Restore replays every source reference; hashes alone are not authentication.
    """
    predictor_id = "continual-scientific-predictor-v1"
    capability = PredictorCapability.L1_HYPOTHESIS_CONDITIONED

    def __init__(self, *, hypotheses, actions, outcomes, scopes, authority,
                 qualification_resolver: Callable, estimator=None):
        self.hypotheses, self.actions, self.outcomes = map(tuple, (hypotheses, actions, outcomes))
        if any(not x or len(set(x)) != len(x) for x in (self.hypotheses, self.actions, self.outcomes)):
            raise ValueError("domains must be nonempty and unique")
        if "NO_EVIDENCE" in self.outcomes:
            raise ValueError("NO_EVIDENCE belongs to the acquisition contract")
        self.scopes = {x.scope_id: x for x in scopes}
        if len(self.scopes) != len(scopes) or not self.scopes:
            raise ValueError("scope contracts must be unique and nonempty")
        self.authority, self.qualification_resolver = authority, qualification_resolver
        self.estimator = estimator or TwoTimescalePosterior(self.outcomes)
        self._journal, self._pending, self._seen = [], [], set()
        self._events = []
        self._version = self._identity()

    def _identity(self):
        return digest({"schema": self.predictor_id, "hypotheses": self.hypotheses,
            "actions": self.actions, "outcomes": self.outcomes,
            "scopes": {k: asdict(v) for k, v in self.scopes.items()},
            "model": self.estimator.state(), "updates": self._journal[-1]["chain"] if self._journal else "genesis"})

    @property
    def model_version(self):
        return self._version

    def _check(self, row, now, *, phase):
        if row.reference == row.independent_label_ref or row.evidence_ref == row.independent_label_ref:
            raise ValueError("evidence is not an independent hypothesis label")
        if (row.phase != phase or row.verified_at > now or row.observed_at > row.verified_at
                or not row.independent_label_ref or not row.evidence_ref
                or row.hypothesis not in self.hypotheses or row.action not in self.actions
                or row.scope_id not in self.scopes):
            raise ValueError("unverified label, wrong phase, time or domain")
        if (not row.missing_at_random_within_stratum or not self.scopes[row.scope_id].identifiable):
            raise ValueError("conditional likelihood is not identifiable")
        if any(not isfinite(p) or not 0 < p <= 1 for p in (
                row.action_propensity, row.observation_propensity, row.label_propensity)):
            raise ValueError("missing selection/observation/label propensity or zero support")
        if row.outcome not in self.outcomes:
            raise ValueError("raw/unavailable observations cannot train a scientific outcome")

    def observe_verified_training_record(self, reference: str, *, now: int):
        row = self.authority.resolve(reference, now=now)
        if row.reference != reference:
            raise ValueError("source reference mismatch")
        self._check(row, now, phase="training")
        if reference in self._seen or any(r.reference == reference for r, _ in self._pending):
            return False
        self._pending.append((row, now))
        return True

    def record_unavailable(self, *, scope_id, action, reason, action_propensity, now):
        if scope_id not in self.scopes or action not in self.actions or not 0 < action_propensity <= 1:
            raise ValueError("invalid acquisition receipt")
        # Operational trace only: does not touch parameters, evidence or belief.
        self._events.append({"scope_id": scope_id, "action": action, "reason": reason,
                             "action_propensity": action_propensity, "time": now})

    def update_model(self):
        # Re-resolve before applying any member of the batch; no partial update
        # if a source has been revoked or substituted since observation.
        for row, now in self._pending:
            current = self.authority.resolve(row.reference, now=now)
            self._check(current, now, phase="training")
            if current != row:
                raise ValueError("training source changed")
        if not self._pending:
            return self.model_version
        for row, now in self._pending:
            self.estimator.observe(row)
            event = {"record": asdict(row), "consumed_at": now}
            event["chain"] = digest({"previous": self._journal[-1]["chain"] if self._journal else "genesis", **event})
            self._journal.append(event)
            self._seen.add(row.reference)
        self._pending.clear()
        self._version = self._identity()  # external qualification no longer matches
        return self.model_version

    def diagnostic_distribution(self, *, hypothesis, action, scope_id):
        """Unqualified WorkResult data only; never pass directly to live control."""
        if hypothesis not in self.hypotheses or action not in self.actions or scope_id not in self.scopes:
            raise ValueError("predictive domain mismatch")
        p = self.estimator.predict(scope_id, hypothesis, action)
        if len(p) != len(self.outcomes) or any(not isfinite(x) or x <= 0 for x in p) or abs(sum(p)-1) > 1e-9:
            raise ValueError("invalid conditional likelihood")
        q = self.scopes[scope_id].qualified_acquisition_probability
        return {**{e: q * x for e, x in zip(self.outcomes, p)}, "NO_EVIDENCE": 1 - q}

    def predict_evidence(self, *, hypothesis, grounded_action, biological_context, evidence_schema):
        scope = biological_context["scope_id"]
        now = biological_context["time"]
        if scope not in self.scopes or not self.scopes[scope].identifiable:
            raise ValueError("unqualified or unidentified scope")
        contract = self.scopes[scope]
        cert = self.qualification_resolver(self.model_version, scope)
        if (cert is None or cert.model_version != self.model_version or cert.scope_id != scope
                or cert.contract_hash != digest(asdict(contract)) or not cert.validation_refs
                or not cert.validator_id or not cert.valid_from <= now <= cert.valid_until
                or evidence_schema.get("outcomes") != list(self.outcomes)
                or biological_context.get("source_version") != contract.source_version
                or biological_context.get("capability_version") != contract.capability_version):
            raise ValueError("model version/scope lacks independent current qualification")
        return self.diagnostic_distribution(hypothesis=hypothesis, action=grounded_action, scope_id=scope)

    def evaluate_calibration(self, references, *, now):
        """Describe reserved verified data; this cannot grant qualification."""
        scores = []
        for ref in references:
            row = self.authority.resolve(ref, now=now)
            self._check(row, now, phase="validation")
            if ref in self._seen or any(r.reference == ref for r, _ in self._pending):
                raise ValueError("training/validation overlap")
            p = self.estimator.predict(row.scope_id, row.hypothesis, row.action)
            i = self.outcomes.index(row.outcome)
            scores.append((-log(p[i]), sum((x-float(j == i))**2 for j, x in enumerate(p))))
        if not scores:
            raise ValueError("no independent calibration observations")
        return {"n": len(scores), "log_loss": sum(x[0] for x in scores)/len(scores),
                "brier": sum(x[1] for x in scores)/len(scores), "model_version": self.model_version}

    def checkpoint(self):
        if self._pending:
            raise ValueError("flush pending records before checkpoint")
        payload = {"schema": self.predictor_id, "hypotheses": self.hypotheses,
            "actions": self.actions, "outcomes": self.outcomes,
            "scopes": [asdict(x) for x in self.scopes.values()], "model": self.estimator.state(),
            "journal": self._journal, "operational_events": self._events, "version": self.model_version}
        # Detached JSON copy prevents mutation through exported checkpoint objects.
        return json.loads(canonical({"payload": payload, "sha256": digest(payload)}))

    @classmethod
    def restore(cls, checkpoint, *, authority, qualification_resolver):
        p = checkpoint["payload"]
        if checkpoint["sha256"] != digest(p) or p["schema"] != cls.predictor_id:
            raise ValueError("checkpoint integrity/schema mismatch")
        m = p["model"]
        if m["name"] != "two_timescale":
            raise ValueError("unsupported model checkpoint")
        estimator = TwoTimescalePosterior(p["outcomes"], decay=m["decay"], share=m["share"],
            adaptive=m["adaptive"], retain_context=m["retain_context"])
        restored = cls(hypotheses=p["hypotheses"], actions=p["actions"], outcomes=p["outcomes"],
            scopes=[ScopeContract(**s) for s in p["scopes"]], authority=authority,
            qualification_resolver=qualification_resolver, estimator=estimator)
        for event in p["journal"]:
            row = authority.resolve(event["record"]["reference"], now=event["consumed_at"])
            if asdict(row) != event["record"]:
                raise ValueError("checkpoint source replay mismatch")
            restored.observe_verified_training_record(row.reference, now=event["consumed_at"])
            restored.update_model()
        if restored.model_version != p["version"] or canonical(restored.estimator.state()) != canonical(m):
            raise ValueError("checkpoint numerical/update replay mismatch")
        for event in p["operational_events"]:
            restored.record_unavailable(scope_id=event["scope_id"], action=event["action"],
                reason=event["reason"], action_propensity=event["action_propensity"], now=event["time"])
        return restored
