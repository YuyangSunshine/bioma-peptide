"""Shared harness types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Type

from pydantic import BaseModel

from bioma.backends.analysis.artifacts import task_artifact_dir
from bioma.tools.base import sanitize_parameters_schema


class HarnessToolInput(BaseModel):
    """Base class for ComAgent harness tool inputs."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid"}


@dataclass
class ToolObservation:
    """Compact result returned to the agent after a tool call."""

    tool_name: str
    status: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    patch: dict[str, Any] = field(default_factory=dict)
    # A source observation of an executed package, not a separately writable catalog.
    execution_package: dict[str, Any] | None = None

    def to_agent_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "status": self.status,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "tables": self.tables,
            "figures": self.figures,
            "metrics": self.metrics,
            "warnings": self.warnings,
        }

    def to_agent_json(self) -> str:
        return json.dumps(self.to_agent_dict(), ensure_ascii=False, default=str)


@dataclass
class HarnessContext:
    """Mutable state owned by one ComAgent harness run."""

    state: dict[str, Any]
    base_dir: Path
    observations: list[dict[str, Any]] = field(default_factory=list)
    analysis_draft: dict[str, Any] | None = None

    @property
    def task_id(self) -> str:
        return str(self.state.get("task_id") or self.state.get("thread_id") or "task")

    def artifact_dir(self, *parts: str) -> Path:
        return task_artifact_dir(
            self.state.get("artifact_root") or self.base_dir / "artifacts",
            str(self.state.get('artifact_scope_id') or self.task_id),
            "harness",
            *(part for part in parts if part),
        )

    def merge_patch(self, patch: dict[str, Any]) -> None:
        for key, value in (patch or {}).items():
            if key == "stage_log":
                self.state[key] = list(self.state.get(key) or []) + list(value or [])
            else:
                self.state[key] = value

    def record_observation(self, observation: ToolObservation) -> None:
        if (observation.execution_package is None and observation.status == "ok"
                and observation.tool_name in {"execute_analysis_code", "revise_analysis_code"}
                and observation.metrics.get("receipt_path")):
            from dataclasses import asdict
            observation.execution_package = asdict(observation)
        self.merge_patch(observation.patch)
        item = observation.to_agent_dict()
        if observation.execution_package is not None:
            item["execution_package"] = observation.execution_package
        self.observations.append(item)
        self.state["harness_observations"] = list(self.observations)


@dataclass
class HarnessTool:
    """Tool definition used by the ComAgent harness loop."""

    name: str
    description: str
    handler: Callable[[HarnessContext, dict[str, Any]], ToolObservation]
    args_schema: Type[HarnessToolInput] | None = None
    tags: list[str] = field(default_factory=list)
    version: str = "unversioned"
    implementation_identity: str | None = None
    result_kind: str = "unclassified"
    source_tool: Any | None = None

    def to_openai_tool(self) -> dict[str, Any]:
        parameters = (
            self.args_schema.model_json_schema()
            if self.args_schema is not None
            else {"type": "object", "properties": {}}
        )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": sanitize_parameters_schema(parameters),
            },
        }

    def call(self, context: HarnessContext, args: dict[str, Any]) -> ToolObservation:
        grant = None
        if self.source_tool is not None:
            from bioma.capabilities.scoped import authorize_tool_invocation
            grant = authorize_tool_invocation(self.source_tool, args or {})
        from bioma.runtime.admission import check_execution
        check_execution(self.source_tool)
        if self.args_schema is not None:
            validated = self.args_schema.model_validate(args or {})
            args = validated.model_dump()
        from bioma.runtime.tool_actions import invoke
        return invoke(self.name, args, lambda: self.handler(context, args),
                      version=self.version,
                      tool_grant_ref=grant.identity if grant is not None else None,
                      tool_grant_context=grant.receipt_context() if grant is not None else None)


@dataclass
class HarnessLoopResult:
    answer: str
    messages: list[dict] = field(default_factory=list)
    exit_reason: str = "completed"
