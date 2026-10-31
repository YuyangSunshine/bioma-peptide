import json
from types import SimpleNamespace
import pytest
from bioma.harness.types import HarnessContext, ToolObservation
from bioma.harness.checkpoint import DeliveryCheckpoint
from bioma.harness.delivery_review import DeliveryReviewIncomplete
from bioma.llm import ComputedPendingReview


def setup(tmp_path, monkeypatch, enabled=True, calls=5):
    from bioma.harness import checkpoint as module
    context=HarnessContext({'task_id':'continue', 'continuous_investigation':True,
                            'harness_mode':'governed', 'investigate_review_gaps':enabled},tmp_path)
    root=context.artifact_dir();root.mkdir(parents=True,exist_ok=True)
    receipt=root/'receipt.json';receipt.write_text('{}')
    observation=ToolObservation('finish_investigation','ok','fixture',
        metrics={'receipt_path':str(receipt),'sha256':{},'execution_sha256':{}},
        patch={'analysis_code_paths':[]})
    budget=SimpleNamespace(remaining_llm_calls=calls,remaining_tool_calls=5,
                           telemetry=SimpleNamespace(flush=lambda:{}))
    checkpoint=DeliveryCheckpoint(context,SimpleNamespace(model_dump=lambda **kwargs:{}))
    attempts=[]
    def incomplete(*args, **kwargs):
        attempts.append(1)
        (root/'delivery_review.json').write_text(json.dumps({'response':'{"issues":[]}',
            'assessment_status':'INCOMPLETE','unresolved_findings':[{'reason':'support gap, not a confirmed defect'}]}))
        raise DeliveryReviewIncomplete('needs evidence')
    monkeypatch.setattr(module,'review_delivery',incomplete)
    return context,observation,budget,checkpoint,attempts


def test_incomplete_review_returns_leads_without_approval_or_repeat_review(tmp_path,monkeypatch):
    context,obs,budget,checkpoint,attempts=setup(tmp_path,monkeypatch)
    with pytest.raises(DeliveryReviewIncomplete,match='investigation leads'):
        checkpoint.review(obs,None,budget)
    assert context.state['delivery_review_incomplete']['evidence_status']=='not_evidence'
    assert not context.state.get('delivery_complete')
    assert checkpoint.path.is_file()
    assert len(attempts)==2  # existing bounded reviewer clarification, no new Agent loop
    with pytest.raises(DeliveryReviewIncomplete,match='UNCHANGED_REVIEW_MATERIAL'):
        checkpoint.review(obs,None,budget)
    assert len(attempts)==2


@pytest.mark.parametrize('enabled,calls',[(False,5),(True,1)])
def test_default_and_exhausted_budget_keep_durable_pending_state(tmp_path,monkeypatch,enabled,calls):
    context,obs,budget,checkpoint,attempts=setup(tmp_path,monkeypatch,enabled,calls)
    with pytest.raises(ComputedPendingReview):checkpoint.review(obs,None,budget)
    assert checkpoint.path.is_file()
    assert not context.state.get('delivery_complete')
