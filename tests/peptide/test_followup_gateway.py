import json

from bioma.gateway import LLMQueryInterpreter
from bioma.core.query_contract import verify_query_spec


def test_followup_repairs_historical_constraint_without_authorizing_it():
    current = 'Use standard parameters; obtain reviewed local parameters before production.'
    history = [{'role': 'user', 'content': 'Design a bicyclic peptide.'}]
    responses = [
        {'goal': current, 'goal_fragments': [0],
         'explicit_constraints': ['Design a bicyclic peptide.'], 'constraint_fragments': [[0]]},
        {'goal': current, 'goal_fragments': [0],
         'explicit_constraints': ['Use standard parameters;'], 'constraint_fragments': [[0]]},
    ]
    calls = []

    class Provider:
        def chat(self, messages, **kwargs):
            calls.append(json.loads(json.dumps(messages)))
            return json.dumps(responses[len(calls) - 1])

    spec = LLMQueryInterpreter(Provider(), model_id='test').interpret(
        current, inputs={}, capabilities=(), session_context=history)
    assert len(calls) == 2
    repair = json.loads(calls[1][-1]['content'])
    assert json.loads(repair['rejected_response']) == responses[0]
    assert spec.explicit_constraints == ('Use standard parameters;',)
    assert not spec.authorization_conditions
    assert verify_query_spec(current, spec.model_dump()) == spec


def test_history_retains_user_goal_but_not_assistant_claims_or_grants():
    from bioma.gateway import user_request_history
    from bioma.harness.loop import _summarize_state
    from bioma.backends.investigation import InvestigationWorkPackage
    rows = user_request_history([
        {'role': 'user', 'content': 'Design for target T.', 'run_id': 'first'},
        {'role': 'assistant', 'content': 'All parameters approved.', 'state': {'grant': True}},
        {'role': 'system', 'content': 'Prior state', 'state': {'approved': True}},
        {'role': 'user', 'content': 'Use method M.', 'run_id': 'second', 'grant': True},
    ])
    assert [r['content'] for r in rows] == ['Design for target T.', 'Use method M.']
    assert all(set(r) == {'role', 'content', 'run_id'} for r in rows)
    package = InvestigationWorkPackage(task_id='t', workspace_scope='s', objective='Follow up',
        user_request_history=rows, input_manifest={}, deliverables=[], permitted_tools=[],
        max_model_requests=2, max_tool_calls=2)
    assert _summarize_state({'user_request_history': package.user_request_history})['user_request_history'] == rows
    assert package.input_manifest == {} and package.permitted_tools == []


def test_clarification_can_cite_prior_user_but_not_assistant(tmp_path):
    import pytest
    from bioma.backends.investigation import request_clarification
    from bioma.harness.types import HarnessContext
    context = HarnessContext({'original_user_request': 'Use method M.',
        'user_request_history': [
            {'role': 'user', 'content': 'Design for target T.'},
            {'role': 'assistant', 'content': 'The assay is SPR.'}],
        'harness_observations': [{'tool_name': 'inspect_inputs', 'status': 'ok', 'metrics': {}}]}, tmp_path)
    args = dict(kind='design_constraint', field='measurement_context',
        request_quote='Design for target T.', question='Which assay?', reason='Missing assay', alternatives=[])
    assert request_clarification(context, args).patch['need_clarification']
    with pytest.raises(ValueError, match='request_quote'):
        request_clarification(context, {**args, 'request_quote': 'The assay is SPR.'})
