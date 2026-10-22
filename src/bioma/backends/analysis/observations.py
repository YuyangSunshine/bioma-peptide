from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from bioma.backends.analysis.models import AnalysisResult, CriterionAssessment, GroupSummary
from bioma.core.models import Observation


class ObservationAnalysisError(ValueError):
    pass


class MissingGroupsError(ObservationAnalysisError):
    def __init__(self, groups: list[str]):
        self.groups = groups
        super().__init__(f"Missing required groups: {groups}.")


class ObservationAnalyzer:
    """Deterministically compare candidate and control measurements."""

    def analyze(
        self,
        observation: Observation,
        success_criteria: dict[str, Any],
    ) -> AnalysisResult:
        # This component only computes statistics; ComAgent and the transition
        # verifier perform authenticated admission before accepting real data.
        if observation.kind not in {"simulated_experiment", "experimental_observation"}:
            raise ObservationAnalysisError(
                f"Unsupported observation kind: {observation.kind!r}."
            )
        if not isinstance(observation.value, dict):
            raise ObservationAnalysisError("Observation value must be an object.")

        metric = _required_text(success_criteria, "metric")
        candidate = _required_text(success_criteria, "candidate_group")
        control = _required_text(success_criteria, "control_group")
        if candidate == control:
            raise ObservationAnalysisError("Candidate and control groups must differ.")
        direction = str(success_criteria.get("direction") or "increase")
        if direction not in {"increase", "decrease"}:
            raise ObservationAnalysisError(
                "direction must be 'increase' or 'decrease'."
            )

        rows = observation.value.get("measurements")
        if not isinstance(rows, list) or not rows:
            raise ObservationAnalysisError("Observation contains no measurement rows.")
        grouped: dict[str, list[float]] = defaultdict(list)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ObservationAnalysisError(
                    f"Measurement row {index} must be an object."
                )
            group = str(row.get("sample") or row.get("group") or "").strip()
            if not group:
                raise ObservationAnalysisError(
                    f"Measurement row {index} has no sample/group."
                )
            value = row.get(metric)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ObservationAnalysisError(
                    f"Measurement row {index} has no numeric {metric!r}."
                )
            number = float(value)
            if not math.isfinite(number):
                raise ObservationAnalysisError(
                    f"Measurement row {index} has a non-finite {metric!r}."
                )
            grouped[group].append(number)

        missing = [name for name in (candidate, control) if name not in grouped]
        if missing:
            raise MissingGroupsError(missing)

        summaries = [
            _summarize(group, metric, values)
            for group, values in sorted(grouped.items())
        ]
        means = {item.group: item.mean for item in summaries}
        raw_change = means[candidate] - means[control]
        improvement = raw_change if direction == "increase" else -raw_change
        relative_change = None
        warnings: list[str] = []
        if means[control] == 0:
            warnings.append("relative_change_unavailable_for_zero_control")
        else:
            relative_change = improvement / abs(means[control])

        criterion, threshold, observed = _criterion(
            success_criteria,
            candidate_mean=means[candidate],
            absolute_improvement=improvement,
            relative_improvement=relative_change,
        )
        return AnalysisResult(
            observation_id=observation.id,
            metric=metric,
            candidate_group=candidate,
            control_group=control,
            summaries=summaries,
            absolute_change=raw_change,
            relative_change=relative_change,
            assessment=CriterionAssessment(
                direction=direction,
                criterion=criterion,
                threshold=threshold,
                observed_improvement=observed,
                meets_criteria=observed >= threshold,
            ),
            warnings=warnings,
        )


def _summarize(group: str, metric: str, values: list[float]) -> GroupSummary:
    return GroupSummary(
        group=group,
        metric=metric,
        n=len(values),
        mean=statistics.fmean(values),
        standard_deviation=statistics.stdev(values) if len(values) > 1 else None,
    )


def _criterion(
    criteria: dict[str, Any],
    *,
    candidate_mean: float,
    absolute_improvement: float,
    relative_improvement: float | None,
) -> tuple[str, float, float]:
    supported = [
        name
        for name in (
            "minimum_relative_improvement",
            "minimum_absolute_improvement",
            "minimum_candidate_value",
        )
        if name in criteria
    ]
    if len(supported) != 1:
        raise ObservationAnalysisError(
            "Success criteria must specify exactly one of "
            "minimum_relative_improvement, minimum_absolute_improvement, or "
            "minimum_candidate_value."
        )
    name = supported[0]
    threshold = criteria[name]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ObservationAnalysisError(f"{name} must be numeric.")
    if not math.isfinite(float(threshold)):
        raise ObservationAnalysisError(f"{name} must be finite.")
    if name == "minimum_relative_improvement":
        if relative_improvement is None:
            raise ObservationAnalysisError(
                "Relative improvement cannot be evaluated with a zero control mean."
            )
        observed = relative_improvement
    elif name == "minimum_absolute_improvement":
        observed = absolute_improvement
    else:
        observed = candidate_mean
    return name, float(threshold), observed


def _required_text(values: dict[str, Any], key: str) -> str:
    value = str(values.get(key) or "").strip()
    if not value:
        raise ObservationAnalysisError(f"Success criteria require {key!r}.")
    return value
