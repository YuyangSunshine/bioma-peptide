from contextlib import closing
from pathlib import Path
from typing import Any, Literal
import hashlib
import json
import sqlite3
import time

from pydantic import BaseModel, ConfigDict, Field

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


class MemoryAccessGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    authorization_ref: str = Field(min_length=1)
    authorization_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    project: str
    source_task_id: str
    target_task_id: str
    source_owner: str
    recipient: str
    purpose: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_event_hashes: tuple[str, ...] = Field(min_length=1)
    layers: tuple[Literal['semantic', 'procedural'], ...] = ('semantic',)
    expires_at: float


def allows_cross_task(record, owner, project, task_id, purpose, visible_evidence, grants):
    if record.status != 'committed' or not record.source_event or not record.evidence_refs:
        return False
    for grant in grants:
        if not isinstance(grant, MemoryAccessGrant):
            continue
        try:
            raw = Path(grant.authorization_ref).read_bytes()
            if hashlib.sha256(raw).hexdigest() != grant.authorization_sha256:
                continue
            document = json.loads(raw)
            expected = grant.model_dump(mode='json', exclude={'authorization_ref', 'authorization_sha256'})
            if document.get('kind') != 'memory-access-only' or document.get('grant') != expected:
                continue
        except (OSError, ValueError, TypeError):
            continue
        if (grant.project == project == record.project
                and grant.source_task_id == record.task_id
                and grant.target_task_id == task_id
                and grant.source_owner == record.owner
                and grant.recipient == owner
                and grant.purpose == purpose
                and grant.expires_at > time.time()
                and record.layer in grant.layers
                and record.metadata.get('source_event_hash') in grant.source_event_hashes
                and set(record.evidence_refs) <= set(grant.evidence_refs) & set(visible_evidence)):
            return True
    return False


class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, owner TEXT, project TEXT,
                  task_id TEXT, layer TEXT, status TEXT, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS memory_scope ON records(owner,project,task_id,layer);
                CREATE TABLE IF NOT EXISTS consumed(event_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS embeddings(record_id TEXT, model_key TEXT, vector TEXT,
                  PRIMARY KEY(record_id,model_key));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def write(db, record):
        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,body=excluded.body',
            (record.id, record.owner, record.project, record.task_id, record.layer, record.status, record.model_dump_json()))

    def put(self, record):
        with closing(self.connect()) as db, db:
            prior = db.execute('SELECT owner,project,task_id,layer FROM records WHERE id=?', (record.id,)).fetchone()
            if prior and tuple(prior) != (record.owner,record.project,record.task_id,record.layer):
                raise ValueError('Memory identity cannot cross owner/project/task/layer')
            self.write(db, record)

    def candidates(self, owner, project, task_id, *, limit=200, visible_evidence=(), access_grants=(), purpose=""):
        if not owner or not project:
            raise ValueError('Memory recall requires an owner and project')
        with closing(self.connect()) as db:
            rows = db.execute("SELECT body FROM records WHERE owner=? AND project=? AND "
                "task_id=? AND status IN ('committed','provisional','failed') "
                "AND (json_extract(body,'$.expires_at') IS NULL OR json_extract(body,'$.expires_at') > ?) "
                "ORDER BY rowid DESC LIMIT ?", (owner,project,task_id,time.time(),limit)).fetchall()
            # Shared state grants visibility only to memories wholly supported by
            # evidence already in this recipient's local task projection. Never
            # expose another role's working notes or unrelated project history.
            refs = sorted(set(visible_evidence))
            if refs:
                rows += db.execute("SELECT body FROM records WHERE owner<>? AND project=? AND task_id=? "
                    "AND status='committed' AND layer IN ('semantic','procedural') "
                    "AND json_extract(body,'$.source_event') IS NOT NULL "
                    "AND json_array_length(json_extract(body,'$.evidence_refs'))>0 "
                    "AND NOT EXISTS (SELECT 1 FROM json_each(records.body,'$.evidence_refs') r "
                    "WHERE r.value NOT IN (SELECT value FROM json_each(?))) "
                    "AND (json_extract(body,'$.expires_at') IS NULL OR json_extract(body,'$.expires_at')>?) "
                    "ORDER BY rowid DESC LIMIT ?", (owner,project,task_id,json.dumps(refs),time.time(),limit)).fetchall()
            # Same-owner project memory is NOT an implicit cross-task authorization.
            if access_grants:
                rows += db.execute("SELECT body FROM records WHERE project=? AND task_id<>? AND status='committed' AND layer IN ('semantic','procedural') ORDER BY rowid DESC LIMIT ?", (project,task_id,limit)).fetchall()
        records = list({r.id:r for r in (MemoryRecord.model_validate_json(row[0]) for row in rows)}.values())
        records = [r for r in records if r.task_id == task_id or allows_cross_task(
            r, owner, project, task_id, purpose, visible_evidence, access_grants)]
        return [r for r in records if (r.expires_at is None or r.expires_at > time.time())
                and r.metadata.get('review_issue', {}).get('status') != 'REFUTED']

    def invalidate(self, owner, project, evidence_refs, status='disputed'):
        if status not in {'disputed','superseded','retracted'}:
            raise ValueError('Invalid memory invalidation state')
        with closing(self.connect()) as db, db:
            rows = db.execute('SELECT body FROM records WHERE owner=? AND project=?', (owner,project)).fetchall()
            for (body,) in rows:
                record = MemoryRecord.model_validate_json(body)
                if set(record.evidence_refs).intersection(evidence_refs):
                    self.write(db, record.model_copy(update={'status':status}))

    def vector(self, record_id, model_key, value=None):
        with closing(self.connect()) as db, db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)', (record_id,model_key,json.dumps(value)))
                return value
            row = db.execute('SELECT vector FROM embeddings WHERE record_id=? AND model_key=?', (record_id,model_key)).fetchone()
            return json.loads(row[0]) if row else None
