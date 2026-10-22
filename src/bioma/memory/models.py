from typing import Any, Literal
from pydantic import Field
import time
from bioma.core.community import Contract


class MemoryRecord(Contract):
    id: str
    owner: str = Field(min_length=1)
    project: str = Field(min_length=1)
    task_id: str
    layer: Literal['working', 'episodic', 'semantic', 'procedural']
    status: Literal['provisional', 'committed', 'failed', 'disputed', 'superseded', 'retracted']
    text: str
    source_event: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_manifest: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: float | None = None
    created_at: float = Field(default_factory=time.time)


class EmbeddingSpec(Contract):
    model: str
    version: str
    dimension: int = Field(gt=0)
