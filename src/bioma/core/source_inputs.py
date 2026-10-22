"""Immutable identities for user-supplied source artifacts.

Admission records what the user supplied.  It does not assert a scientific
claim and it never promotes a later computation to Evidence.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import Field

from bioma.core.models import StrictModel


class SourceIdentity(StrictModel):
    id: str = Field(pattern=r"^source:sha256:[0-9a-f]{64}$")
    uri: str = Field(min_length=1)
    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    authority: str = "user_supplied_input"


def identify_source(path_value: str | Path) -> SourceIdentity:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"User input artifact does not exist: {path}")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"User input artifact is not readable: {path}") from exc
    digest = hashlib.sha256(content).hexdigest()
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return SourceIdentity(id="source:sha256:" + digest, uri=str(path), name=path.name,
        sha256=digest, size_bytes=len(content), media_type=media_type)


def identify_sources(paths: list[str | Path]) -> list[SourceIdentity]:
    rows = [identify_source(path) for path in paths]
    if len({row.uri for row in rows}) != len(rows):
        raise ValueError("Duplicate user input artifact")
    return rows


def verify_source_identities(values: list[dict[str, Any] | SourceIdentity]) -> list[SourceIdentity]:
    expected = [value if isinstance(value, SourceIdentity) else SourceIdentity.model_validate(value)
                for value in values]
    for record in expected:
        current = identify_source(record.uri)
        if current != record:
            raise ValueError(f"User input artifact changed after admission: {record.name}")
    return expected


def scoped_source_refs(parent_inputs, child_inputs):
    """Resolve identical, still-valid user files explicitly admitted by parent."""
    parents=parent_inputs.get('source_inputs',[])
    selected=[]
    for row in child_inputs.get('source_inputs',[]):
        if row not in parents:
            raise ValueError('Child source identity was not admitted by parent')
        selected.append(row)
    return {row.id for row in verify_source_identities(selected)}
