from bioma.core.action_contracts import Predicate, ScientificActionContract
from bioma.core.models import Artifact, Evidence, ScientificState, StateDelta
from bioma.core.transitions import TransitionProposal
from bioma.runtime.boundaries import normalize_work_result, specialize_work_contract


def proposal():
    return TransitionProposal(task_id='work', message_id='message', proposer='worker',
        base_version=1, observation_hash='state', capability_id='scientific.data_analysis',
        capability_version='1', method_id='analysis', action='task.submitted', input_hash='input',
        read_set={}, write_set=['metrics'], supporting_evidence=[],
        contract=ScientificActionContract(writable_fields=['metrics'],
            expected_effects=[Predicate(id='supporting_evidence_returned', source='result',
                path='evidence', operator='nonempty')]), risk='low')


def state(task_class='WORK'):
    return ScientificState(task_id='work', objective='bounded work', version=1,
        gateway=({'intake_version':'research-contract-intake-v1'} if task_class == 'WORK' else {}))


def raw_delta(tmp_path, *, kind='analysis'):
    evidence = Evidence(id='candidate', kind=kind, claim='provider result',
        value={'computed': 7}, source='worker', method='analysis')
    artifact = Artifact(id='artifact', kind='json', uri=str(tmp_path / 'result.json'),
        source='worker', metadata={'sha256': 'digest'}, evidence_ids=[evidence.id])
    return StateDelta(agent_id='worker', source_message_id='message', expected_version=1,
        evidence=[evidence], artifacts=[artifact], updates={'metrics': {'done': True}},
        outcome='completed')


def test_work_result_is_deterministically_removed_from_scientific_evidence(tmp_path):
    bound = specialize_work_contract(state(), proposal())
    raw = raw_delta(tmp_path)
    normalized = normalize_work_result(state(), bound, raw)
    repeated = normalize_work_result(state(), bound, raw)
    assert normalized == repeated
    assert normalized.evidence == []
    observation = normalized.updates['observations'][0]
    assert observation['kind'] == 'work_result'
    assert observation['method'] == 'analysis'
    assert observation['value']['candidate_results'][0]['id'] == 'candidate'
    assert normalized.artifacts[0].evidence_ids == []
    assert normalized.artifacts[0].metadata['candidate_result_refs'] == ['candidate']
    assert bound.contract.preconditions == proposal().contract.preconditions
    assert bound.contract.invariants == proposal().contract.invariants
    assert bound.contract.resources == proposal().contract.resources
    assert bound.contract.risk_level == proposal().contract.risk_level
    assert bound.contract.expected_effects[0].path == 'updates.observations'


def test_work_execution_failure_is_operational_not_scientific_evidence(tmp_path):
    normalized = normalize_work_result(state(), proposal(), raw_delta(tmp_path, kind='execution_failure'))
    assert normalized.evidence == []
    assert normalized.updates['observations'][0]['value']['candidate_results'][0]['kind'] == 'execution_failure'


def test_research_result_and_contract_are_unchanged(tmp_path):
    original = raw_delta(tmp_path)
    research = state('RESEARCH')
    assert normalize_work_result(research, proposal(), original) == original
    assert specialize_work_contract(research, proposal()) == proposal()
