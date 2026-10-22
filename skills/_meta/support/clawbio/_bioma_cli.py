"""Shared subprocess adapter for ClawBio skill entry points."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
from pathlib import Path
from typing import Any


def existing_file(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    return path


def existing_directory(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"{label} does not exist or is not a directory: {path}")
    return path


def output_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"required executable is not installed or not on PATH: {name}")
    return executable


def _result_summary(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            summary[key] = value
        elif isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif key in {"summary", "metadata", "qc_summary", "classification_counts"} and isinstance(value, dict):
            summary[key] = {
                child_key: child_value
                for child_key, child_value in value.items()
                if child_value is None or isinstance(child_value, (str, int, float, bool))
            }
    return summary


def run_cli(script: Path, arguments: list[str], output_dir: Path | None = None,
            timeout: int = 43200, extra_env: dict[str, str] | None = None,
            working_dir: Path | None = None) -> str:
    interpreter = shutil.which("bash") if script.suffix == ".sh" else sys.executable
    command = [str(interpreter), str(script), *arguments]
    environment = os.environ.copy()
    environment["PATH"] = str(Path(sys.executable).resolve().parent) + os.pathsep + environment.get("PATH", "")
    if extra_env:
        environment.update(extra_env)
    try:
        process = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=environment, cwd=working_dir)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{script.stem} timed out after {timeout} seconds") from exc
    if process.returncode:
        detail = "\n".join(part for part in (process.stdout, process.stderr) if part)
        raise RuntimeError(f"{script.stem} failed with exit code {process.returncode}: {detail[-16000:]}")

    files: list[str] = []
    result: Any = None
    if output_dir and output_dir.is_dir():
        files = sorted(
            str(path.relative_to(output_dir))
            for path in output_dir.rglob("*")
            if path.is_file()
        )
        result_path = output_dir / "result.json"
        if result_path.is_file() and result_path.stat().st_size <= 5_000_000:
            try:
                result = _result_summary(json.loads(result_path.read_text(encoding="utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                result = None
    return json.dumps({
        "script": str(script),
        "output_dir": str(output_dir) if output_dir else None,
        "files": files[:200],
        "result_summary": result,
        "stdout_tail": process.stdout[-4000:],
        "stderr_tail": process.stderr[-2000:],
    }, ensure_ascii=False)
