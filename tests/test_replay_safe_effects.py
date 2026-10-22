import pytest

from bioma.runtime.participant import RuntimeParticipant
from bioma.core.harness import runtime_recovery_action
from bioma.core.models import ScientificTask, StateDelta, Evidence, ScientificState, AgentMessage
from bioma.runtime.effects import EffectManager, EffectUncertain, effect_scope
from bioma.runtime.engine import DecentralizedRuntime
from bioma.runtime.execution import DurableExecution
from bioma.runtime.ledger import RunLedger
from bioma.tools.base import Tool
from bioma.tools.errors import ToolError


def tool(action, *, safe=True, reconcile=None):
    return Tool('lookup', '', 'test', '1', 'test', action,
                external_effect=True, replay_safe=safe, reconcile_effect=reconcile)


def test_safe_error_is_preserved_and_retry_is_cached_after_success(tmp_path):
    ledger=RunLedger(tmp_path/'run.db');calls=[]
    error=ToolError('service unavailable',error_type='unavailable',retriable=True)
    def action():
        calls.append(1)
        if len(calls)==1:raise error
        return {'value':42}
    lookup=tool(action)
    with effect_scope(EffectManager(ledger),'r'),pytest.raises(ToolError) as caught:lookup.call()
    assert caught.value is error
    with effect_scope(EffectManager(RunLedger(ledger.path)),'r'):
        assert lookup.call()==lookup.call()=={'value':42}
    assert len(calls)==2
    events=ledger.events('r')
    assert [e.payload['attempt'] for e in events if e.kind=='EffectStarted']==[1,2]
    assert sum(e.kind=='EffectFailed' for e in events)==1
    assert not any(e.kind=='EffectUncertain' for e in events)
    assert all(e.payload['contract']['replay_safe'] for e in events)


@pytest.mark.parametrize('safe',[False,True])
def test_interrupted_attempt_obeys_persisted_replay_policy(tmp_path,safe):
    ledger=RunLedger(tmp_path/'run.db');calls=[]
    def action():
        calls.append(1)
        if len(calls)==1:raise SystemExit('interrupted')
        return 42
    lookup=tool(action,safe=safe)
    with effect_scope(EffectManager(ledger),'r'),pytest.raises(SystemExit):
        lookup.call(action_instance_id='operation')
    with effect_scope(EffectManager(RunLedger(ledger.path)),'r'):
        if safe:assert lookup.call(action_instance_id='operation')==42
        else:
            with pytest.raises(EffectUncertain):lookup.call(action_instance_id='operation')
    assert len(calls)==(2 if safe else 1)


def test_reconciliation_reuses_result_without_replaying_action(tmp_path):
    ledger=RunLedger(tmp_path/'run.db');calls=[];ready=False;timestamps=[]
    def action():calls.append(1);raise TimeoutError('reply lost')
    def reconcile(contract):
        timestamps.append(contract['effect_started_at'])
        return ('succeeded',42) if ready else ('unknown',None)
    lookup=tool(action,reconcile=reconcile)
    with effect_scope(EffectManager(ledger),'r'),pytest.raises(TimeoutError):lookup.call()
    ready=True
    with effect_scope(EffectManager(RunLedger(ledger.path)),'r'):assert lookup.call()==42
    assert len(calls)==1 and timestamps[0]==timestamps[1]


def test_same_action_cannot_change_replay_safety(tmp_path):
    manager=EffectManager(RunLedger(tmp_path/'run.db'))
    def action():raise TimeoutError()
    with pytest.raises(EffectUncertain):manager.execute('r','action',{},action)
    with pytest.raises(ValueError,match='conflict'):
        manager.execute('r','action',{},lambda:pytest.fail('must not run'),replay_safe=True)


class Worker(RuntimeParticipant):
    def __init__(self,lookup):super().__init__(['worker']);self.lookup=lookup
    def handle(self,state,message):
        value=self.lookup.call()
        return StateDelta(agent_id='worker',source_message_id=message.id,expected_version=state.version,
            evidence=[Evidence(kind='test_result',source='worker',claim='Result',value=value)],outcome='completed')


@pytest.mark.parametrize('failures,retriable,expected_calls',[(2,True,3),(99,True,3),(1,False,1)])
def test_runtime_retry_is_real_bounded_and_respects_error_metadata(tmp_path,failures,retriable,expected_calls):
    calls=[]
    def action():
        calls.append(1)
        if len(calls)<=failures:
            raise ToolError('opaque service error',error_type='unavailable',retriable=retriable)
        return {'value':42}
    worker=Worker(tool(action));runtime=DecentralizedRuntime([worker],ledger_path=tmp_path/'run.db')
    task=ScientificTask(objective='audit',initial_agent='worker');events=[]
    if failures<expected_calls:
        assert runtime.run(task,event_sink=events.append).status=='completed'
    else:
        with pytest.raises(ToolError):runtime.run(task,event_sink=events.append)
        assert runtime.ledger.replay_state(task.id).status=='failed'
    assert len(calls)==expected_calls
    recoveries=[e for e in events if e['type']=='runtime.recovery']
    assert len(recoveries)==expected_calls-1


def test_durable_failure_preserves_machine_readable_retry_semantics(tmp_path):
    lookup=tool(lambda:(_ for _ in ()).throw(ToolError('opaque',error_type='unavailable',retriable=True,backend='fixture',retry_after_seconds=2)))
    worker=Worker(lookup);ledger=RunLedger(tmp_path/'run.db')
    state=ScientificState.from_task(ScientificTask(objective='audit',initial_agent='worker'))
    message=AgentMessage(sender='user',recipient='worker',message_type='task.submitted')
    for _ in range(2):
        with effect_scope(EffectManager(ledger),state.task_id),pytest.raises(ToolError) as caught:
            DurableExecution(ledger).execute(worker,state,message)
        assert caught.value.retriable is True
        assert caught.value.backend=='fixture' and caught.value.retry_after_seconds==2
        assert runtime_recovery_action(caught.value,0)=='retry'


def test_permanent_error_does_not_retry_because_of_its_text():
    error=ToolError('timeout appears in an invalid argument',error_type='invalid_input',retriable=False)
    assert runtime_recovery_action(error,0)=='fail'
