"""Content-addressed local delivery for role-owned documents and job records."""

import hashlib
from pathlib import Path
from uuid import uuid4

from bioma.core.models import Artifact


def materialize_text(base_dir, task_id, source, kind, text, evidence_id, *, suffix, filename=None):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Never use an unchecked task identifier as a relative filesystem path.
    task_folder = hashlib.sha256(task_id.encode()).hexdigest()[:24]
    folder = Path(base_dir).resolve() / "artifacts" / task_folder / source
    if filename is not None:
        from pathlib import PureWindowsPath
        if (not filename or Path(filename).name != filename or PureWindowsPath(filename).name != filename
                or PureWindowsPath(filename).drive or filename in {'.', '..'} or Path(filename).suffix != suffix
                or any(c in filename for c in '<>:"/\\|?*') or filename.endswith((' ', '.'))):
            raise ValueError('Named delivery must be a safe basename with the declared suffix')
        folder = folder / digest
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (filename or (digest + suffix))
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Existing content-addressed artifact was modified.")
    if not path.exists():
        temporary = folder / (uuid4().hex + ".tmp")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return Artifact(
        kind=kind,
        uri=str(path),
        source=source,
        evidence_ids=[evidence_id],
        metadata={
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "task_id": task_id,
        },
    )
