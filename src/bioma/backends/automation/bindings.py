"""Direct automation service ownership; WP1 remains the execution authority."""
from bioma.backends.base import MethodBinding, ScientificBackend
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import EXECUTION


def execution_backend(bindings, *, scoped_skill_resolver=None):
    return ScientificBackend(EXECUTION, 'experiment-execution', bindings,
                             scoped_skill_resolver=scoped_skill_resolver)


def build_execution_backend( service, *, base_dir, method_tools=None):
    from bioma.capabilities.definitions import execution_capability
    grants = method_tools or {}
    from bioma.core.action_contracts import Predicate
    bindings=[]
    for original in execution_capability(allow_simulation=service.allow_simulation).methods:
        declaration=method(EXECUTION,original.id,'ExecutionReceipt',legacy=original,
            available=original.available,reason=original.unavailable_reason)
        # Replace the obsolete role-owned state.protocol flag with the
        # independently qualified evidence reference; do not remove review.
        declaration.action_contract.preconditions=[p for p in declaration.action_contract.preconditions if p.id!='reviewed_protocol']
        required={'kind':'reviewed_protocol','source':'harness','value.qualification_status':'independently_reviewed'}
        if original.id=='simulate_reviewed_protocol':
            required['value.execution_scope']='simulation'
        declaration.action_contract.preconditions.append(Predicate(id='independently_reviewed_protocol',source='message',
            path='payload.protocol_evidence_id',operator='evidence_reference',value=required))
        bindings.append(MethodBinding(declaration,_execute_protocol(service,base_dir),tuple(grants.get(original.id,()))))
    return execution_backend(bindings)


def _execute_protocol(service, base_dir):
    def execute(state, message, tools):
        del tools
        import json
        import hashlib
        from pathlib import Path
        from bioma.runtime.admission import current_execution_permit, check_execution
        from bioma.core.handoffs import ReviewedProtocol
        from bioma.core.models import Artifact
        from bioma.backends.base import BackendIncomplete
        from bioma.core.scientific_capabilities import WorkResult
        current_execution_permit(); check_execution()
        parent_id = str(message.payload.get('protocol_evidence_id') or '')
        # Preserve the existing committed protocol/review/hash boundary. A bare
        # StudyPlan WorkResult is not an admitted executable protocol.
        qualification = next((e for e in state.evidence if e.id==parent_id and e.kind=='reviewed_protocol' and e.source=='harness'),None)
        if qualification is None:
            raise ValueError('New execution requires independently qualified StudyPlan; historical role evidence is read-only')
        protocol = ReviewedProtocol.from_state(state, parent_id)
        method_id = message.payload['method_id']
        if method_id not in {'compile_reviewed_protocol', 'simulate_reviewed_protocol'}:
            raise ValueError('Unknown automation method')
        compile_only = method_id == 'compile_reviewed_protocol'
        if not compile_only and qualification.value.get('execution_scope')!='simulation':
            raise ValueError('Protocol qualification does not cover simulation')
        if not compile_only and (not service.allow_simulation or state.inputs.get('execution_mode') == 'scientific'):
            raise BackendIncomplete('blocked', {'code':'missing_experimental_observation',
                'message':'Simulation cannot execute a scientific instrument request.'})
        outcome = service.execute(state.task_id, {} if compile_only else state.inputs,
            protocol=protocol, compile_only=compile_only, execution_id=message.task_request_id or message.id)
        root = Path(base_dir) / 'artifacts' / 'derived' / state.task_id / 'automation' / (message.task_request_id or message.id)
        root.mkdir(parents=True, exist_ok=True)
        outputs = [('machine-code-json', 'machine-code.json', outcome.machine_code)]
        if not compile_only:
            outputs.append(('simulation-observation-json', 'observation.json', outcome.observation))
        artifacts = []
        for kind, name, value in outputs:
            path = root / name
            content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
            if path.exists() and path.read_text() != content:
                raise ValueError('Automation artifact conflict; reconciliation required')
            path.write_text(content)
            artifacts.append(Artifact(kind=kind, uri=str(path), source='experiment-execution',
                metadata={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'protocol_evidence_id':parent_id}))
        return WorkResult(family=EXECUTION, kind='ExecutionReceipt', input_refs=[parent_id],
            payload={'machine_code':outcome.machine_code, 'observation':None if compile_only else outcome.observation,
                     'data_kind':'calculation' if compile_only else 'simulation',
                     'compiled':True, 'simulated':not compile_only, 'physical_execution':False,
                     'verification':{'passed':outcome.verification.passed, 'issues':list(outcome.verification.issues)},
                     'limitations':['Compilation and virtual-device replay are not physical experiment observations.']},
            artifacts=[a.model_dump(mode='json') for a in artifacts],
            provenance={'backend':type(service).__name__, 'method':method_id, 'source_message_id':message.id,
                        'execution_id':message.task_request_id or message.id, 'protocol_sha256':protocol.sha256})
    return execute
