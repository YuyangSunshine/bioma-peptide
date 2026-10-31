import json
from types import SimpleNamespace
import pytest
from bioma.harness.delivery_review import review_delivery
from bioma.harness.types import HarnessContext, ToolObservation
from bioma.peptide.contracts import sha256


class Captured(Exception):
    pass


class CaptureReviewer:
    def chat_with_tools(self, messages, tools, **kwargs):
        self.payload = json.loads(messages[1]['content'])
        raise Captured()


def fixture(tmp_path, mode):
    context = HarnessContext({'task_id':'review', 'continuous_investigation':True,
                              'review_history_mode':mode}, tmp_path)
    root = context.artifact_dir()
    root.mkdir(parents=True, exist_ok=True)
    receipt = root/'receipt.json'; receipt.write_text('{}')
    outputs=root/'outputs'; outputs.mkdir()
    answer=outputs/'answer.txt'; answer.write_text('SELECTED OUTPUT')
    program=root/'selected.py'; program.write_text('SELECTED SOURCE')
    old=root/'old.py'; old.write_text('UNSELECTED HISTORY\n' * 20000)
    context.state['harness_observations']=[{'tool_name':'execute_analysis_code', 'status':'error',
        'metrics':{'execution_sha256':{str(old):sha256(old)}}}]
    observation=ToolObservation('finish_investigation','ok','fixture',artifacts=[str(answer)],
        metrics={'receipt_path':str(receipt),'input_sha256':{},'runtime':{},
                 'sha256':{str(answer):sha256(answer)},
                 'execution_sha256':{str(program):sha256(program)}},
        patch={'analysis_code_paths':[str(program)]})
    budget=SimpleNamespace(remaining_llm_calls=1,record_llm_call=lambda:None)
    return context,observation,budget,old,program


def test_identity_history_keeps_full_selected_material_and_checks_old_hashes(tmp_path):
    context,obs,budget,old,program=fixture(tmp_path,'identity')
    llm=CaptureReviewer()
    with pytest.raises(Captured):review_delivery(context,obs,llm,budget)
    assert llm.payload['executed_programs']['selected.py']=='SELECTED SOURCE'
    assert llm.payload['artifacts']['answer.txt']=='SELECTED OUTPUT'
    history=llm.payload['investigation_execution_history'][0]['verified_files'][str(old)]
    assert history['sha256']==sha256(old)
    assert 'not semantically reviewed' in history['scope']
    assert 'UNSELECTED HISTORY' not in json.dumps(llm.payload)
    old.write_text('tampered')
    with pytest.raises(ValueError,match='Prior investigation artifact changed'):
        review_delivery(context,obs,llm,budget)


def test_legacy_full_history_budget_is_preserved(tmp_path):
    context,obs,budget,old,program=fixture(tmp_path,'full')
    with pytest.raises(ValueError,match='history exceeds review budget'):
        review_delivery(context,obs,CaptureReviewer(),budget)


def test_identity_mode_does_not_skip_oversized_selected_source(tmp_path):
    context,obs,budget,old,program=fixture(tmp_path,'identity')
    program.write_text('SELECTED SOURCE\n' * 20000)
    obs.metrics['execution_sha256'][str(program)]=sha256(program)
    with pytest.raises(ValueError,match='Executed source exceeds'):
        review_delivery(context,obs,CaptureReviewer(),budget)
