"""Small, opt-in context packets. References provide access, never scientific authority."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib,json,time
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

class NeedsMoreContext(ValueError):
    status='NEEDS_MORE_CONTEXT'

class SourceRef(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid')
    id:str
    uri:str
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    version:str
    task_id:str
    purpose:str
    roles:tuple[str,...]
    scope:str

class SourceCatalog:
    """Sources must be explicitly admitted by the trusted host, not discovered by path."""
    def __init__(self,refs):
        refs=list(refs)
        self.refs={r.id:r for r in refs}
        if len(self.refs)!=len(refs):raise ValueError('Duplicate source IDs')
    def verify(self,ref,*,task_id,role,purpose):
        source=self.refs.get(ref)
        if source is None or (source.task_id,source.purpose)!=(task_id,purpose) or role not in source.roles:
            raise NeedsMoreContext('Reference is not authorized for this task, role and purpose')
        try:
            raw=Path(source.uri).read_bytes()
        except OSError as exc:
            raise NeedsMoreContext('Reference unavailable') from exc
        if hashlib.sha256(raw).hexdigest()!=source.sha256:
            raise NeedsMoreContext('Reference content hash changed')
        return source,raw

    def read(self,ref,*,task_id,role,purpose,offset=0,limit=None):
        source,raw=self.verify(ref,task_id=task_id,role=role,purpose=purpose)
        if type(offset) is not int or offset<0 or (limit is not None and (type(limit) is not int or limit<1)):
            raise NeedsMoreContext('Invalid reference read range')
        try:text=raw.decode('utf-8-sig')
        except UnicodeError as exc:raise NeedsMoreContext('Binary source requires a typed reader; use its verified extracted record') from exc
        end=len(text) if limit is None else min(len(text),offset+limit)
        return {'ref':ref,'sha256':source.sha256,'version':source.version,'scope':source.scope,
            'offset':offset,'content':text[offset:end],'next_offset':end if end<len(text) else None,
            'complete':offset==0 and end==len(text)}

class ReviewIssue(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str
    source_ref:str
    source_pointer:str
    original_location:str
    original_message:str
    status:Literal['PENDING_CHECK','CONFIRMED','REFUTED','UNRESOLVED']='PENDING_CHECK'
    verification_refs:list[str]=Field(default_factory=list)
    rationale:str=''
    history:list[dict]=Field(default_factory=list)
    def adjudicate(self,status,refs,rationale):
        if status not in {'CONFIRMED','REFUTED','UNRESOLVED'}:raise ValueError('Invalid review issue status')
        if status in {'CONFIRMED','REFUTED'} and not refs:raise ValueError('Adjudication requires source references')
        return self.model_copy(update={'status':status,'verification_refs':list(refs),'rationale':rationale,
            'history':[*self.history,{'previous':self.status,'status':status,'verification_refs':list(refs),'rationale':rationale}]})

class ContextPacket:
    """Mandatory active information is never silently truncated or similarity-ranked away."""
    required={'original_goal','research_plan','execution_state','latest_review','pending_code','budget','legal_actions'}
    def __init__(self,catalog,*,task_id,purpose,active_refs,history_refs=()):
        self.catalog,self.task_id,self.purpose=catalog,task_id,purpose
        self.active_refs=dict(active_refs);self.history_refs=tuple(history_refs)
        if not self.required<=self.active_refs.keys():raise NeedsMoreContext('Missing mandatory active context')
    def assemble(self,role):
        active={name:self.catalog.read(ref,task_id=self.task_id,role=role,purpose=self.purpose)
            for name,ref in self.active_refs.items()}
        references=[]
        for ref in self.history_refs:
            # Verify every referenced source now, even if its full text is not injected.
            self.catalog.verify(ref,task_id=self.task_id,role=role,purpose=self.purpose)
            references.append(self.catalog.refs[ref].model_dump(mode='json'))
        return {'context_version':'scientific-memory-v1','task_id':self.task_id,'model_role':role,
            'purpose':self.purpose,'active':active,'history_references':references,
            'authority':'Context and review opinions are not Qualified Evidence; pending and refuted records retain their status.',
            'read_protocol':'read_context(ref,offset,limit); missing/unauthorized/changed content returns NEEDS_MORE_CONTEXT, never a scientific judgment'}
    def expand(self,ref,role,offset=0,limit=4000):
        if ref not in {*self.active_refs.values(),*self.history_refs}:raise NeedsMoreContext('Reference outside current packet')
        if limit>8000:raise NeedsMoreContext('Page limit exceeded')
        return self.catalog.read(ref,task_id=self.task_id,role=role,purpose=self.purpose,offset=offset,limit=limit)

class ScientificContextChat:
    """Install around an existing budgeted ChatProvider; all expansion calls use it too."""
    def __init__(self,inner,packet,role,log_path,estimate,max_reads=3):
        self.inner,self.packet,self.role=inner,packet,role
        self.log_path=Path(log_path);self.estimate=estimate;self.max_reads=max_reads
    def __getattr__(self,key):return getattr(self.inner,key)
    def _call(self,messages,tools,kwargs):
        from bioma.memory.context import augment_messages,prepared_context
        packet=self.packet.assemble(self.role)
        current=[*messages,{'role':'user','content':json.dumps(packet,ensure_ascii=False)}]
        for turn in range(self.max_reads+1):
            final=augment_messages(current)
            row={'role':self.role,'task_id':self.packet.task_id,'context_version':packet['context_version'],'snapshot_id':packet.get('snapshot_id'),
                'sources':[r.model_dump(mode='json') for r in self.packet.catalog.refs.values()],
                'message_sha256':hashlib.sha256(json.dumps(final,sort_keys=True).encode()).hexdigest(),
                'estimated_input_tokens':self.estimate(json.dumps({'messages':final,'tools':tools},ensure_ascii=False)),
                'started':time.time(),'status':'STARTED'}
            self.log_path.parent.mkdir(parents=True,exist_ok=True)
            def record(value):
                with self.log_path.open('a') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
            record(row)
            events=[]
            from bioma.runtime.telemetry import observe
            try:
                with prepared_context(final),observe(events.append):
                    result=(self.inner.chat(final,**kwargs) if tools is None else self.inner.chat_with_tools(final,tools,**kwargs))
                raw=result if isinstance(result,str) else result.content
                row['actual_usage']=next((e.get('usage') for e in reversed(events) if e.get('type')=='llm.completed'),None)
                row['status']='RESPONSE_RECEIVED'
            except Exception as exc:
                row.update(status='FAILED',error_type=type(exc).__name__,actual_usage=None);raise
            finally:
                row['ended']=time.time();record(row)
            try:value=json.loads(raw)
            except (ValueError,TypeError):return result
            if not isinstance(value,dict) or 'read_context' not in value:return result
            if turn==self.max_reads:raise NeedsMoreContext('Bounded context read allowance exhausted')
            try:
                detail=self.packet.expand(role=self.role,**value['read_context'])
            except (NeedsMoreContext,TypeError) as exc:
                record({'status':'NEEDS_MORE_CONTEXT','role':self.role,'task_id':self.packet.task_id,
                        'requested_reference':value['read_context'],'error_type':type(exc).__name__})
                raise NeedsMoreContext('Requested evidence cannot be expanded; no scientific judgment made') from exc
            current.extend([{'role':'assistant','content':raw},{'role':'user','content':json.dumps(detail)}])
        raise NeedsMoreContext('No complete response')
    def chat(self,messages,**kwargs):return self._call(messages,None,kwargs)
    def chat_with_tools(self,messages,tools,**kwargs):return self._call(messages,tools,kwargs)
