"""Inspectable execution specifications for exact-sequence structure retrieval."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PlanBudget:
    total_deadline_seconds: int
    llm_calls: int
    skill_metadata_queries: int
    skill_content_loads: int
    tool_calls: dict[str, int]


@dataclass(frozen=True)
class PlanStep:
    id: str
    action: str
    kind: str
    timeout_seconds: int
    max_attempts: int
    idempotency_key: str
    expected_evidence: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionPlan:
    id: str
    capability: str
    task_request_id: str | None
    sequence_sha256: str
    steps: tuple[PlanStep, ...]
    budget: PlanBudget

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionCheckpoint:
    plan_id: str
    step_id: str
    idempotency_key: str
    status: str
    observation_hash: str | None = None
    evidence_kinds: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    reason: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def extract_sequence(inputs: dict[str, Any]) -> str | None:
    """Read a declared input; never infer a scientific object from task prose."""
    from bioma.core.sequence_inputs import canonical_sequence

    for candidate in (inputs.get("sequence"), inputs.get("protein_sequence")):
        if sequence := canonical_sequence(candidate):
            return sequence
    return None


def structure_resolution_plan(
    *, sequence: str, task_request_id: str | None,
) -> ExecutionPlan:
    digest = hashlib.sha256(sequence.encode("ascii")).hexdigest()
    plan_id = f"structure-resolution:{digest[:16]}"
    return ExecutionPlan(
        id=plan_id,
        capability="scientific.computational_research",
        task_request_id=task_request_id,
        sequence_sha256=digest,
        steps=(
            PlanStep(
                id="resolve_exact_structure",
                action="resolve_sequence_structure",
                kind="tool",
                timeout_seconds=55,
                max_attempts=2,
                idempotency_key=f"{digest}:reviewed_only=true",
                expected_evidence=("structure_resolution",),
                expected_artifacts=("protein-structure-pdb", "structure-index-json"),
            ),
        ),
        budget=PlanBudget(
            total_deadline_seconds=75,
            llm_calls=0,
            skill_metadata_queries=0,
            skill_content_loads=0,
            tool_calls={"resolve_sequence_structure": 2},
        ),
    )


def checkpoint_for(plan: ExecutionPlan, observation: dict[str, Any]) -> ExecutionCheckpoint:
    rendered = repr(sorted(observation.items())).encode("utf-8")
    return ExecutionCheckpoint(
        plan_id=plan.id,
        step_id="resolve_exact_structure",
        idempotency_key=plan.steps[0].idempotency_key,
        status="succeeded" if observation.get("success") else "failed",
        observation_hash=hashlib.sha256(rendered).hexdigest(),
        evidence_kinds=("structure_resolution",) if observation.get("success") else (),
        artifact_kinds=("protein-structure-pdb", "structure-index-json") if observation.get("success") else (),
        reason=None if observation.get("success") else str(observation.get("error") or "tool returned no success"),
    )
