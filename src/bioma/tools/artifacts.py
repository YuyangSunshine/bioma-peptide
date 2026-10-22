from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from bioma.tools.base import Tool, ToolInput


class WriteArtifactInput(ToolInput):
    path: str = Field(description="Relative output path under the artifact root.")
    content: str
    artifact_type: str = "text"
    description: str = ""


class ListArtifactsInput(ToolInput):
    limit: int = Field(default=20, ge=1, le=1000)


def build_artifact_tools(base_dir: Path) -> tuple[Tool, Tool]:
    artifact_root = (base_dir / "artifacts").resolve()
    manifest_path = artifact_root / "manifest.json"

    def write_artifact(
        path: str,
        content: str,
        artifact_type: str = "text",
        description: str = "",
    ) -> str:
        target = _safe_target(artifact_root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        data = content.encode("utf-8")
        record = {
            "path": str(target.relative_to(base_dir)),
            "type": artifact_type,
            "description": description,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        records = _read_manifest(manifest_path)
        records.append(record)
        manifest_path.write_text(
            json.dumps({"artifacts": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return json.dumps(record, ensure_ascii=False)

    def list_artifacts(limit: int = 20) -> str:
        return json.dumps(
            _read_manifest(manifest_path)[-limit:],
            ensure_ascii=False,
            indent=2,
        )

    return (
        Tool(
            name="write_artifact",
            description="Write a reproducible research artifact under the BioMA artifact root.",
            category="artifact",
            version="1.0.0",
            author="BioMA",
            func=write_artifact,
            args_schema=WriteArtifactInput,
            tags=("artifact", "write"),
        ),
        Tool(
            name="list_artifacts",
            description="List artifacts recorded in the current BioMA workspace.",
            category="artifact",
            version="1.0.0",
            author="BioMA",
            func=list_artifacts,
            args_schema=ListArtifactsInput,
            tags=("artifact", "list"),
        ),
    )


def _safe_target(root: Path, requested: str) -> Path:
    relative = Path(requested)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Artifact path must be relative and cannot traverse parents.")
    target = (root / relative).resolve()
    target.relative_to(root)
    if target.name in {".env", "keys.env"}:
        raise ValueError("Secret-like artifact paths are not allowed.")
    return target


def _read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    records = payload.get("artifacts") if isinstance(payload, dict) else None
    return records if isinstance(records, list) else []
