from types import SimpleNamespace
import importlib
import time

import pytest

from bioma.api import BioMA
from bioma.backends.base import ScientificBackend, MethodBinding
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import COMPUTE, WorkResult
from bioma.core.models import ScientificTask


@pytest.mark.parametrize('budgets,status', [
    ({}, 'completed'),
    ({'task_deadline_seconds': 120}, 'failed'),
    ({'query_deadline_seconds': 200}, 'failed'),
    ({'query_deadline_seconds': 260}, 'completed'),
])
def test_query_and_execution_budgets_share_a_bounded_clock(tmp_path, monkeypatch, budgets, status):
    clock = [0.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    monkeypatch.setattr(importlib.import_module('bioma.runtime.deadline'), 'time', fake_time)
    monkeypatch.setattr(importlib.import_module('bioma.runtime.engine'), 'time', fake_time)

    def execute(state, message, tools):
        clock[0] += 180  # A legitimate operation exceeding the previous 120s cap.
        return WorkResult(family=COMPUTE, kind='ComputeResult',
            payload={'finished':True}, provenance={'basis':'controlled clock'})
    worker = ScientificBackend(COMPUTE, 'worker', [
        MethodBinding(method(COMPUTE, 'bounded_work', 'ComputeResult'), execute)])
    app = BioMA(backends=[worker], ledger_path=tmp_path/'run.db', **budgets)
    task = ScientificTask(objective='bounded work', initial_capability=COMPUTE,
        initial_method_id='bounded_work', initial_message_type='task.submitted')

    def admitted(*args, **kwargs):
        clock[0] += 70
        return app.run(task)

    monkeypatch.setattr(app, '_query', admitted)
    result = app.query('bounded work')
    assert result.status == status
    assert app.runtime.ledger.replay_state(task.id).status == status
    assert any(o.kind == 'work_result' and o.value['payload'].get('finished')
               for o in result.observations) == (status == 'completed')
    if status == 'failed':
        assert result.status_reason['code'] == 'execution_timeout'
