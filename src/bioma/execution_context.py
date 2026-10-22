from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from bioma.core.models import StrictModel


class TaskExecutionContext(StrictModel):
    """Deterministic filesystem and process context for an external task."""

    task_root: Path
    input_root: Path
    artifact_root: Path
    output_root: Path
    working_directory: Path
    writable_roots: list[Path] = Field(min_length=1)
    relative_input_paths: Literal["input_root"] = "input_root"
    relative_output_paths: Literal["output_root"] = "output_root"

    @model_validator(mode="after")
    def paths_are_bounded(self) -> "TaskExecutionContext":
        self.task_root = self.task_root.resolve()
        for field in ("input_root", "artifact_root", "output_root", "working_directory"):
            value = _within(self.task_root, getattr(self, field), field)
            setattr(self, field, value)
        self.writable_roots = [_within(self.task_root, path, "writable_root")
                               for path in self.writable_roots]
        for required in (self.artifact_root, self.output_root):
            if not any(required == root or root in required.parents for root in self.writable_roots):
                raise ValueError(f"required writable location is not writable: {required}")
        return self

    @classmethod
    def from_mapping(cls, mapping: dict, *, task_root: Path) -> "TaskExecutionContext":
        root = Path(mapping.get("task_root") or task_root).resolve()
        return cls(
            task_root=root,
            input_root=_relative(root, mapping.get("input_root", "inputs")),
            artifact_root=_relative(root, mapping.get("artifact_root", "answer")),
            output_root=_relative(root, mapping.get("output_root", "outputs")),
            working_directory=_relative(root, mapping.get("working_directory", ".")),
            writable_roots=[_relative(root, path) for path in
                            mapping.get("writable_roots", ["answer", "outputs"])],
            relative_input_paths=mapping.get("relative_input_paths", "input_root"),
            relative_output_paths=mapping.get("relative_output_paths", "output_root"),
        )

    def input_path(self, path: str | Path) -> Path:
        return _within(self.task_root, _relative(self.input_root, path), "input path")

    def artifact_path(self, path: str | Path) -> Path:
        return _within(self.task_root, _relative(self.artifact_root, path), "artifact path")

    def output_path(self, path: str | Path) -> Path:
        return _within(self.task_root, _relative(self.output_root, path), "output path")

    def prepare(self) -> None:
        for path in {self.input_root, self.artifact_root, self.output_root,
                     self.working_directory, *self.writable_roots}:
            path.mkdir(parents=True, exist_ok=True)

    def prompt_contract(self) -> str:
        return (
            f"Task root: {self.task_root}\n"
            f"Input root: {self.input_root}\n"
            f"Artifact/source root: {self.artifact_root}\n"
            f"Required output root: {self.output_root}\n"
            f"Process working directory: {self.working_directory}\n"
            f"Writable roots: {', '.join(str(path) for path in self.writable_roots)}\n"
            "Resolve relative input names against the input root. Resolve required output "
            "names against the output root. The generated source file's directory never "
            "changes these rules. The program will execute with the declared process working "
            "directory; do not derive input or output locations from __file__."
        )


class ExecutionResult(StrictModel):
    attempted: bool = True
    success: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def execute_python(context: TaskExecutionContext, program: Path, *, timeout: float = 120) -> ExecutionResult:
    program = _within(context.task_root, program, "program")
    context.prepare()
    environment = dict(os.environ)
    environment.update({
        "BIOMA_TASK_ROOT": str(context.task_root),
        "BIOMA_INPUT_ROOT": str(context.input_root),
        "BIOMA_ARTIFACT_ROOT": str(context.artifact_root),
        "BIOMA_OUTPUT_ROOT": str(context.output_root),
    })
    try:
        result = subprocess.run([sys.executable, str(program)], cwd=context.working_directory,
                                env=environment, capture_output=True, text=True, timeout=timeout)
        return ExecutionResult(success=result.returncode == 0, returncode=result.returncode,
                               stdout=result.stdout[-2000:], stderr=result.stderr[-2000:])
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(success=False, stdout=str(exc.stdout or "")[-2000:],
                               stderr=str(exc.stderr or "")[-2000:], timed_out=True)


def _relative(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _within(root: Path, path: str | Path, label: str) -> Path:
    root = root.resolve()
    candidate = Path(path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"{label} escapes task root: {candidate}")
    return candidate
