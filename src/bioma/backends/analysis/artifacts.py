from __future__ import annotations

from pathlib import Path


def task_artifact_dir(
    artifact_root: str | Path,
    task_id: str,
    *parts: str,
) -> Path:
    path = Path(artifact_root).resolve() / task_id
    for part in parts:
        path /= part
    path.mkdir(parents=True, exist_ok=True)
    return path
