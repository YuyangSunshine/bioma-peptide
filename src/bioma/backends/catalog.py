"""Construct non-authoritative method contracts for direct backend bindings."""
from bioma.core.action_contracts import Predicate, ScientificActionContract
from bioma.core.methods import ExecutionMethod, ResultContract


def method(family, name, kind, *, legacy=None, available=True, reason='', tools=(),
           description=None, message_type='task.submitted', input_schema=None, profiles=(),
           verification=(), limitations=(), artifacts=(), data_kinds=()):
    contract = ScientificActionContract(writable_fields=['observations'], expected_effects=[
        Predicate(id='non_authoritative_result', source='result', path='updates.observations', operator='nonempty')])
    contract.invariants = [Predicate(id='preserve_' + field, source='after', path=field,
        operator='same_as', value={'source': 'state', 'path': field}) for field in ('task_id', 'objective')]
    contract.preconditions = [Predicate(id='committed_' + field, source='message',
        path='payload.' + field, operator='evidence_reference')
        for field in (input_schema or {}).get('required', []) if field.endswith('_evidence_id')]
    if legacy:
        contract.preconditions = legacy.action_contract.preconditions
        contract.invariants = legacy.action_contract.invariants
        contract.resources = legacy.action_contract.resources
        contract.risk_level = legacy.action_contract.risk_level
    return ExecutionMethod(id=name, description=legacy.description if legacy else (description or name.replace('_', ' ')),
        message_type=legacy.message_type if legacy else message_type,
        input_schema=legacy.input_schema if legacy else (input_schema or {'type': 'object'}),
        profiles=legacy.profiles if legacy else list(profiles), limitations=list(limitations), required_tools=list(tools),
        **({name:getattr(legacy,name) for name in ('required_input_kinds','required_evidence_refs',
            'execution_environment','required_input_qualification','required_artifact_suffixes',
            'min_input_count','max_input_count')} if legacy else {}),
        result=ResultContract(responsibility=family, result_channel='work_result', evidence_kinds=[kind],
            artifact_kinds=list(artifacts), data_kinds=list(data_kinds),
            verification_obligations=list(dict.fromkeys(['input_identity', 'result_provenance', 'no_self_commit', *verification]))),
        action_contract=contract, available=available,
        unavailable_reason=reason if not available else '')
