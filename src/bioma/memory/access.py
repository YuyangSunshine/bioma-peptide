"""Trusted-host grants for cross-task recall; never accepted from model messages."""
import time,json,hashlib
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

class MemoryAccessGrant(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    authorization_ref:str=Field(min_length=1)
    authorization_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    project:str
    source_task_id:str
    target_task_id:str
    source_owner:str
    recipient:str
    purpose:str=Field(min_length=1)
    evidence_refs:tuple[str,...]=Field(min_length=1)
    source_event_hashes:tuple[str,...]=Field(min_length=1)
    layers:tuple[Literal['semantic','procedural'],...]=('semantic',)
    expires_at:float


def allows_cross_task(record,owner,project,task_id,purpose,visible_evidence,grants):
    if record.status!='committed' or not record.source_event or not record.evidence_refs:
        return False
    for grant in grants:
        if not isinstance(grant,MemoryAccessGrant):
            continue
        try:
            raw=Path(grant.authorization_ref).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=grant.authorization_sha256:
                continue
            document=json.loads(raw)
            expected=grant.model_dump(mode='json',exclude={'authorization_ref','authorization_sha256'})
            if document.get('kind')!='memory-access-only' or document.get('grant')!=expected:
                continue
        except (OSError,ValueError,TypeError):
            continue
        if (grant.project==project==record.project and grant.source_task_id==record.task_id
            and grant.target_task_id==task_id and grant.source_owner==record.owner
            and grant.recipient==owner and grant.purpose==purpose and grant.expires_at>time.time()
            and record.layer in grant.layers
            and record.metadata.get('source_event_hash') in grant.source_event_hashes
            and set(record.evidence_refs)<=set(grant.evidence_refs)&set(visible_evidence)):
            return True
    return False
