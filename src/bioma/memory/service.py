"""Working/episodic lifecycle and idempotent consolidation from committed ledger events."""
from contextlib import closing
import time
from bioma.core.research import fingerprint
from bioma.memory.models import MemoryRecord
from bioma.memory.store import MemoryStore
from bioma.memory.retrieval import MemoryRetriever


class MemoryService:
    def __init__(self, path, *, project=None, embedding=None, embedding_spec=None, access_grants=(), purpose=""):
        self.store = MemoryStore(path)
        self.project = project or fingerprint(str(self.store.path.resolve().parent))
        self.retriever = MemoryRetriever(self.store, embedding=embedding, spec=embedding_spec)
        self.access_grants = tuple(access_grants)
        self.purpose = purpose
        self._cursors = {}

    def context(self, owner, state, message):
        recalled = self.retriever.search(owner,self.project,state.task_id,
            message.payload.get('assignment_objective') or state.objective,
            visible_evidence=[e.id for e in state.evidence],
            access_grants=self.access_grants, purpose=self.purpose)
        return {**recalled,'owner':owner,'project':self.project,'task_id':state.task_id,
            'assignment_id':message.task_request_id or message.id,'state_version':state.version,
            'objective':message.payload.get('assignment_objective') or state.objective}

    def begin(self, proposal, context):
        now = time.time()
        for layer in ('working','episodic'):
            self.store.put(MemoryRecord(id=fingerprint([proposal.identity,layer]),owner=proposal.proposer,
                project=self.project,task_id=proposal.task_id,layer=layer,status='provisional',
                text=context.get('objective', '') + '\nAction: ' + proposal.action + ' ' + str(proposal.method_id),created_at=now,
                expires_at=now+86400 if layer=='working' else None,
                evidence_refs=proposal.supporting_evidence,metadata={'proposal_id':proposal.identity,
                    'read_set':proposal.read_set,'memory_refs':proposal.memory_refs}))

    def fail(self, proposal, reason):
        now = time.time()
        episode = MemoryRecord(id=fingerprint([proposal.identity,'episodic']),owner=proposal.proposer,
            project=self.project,task_id=proposal.task_id,layer='episodic',status='failed',
            text=proposal.action + ': ' + reason[:2000],created_at=now,
            evidence_refs=proposal.supporting_evidence,
            metadata={'proposal_id':proposal.identity,'authority':'attempt_only'})
        working = MemoryRecord(id=fingerprint([proposal.identity,'working']),owner=proposal.proposer,
            project=self.project,task_id=proposal.task_id,layer='working',status='superseded',
            text='Assignment attempt failed',created_at=now,expires_at=now,
            metadata={'proposal_id':proposal.identity})
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT status FROM records WHERE id=?',(episode.id,)).fetchone()
            if prior and prior[0] in {'committed','disputed','retracted'}:
                return  # A late failure notification cannot undo an approved commit.
            self.store.write(db, episode)
            self.store.write(db, working)

    def sync(self, ledger, run_id):
        key = (str(ledger.path.resolve()),run_id)
        if not ledger.verify_chain(run_id):
            raise ValueError('Memory sync requires a valid ledger chain')
        for event in ledger.events(run_id, after_sequence=self._cursors.get(key,0)):
            if event.kind == 'TransitionCommitted':
                self.consume(event)
            self._cursors[key] = event.sequence

    def consume(self, event):
        if event.kind != 'TransitionCommitted' or event.payload.get('receipt',{}).get('decision') != 'approved':
            raise ValueError('Memory consolidation requires an approved committed event')
        delta = event.payload['delta']
        owner = delta['agent_id']
        transition = delta.get('transition') or {}
        proposal_id = (transition.get('permit') or {}).get('proposal_id')
        qualification = (not proposal_id and owner == 'harness' and
            event.payload.get('message', {}).get('message_type') == 'runtime.evidence_qualification')
        if not proposal_id and not qualification:
            return  # Terminal events are not invented agent experiences.
        now = time.time()
        records = []
        if proposal_id:
            records = [MemoryRecord(id=fingerprint([proposal_id,'episodic']), owner=owner,
                project=self.project,task_id=event.run_id,layer='episodic',status='committed',
                source_event=event.event_id,created_at=now,evidence_refs=[e['id'] for e in delta['evidence']],
                text=event.payload['before']['objective'] + '\nWork result: ' + transition['result']['work_outcome'],
                metadata={'proposal_id':proposal_id,'source_event_hash':event.event_hash,
                          'work_outcome':transition['result']['work_outcome']})]
            records.append(MemoryRecord(id=fingerprint([proposal_id,'working']),owner=owner,
                project=self.project,task_id=event.run_id,layer='working',status='superseded',
                source_event=event.event_id,created_at=now,text='Assignment committed',expires_at=now))
        for evidence in delta['evidence']:
            if evidence['kind'] in {'research_assessment','evidence_gap','execution_failure'}:
                continue
            value = evidence.get('value') or {}
            manifest = [{'uri':a['uri'],'sha256':a.get('metadata',{}).get('sha256')}
                        for a in delta['artifacts'] if evidence['id'] in a['evidence_ids']]
            if qualification:
                reference = value.get('work_result_ref')
                work = next((o.get('value', {}) for o in event.payload['before'].get('observations', [])
                    if o.get('id') == reference and o.get('kind') == 'work_result'), {})
                manifest += [{'uri':a['uri'],'sha256':a.get('metadata',{}).get('sha256')}
                    for a in work.get('artifacts', [])]
            if any(not a['sha256'] for a in manifest):
                continue
            records.append(MemoryRecord(id=fingerprint([event.event_id,evidence['id'],'semantic']),owner=owner,
                project=self.project,task_id=event.run_id,layer='semantic',status='committed',created_at=now,
                source_event=event.event_id,evidence_refs=[evidence['id']],artifact_manifest=manifest,
                text=evidence['claim'] + '\nConditions: ' + event.payload['before']['objective'],
                metadata={'data_kind':value.get('evidence_package',{}).get('data_kind') if isinstance(value,dict) else None,
                          'source_event_hash':event.event_hash,'authority':'context_only',
                          'qualification_model':value.get('qualification_model'),
                          'scientific_support':value.get('goal_verification',{}).get('finding',{}).get('status','NOT_VERIFIABLE')}))
        if proposal_id:
            records.append(MemoryRecord(id=fingerprint([proposal_id,'procedural']),owner=owner,project=self.project,
                task_id=event.run_id,layer='procedural',status='committed',created_at=now,source_event=event.event_id,
                evidence_refs=[e['id'] for e in delta['evidence']],
                text=event.payload['before']['objective'] + '\nMethod experience: ' + str(transition['proposal'].get('method_id')) + '; outcome: ' + transition['result']['work_outcome'],
                metadata={'capability_version':transition['proposal']['capability_version'],'authority':'experience_not_skill_registration'}))
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM consumed WHERE event_id=?',(event.event_id,)).fetchone():
                return
            for record in records:
                self.store.write(db,record)
            disputed = set()
            for evidence in delta['evidence']:
                value = evidence.get('value')
                if isinstance(value,dict) and isinstance(value.get('disputes'), list):
                    known = {e['id'] for e in event.payload['before']['evidence']}
                    disputed.update(x for x in value['disputes'] if isinstance(x,str) and x in known)
            if disputed:
                rows = db.execute('SELECT body FROM records WHERE project=?',(self.project,)).fetchall()
                for (body,) in rows:
                    record = MemoryRecord.model_validate_json(body)
                    if disputed.intersection(record.evidence_refs):
                        self.store.write(db,record.model_copy(update={'status':'disputed'}))
            db.execute('INSERT INTO consumed VALUES(?)',(event.event_id,))
