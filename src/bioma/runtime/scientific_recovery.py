"""Explicit, opt-in adoption of a reviewed recovery child in the same journal.

No external Evidence is imported and no scientific support is fabricated. The
caller must first run and qualify the child through the governed runtime.
"""
import hashlib
from pathlib import Path
from bioma.core.models import ScientificState
from bioma.core.research import contract_for, fingerprint
from bioma.core.research_planning import plan_for
from bioma.runtime.scientific_control import _merge_committed, ConsumedControlBudget
from bioma.runtime.task_lease import task_lease


def accept_recovered_child(control, parent_task, child_id, original_child_id):
    ledger=control.runtime.ledger
    with task_lease(ledger,parent_task.id):
        if not ledger.verify_chain(parent_task.id) or not ledger.verify_chain(child_id):
            raise ValueError('Recovery requires verified parent and child chains')
        parent_events=ledger.events(parent_task.id);child_events=ledger.events(child_id)
        boots=[e for e in parent_events if e.kind=='AdaptiveContractPlanBootstrapped']
        if not boots or control._public_task(boots[0].payload['task'])!=control._public_task(parent_task.model_dump(mode='json')):
            raise ValueError('Recovery cannot replace original task')
        existing=[e for e in parent_events if e.kind=='ScientificRecoveryAccepted' and e.payload['child_id']==child_id]
        if existing:
            # A crash after acceptance but before checkpointing is recoverable.
            event=existing[-1];state=ScientificState.model_validate(event.payload['state'])
            control._checkpoint(parent_task.id,state,ConsumedControlBudget.model_validate(event.payload['consumed']),event.payload['next_cycle'])
            return state
        checkpoints=[e for e in parent_events if e.kind=='AdaptiveControlCheckpoint']
        cp=checkpoints[-1].payload
        if cp.get('terminal'):raise ValueError('Terminal parent needs a separately authorized reopen policy')
        parent=ScientificState.model_validate(cp['state'])
        child=ledger.replay_state(child_id)
        init=next((e for e in child_events if e.kind=='RunInitialized'),None)
        if not child or child.status!='completed' or not init:
            raise ValueError('Recovery child is not a completed governed execution')
        task=init.payload['task']
        if task.get('inputs',{}).get('recovery_of_task')!=original_child_id:
            raise ValueError('Recovery child lacks source task binding')
        # Reconstruct lineage from actual prior selection and grounded step, not
        # the new child's self-description alone.
        selection=None
        for e in parent_events:
            if e.kind!='OptionExecutionRecord':continue
            record=e.payload
            cycle=e.idempotency_key.rsplit(':',1)[-1]
            decisions=[d for d in parent_events if d.kind=='ControllerDecision' and d.idempotency_key=='controller-decision:'+cycle]
            if not decisions or decisions[-1].payload['selected_grounded_option_identity']!=record['grounding_identity']:continue
            for grounding in (g for g in parent_events if g.kind=='Grounding'):
                if grounding.idempotency_key!='groundings:'+cycle:continue
                for result in grounding.payload['results']:
                    for option in result['result'].get('alternatives',[]):
                        from bioma.core.capability_binding import GroundedOption
                        bound=GroundedOption.model_validate(option)
                        # Historical identities used insertion-order JSON; rehydrating
                        # sorted journal mappings can change their digest. Use the
                        # persisted decision/option binding plus actual child bootstrap.
                        if bound.option_identity!=record['option_identity']:continue
                        for step in bound.steps:
                            expected='option.'+hashlib.sha256(f"{record['execution_instance_id']}:{step.step_id}".encode()).hexdigest()
                            if expected==original_child_id:
                                originals=ledger.events(original_child_id)
                                original_init=next((x for x in originals if x.kind=='RunInitialized'),None)
                                exact=(original_init.payload['task'].get('gateway',{}).get('exact_grounded_binding',{}) if original_init else {})
                                if exact!={'provider_id':step.provider_id,'capability_id':step.capability_id,'method_id':step.method_id,'implementation_identity':step.implementation_identity}:continue
                                selection=(e,step)
        if selection is None:raise ValueError('No parent-selected original child lineage')
        selected_event,step=selection
        if task.get('initial_method_id')!=step.method_id or task.get('initial_capability')!=step.capability_id:
            raise ValueError('Recovery method differs from parent selection')
        if not all(any(e.kind==k for e in child_events) for k in ['ExecutionPermitted','ExecutionResultRecorded']):
            raise ValueError('Recovery admission or execution receipt absent')
        qualified=set()
        for e in child_events:
            if e.kind=='TransitionCommitted' and e.payload.get('message',{}).get('message_type')=='runtime.evidence_qualification':
                qualified.update(x['id'] for x in e.payload['delta'].get('evidence',[]))
        if not qualified or not qualified<={e.id for e in child.evidence}:
            raise ValueError('No actual qualified recovery Evidence')
        results=[o for o in child.observations if o.kind=='work_result']
        if not results:raise ValueError('Recovery WorkResult absent')
        for result in results:
            work=result.value;review=work.get('payload',{}).get('delivery_review',{})
            if review.get('issues')!=[] or not review.get('path') or not review.get('sha256'):
                raise ValueError('Recovery lacks complete delivery review receipt')
            for path,digest in review['sha256'].items():
                if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:raise ValueError('Reviewed artifact changed')
            reviewed_artifact=next((a for a in work.get('artifacts',[]) if a.get('uri')==review['path']),None)
            if reviewed_artifact is None or hashlib.sha256(Path(review['path']).read_bytes()).hexdigest()!=reviewed_artifact.get('metadata',{}).get('sha256'):
                raise ValueError('Review receipt not bound to committed WorkResult')
        state=_merge_committed(parent,child)
        if contract_for(state)!=contract_for(parent) or plan_for(state)!=plan_for(parent):
            raise ValueError('Recovery changed frozen contract or plan')
        completed_cycles=[int(e.idempotency_key.rsplit(':',1)[-1]) for e in parent_events if e.kind=='OptionExecutionRecord']
        snapshot_cycles=[int(e.idempotency_key.split(':')[1]) for e in parent_events if e.kind=='DecisionSnapshot']
        next_cycle=max(cp['next_cycle'],max(completed_cycles,default=-1)+1,max(snapshot_cycles,default=-1)+1)
        consumed=ConsumedControlBudget.model_validate(cp['consumed'])
        consumed=consumed.model_copy(update={'cycles':max(consumed.cycles,next_cycle)})
        # Keep prior failure feedback as history; only remove this exact recovered
        # binding from transient pruning. No other failed action is unblocked.
        reason=dict(state.status_reason)
        for key in ['control_failed_bindings','control_unproductive_bindings']:
            if key in reason:reason[key]=[r for r in reason[key] if tuple(r)!=(step.provider_id,step.method_id)]
        reason['recovered_child_ref']=child_id
        state=state.model_copy(update={'status_reason':reason})
        payload={'child_id':child_id,'original_child_id':original_child_id,
            'selected_execution_event_id':selected_event.event_id,'qualified_evidence_refs':sorted(qualified),
            'before_state_hash':fingerprint(parent.model_dump(mode='json')),'state':state.model_dump(mode='json'),
            'consumed':consumed.model_dump(mode='json'),'next_cycle':next_cycle}
        control._append(parent_task.id,'ScientificRecoveryAccepted',payload,'recovery-accepted:'+child_id)
        control._checkpoint(parent_task.id,state,consumed,next_cycle)
        return state
