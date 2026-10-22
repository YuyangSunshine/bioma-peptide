from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def source_completeness(root: str | Path) -> dict[str, Any]:
    """Check importable sourceless modules separately from ignored PEP 3147 caches.

    Without a custom bytecode finder, an orphan in __pycache__ is not an
    importable implementation. Report that generated debris without treating
    it as a missing release source file.
    """
    root = Path(root).resolve()
    missing = []
    stale_caches = []
    for pyc in root.rglob("*.pyc"):
        if "__pycache__" in pyc.parts:
            stem = pyc.name.split(".cpython-")[0]
            source = pyc.parent.parent / f"{stem}.py"
        else:
            source = pyc.with_suffix(".py")
        if not source.is_file():
            target = stale_caches if "__pycache__" in pyc.parts else missing
            target.append(str(pyc.relative_to(root)))
    return {"complete": not missing, "bytecode_without_source": sorted(missing),
            "stale_bytecode_caches": sorted(stale_caches)}


def release_manifest(project_root: str | Path, *, config_path: str = "configs/bioma.yaml") -> dict[str, Any]:
    root = Path(project_root).resolve()
    included = []
    roots = [root / "src" / "bioma", root / "configs", root / "tools" / "our_tools"]
    for data in (root / "src" / "bioma" / "backends" / "automation" / "resources" / "registry").glob("*"):
        if data.is_file():
            roots.append(data)
    files = set()
    for item in roots:
        if item.is_file(): files.add(item)
        elif item.exists(): files.update(path for path in item.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    for path in sorted(files):
        raw = path.read_bytes()
        included.append({"path": path.relative_to(root).as_posix(), "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    config = root / config_path
    config_fingerprint = hashlib.sha256(config.read_bytes()).hexdigest()
    manifest_body = {"schema": "bioma-source-release-v1", "config_path": config_path, "config_sha256": config_fingerprint, "files": included}
    manifest_body["release_sha256"] = hashlib.sha256(json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return manifest_body


def write_release_manifest(project_root: str | Path, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(release_manifest(project_root), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
