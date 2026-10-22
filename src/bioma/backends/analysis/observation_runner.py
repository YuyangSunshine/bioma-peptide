from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bioma.backends.analysis.observations import ObservationAnalyzer
from bioma.backends.analysis.artifacts import task_artifact_dir
from bioma.backends.analysis.models import AnalysisResult
from bioma.core.models import Observation


@dataclass(frozen=True)
class ObservationRunResult:
    analysis: AnalysisResult
    artifact_path: Path
    stages: tuple[str, ...]


class ObservationAnalysisRunner:
    """Small deterministic harness for observation-to-evidence analysis."""

    def __init__(
        self, base_dir: Path, analyzer: ObservationAnalyzer | None = None
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.analyzer = analyzer or ObservationAnalyzer()

    def run(
        self,
        task_id: str,
        observation: Observation,
        success_criteria: dict,
    ) -> ObservationRunResult:
        stages = ("inspect_observation", "summarize_measurements", "assess_criterion")
        analysis = self.analyzer.analyze(observation, success_criteria)
        artifact_dir = task_artifact_dir(
            self.base_dir / "artifacts", task_id, "comagent"
        )
        artifact_path = artifact_dir / f"analysis-{observation.id}.json"
        artifact_path.write_text(
            analysis.model_dump_json(indent=2),
            encoding="utf-8",
        )
        json.loads(artifact_path.read_text(encoding="utf-8"))
        return ObservationRunResult(
            analysis=analysis, artifact_path=artifact_path, stages=stages
        )
