"""Two locally registered methods; real CSV execution, qualified dependency, delivery.

Run with an installed BioMA wheel: python examples/method_integration.py --root /tmp/method-demo
No model or network calls. Synthetic mutation scores are not biological measurements.
"""
from pathlib import Path
import argparse
import json

from bioma import BioMA
from bioma.config import load_config
from bioma.core.action_contracts import ScientificActionContract
from bioma.core.community import ExecutionProfile
from bioma.core.methods import ExecutionMethod, ResultContract
from bioma.core.models import ScientificTask
from bioma.core.protocols import VerificationReport
from bioma.backends.base import MethodBinding
from bioma.backends.factory import build_backends
from bioma.backends.protocol.bindings import study_backend, _analysis_informed_study_method
from bioma.backends.tabular_analysis import generic_tabular_analysis
from bioma.runtime.provider_delivery import deliver_named_results
from bioma.runtime.result_qualification import WorkResultQualificationService, production_scientific_result_qualifier
from bioma.tools.base import Tool, ToolInput
from bioma.core.tabular import summarize_csv


class CSVInput(ToolInput):
    path: str


def recompute_csv(path: str):
    return summarize_csv(Path(path))


def backends(root):
    tool = Tool('csv_recompute', 'Recompute descriptive CSV statistics, not biological effects.',
        'calculation', '1', 'BioMA example', recompute_csv, args_schema=CSVInput,
        result_kind='calculation', replay_safe=True,
        result_validator=lambda result, args: isinstance(result, dict) and 'row_count' in result)
    analysis = ExecutionMethod(id='tabular_quality_v1', description='Descriptive mutation-score table audit.',
        message_type='task.submitted',
        input_schema={'type':'object', 'required':['inputs'], 'properties':{'inputs':{
            'type':'object', 'required':['source_inputs'], 'properties':{
                'source_inputs':{'type':'array','minItems':1,'maxItems':1}}}}},
        result=ResultContract(responsibility='scientific.compute', result_channel='work_result',
            evidence_kinds=['AnalysisResult'], artifact_kinds=['analysis-code-python', 'analysis-summary-json',
                'analysis-execution-receipt'], verification_obligations=['Independent CSV recomputation and hash verification']),
        profiles=[ExecutionProfile(id='python-csv', engine='python', modalities=['table'],
            output_modalities=['json'], operations=['tabular quality summary'])],
        required_tools=[tool.name], execution_environment=['python'],
        required_input_kinds=['user_supplied_input'], required_artifact_suffixes=['.csv'], max_input_count=1,
        limitations=['Descriptive statistics only; no biological confirmation.'],
        action_contract=ScientificActionContract(writable_fields=['observations']))
    runner = generic_tabular_analysis(root)
    def execute(state, message, tools):
        work = runner(state, message, tools)
        if tools[0].call(path=work.payload['source_csv_uri']) != work.payload['summary']:
            raise ValueError('CSV numerical discrepancy')
        return work
    study = ExecutionMethod(id='qualified_mutation_plan_v1',
        description='Future mutation-score validation plan requiring independently recomputed AnalysisResult.',
        message_type='task.submitted', entrypoint=False,
        result=ResultContract(responsibility='scientific.study_design', result_channel='work_result',
            evidence_kinds=['StudyPlan'], artifact_kinds=['study-plan-json'],
            verification_obligations=['Qualified upstream analysis, referenced summary and future-only scope']),
        profiles=[ExecutionProfile(id='future-plan', operations=['mutation validation planning'])],
        limitations=['Synthetic mutation-score input only in this example; no assay validation or execution authorization.'],
        action_contract=ScientificActionContract(writable_fields=['observations']))
    return MethodBinding(analysis, execute, (tool,)), MethodBinding(study, _analysis_informed_study_method(root))


def application(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    analysis, study = backends(root)
    providers, interpreter = build_backends(load_config(), base_dir=root,
        remote_compute_backends=(analysis,))  # Existing binding slot also accepts local MethodBinding.
    providers = [study_backend([*p.bindings.values(), study], scoped_skill_resolver=p.scoped_skill_resolver)
                 if p.family == 'scientific.study_design' else p for p in providers]
    return BioMA(backends=providers, query_interpreter=interpreter, ledger_path=root/'ledger.sqlite3')


def run(root):
    root = Path(root).resolve()
    app = application(root)
    source = root/'mutation-scores.csv'
    source.write_text('mutation,position,score\nA1V,1,0.2\nC2S,2,0.8\nD3E,3,0.5\n', encoding='utf-8')
    events = []
    def record(event):
        events.append(event.model_dump(mode='json') if hasattr(event, 'model_dump') else str(event))
    from bioma.api import _query_inputs
    method_state = app.run(ScientificTask(
        objective='Calculate a tabular quality summary of this synthetic mutation-score CSV.',
        initial_capability='scientific.compute', initial_method_id='tabular_quality_v1',
        inputs=_query_inputs([source])), event_sink=record)
    raw = [o for o in method_state.observations if o.value.get('kind')=='AnalysisResult']
    if len(raw) != 1 or raw[0].value['provenance']['method_id'] != 'tabular_quality_v1':
        raise AssertionError('Explicit method task did not execute the registered method: '+str(method_state.status_reason))
    # A separate, explicit research contract requests the qualified dependency.
    # This uses the same public Runtime, not an example-specific provider or loop.
    task = ScientificTask(objective='Audit synthetic mutation scores and deliver a future-only validation study plan.',
        initial_capability='scientific.compute', initial_method_id='tabular_quality_v1',
        inputs={**method_state.inputs, 'execution_mode':'scientific'},
        gateway={'acceptance_mode':'research', 'evidence_requirements':[
            {'id':'summary','description':'Independently recompute the supplied synthetic CSV.',
             'evidence_kinds':['AnalysisResult'],'semantic_review':False,
             'predicates':[{'field':'qualification_status','operator':'eq','value':'independently_recomputed'}]},
            {'id':'plan','description':'Prepare a future-only plan consuming the qualified analysis.',
             'evidence_kinds':['StudyPlan'],'depends_on':['summary'],'semantic_review':False,
             'capability':'scientific.study_design','method_id':'qualified_mutation_plan_v1',
             'predicates':[{'field':'experiment_executed','operator':'eq','value':False}]},
            {'id':'delivery','description':'Deliver the reviewed plan JSON.',
             'evidence_kinds':['delivery'],'artifact_kinds':['study-plan-json'],
             'artifact_names':['analysis-informed-study-plan.json'],'stage':'delivery','depends_on':['plan'],'semantic_review':False}],
             'delivery_requirement_ids':['delivery']})
    state = app.run(task, event_sink=record)
    qualified = []
    for kind in ('AnalysisResult', 'StudyPlan'):
        work = next(o for o in reversed(state.observations) if o.value.get('kind')==kind)
        refs = production_scientific_result_qualifier(inputs=state.inputs, state=state).qualify(app.runtime, task.id, [work.id])
        if len(refs) != 1:
            raise AssertionError('Independent result qualification failed: '+kind)
        qualified.extend(refs)
        state = app.run(task, event_sink=record)
    review = next(o for o in reversed(state.observations) if o.value.get('kind')=='EpistemicQualification')
    def independently_review(work):
        evidence = {e.id:e for e in state.evidence}
        analysis, plan = (evidence[ref] for ref in qualified)
        rows = work.payload['assessment']['requirements']
        valid = (analysis.value['summary'] == summarize_csv(source)
            and plan.value['experiment_executed'] is False and analysis.id in plan.parent_ids
            and len(rows)==2 and all(r['status']=='satisfied' for r in rows)
            and {ref for r in rows for ref in r['evidence_ids']} == set(qualified))
        return VerificationReport(valid, () if valid else ('Numerical or dependency review failed',))
    reviews = WorkResultQualificationService.research_review('example-contract-recomputation-v1', state,
        independently_review).qualify(app.runtime, task.id, [review.id])
    if len(reviews) != 1:
        raise AssertionError('Independent whole-contract review not qualified')
    state = app.runtime.ledger.replay_state(task.id)
    state = deliver_named_results(app.runtime, state, ['analysis-informed-study-plan.json'], root/'delivery')
    if not state.acceptance['complete'] or not app.runtime.ledger.verify_chain(task.id):
        raise AssertionError('Scientific delivery or ledger verification failed')
    (root/'state.json').write_text(state.model_dump_json(indent=2), encoding='utf-8')
    (root/'events.json').write_text(json.dumps(events, indent=2), encoding='utf-8')
    report = {'task_id':task.id, 'method_task_id':method_state.task_id, 'status':state.status,
        'acceptance':state.acceptance, 'methods':[o.value.get('provenance',{}).get('method_id')
            for o in state.observations if o.kind=='work_result'], 'qualified_refs':qualified,
        'review_refs':list(reviews), 'external_model_calls':0, 'ledger':str(app.runtime.ledger.path)}
    (root/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return app, state


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    run(parser.parse_args().root)
