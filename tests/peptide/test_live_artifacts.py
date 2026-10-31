from bioma.harness.types import HarnessContext, ToolObservation
from bioma.runtime.telemetry import observe
from bioma.peptide.contracts import sha256
from bioma.web import WebRun, _run_response


def test_live_files_survive_final_failure_without_scientific_promotion(tmp_path):
    path = tmp_path / 'structure.cif'
    path.write_text('coordinates')
    run = WebRun(query='inspect target', files=[], session_id='session')
    context = HarnessContext({}, tmp_path)
    with observe(run.receive):
        context.record_observation(ToolObservation('assess', 'ok', 'inventory',
            metrics={'preview_sha256': {str(path): sha256(path)}}))
    assert run.state is None
    payload = run.payload('test')
    assert payload['state']['artifacts'][0]['metadata']['delivery_review'] == 'pending'
    run.state = {'status': 'blocked', 'artifacts': []}
    assert len(run.payload('test')['state']['artifacts']) == 1
    assert run.state['artifacts'] == []


def test_preview_rejects_changed_and_outside_files(tmp_path):
    root = tmp_path / 'task'
    root.mkdir()
    inside = root / 'data.csv'; inside.write_text('changed')
    outside = tmp_path / 'private.txt'; outside.write_text('outside')
    events = []
    with observe(events.append):
        HarnessContext({}, root).record_observation(ToolObservation('analysis', 'ok', '',
            metrics={'preview_sha256': {str(inside): '0'*64, str(outside): sha256(outside)}}))
    assert events == []


def test_length_error_explains_no_partial_execution():
    response = _run_response({'status': 'blocked', 'status_reason': {
        'error_type': 'ModelOutputBudgetError', 'message': 'length'}})
    assert '完整' in response and 'ModelOutputBudgetError' not in response


def test_scalar_inspection_digest_is_not_a_file_manifest(tmp_path):
    events = []
    with observe(events.append):
        HarnessContext({}, tmp_path).record_observation(ToolObservation(
            'inspect_input_data', 'ok', 'inspected', metrics={'sha256': 'a'*64}))
    assert events == []


def test_clarification_is_a_readable_question_not_raw_runtime_json():
    text = _run_response({'status': 'pending', 'status_reason': {
        'code': 'clarification_required', 'question': '已有候选还是从头设计？',
        'reason': '候选约束尚未提供'}})
    assert '已有候选还是从头设计？' in text
    assert 'clarification_required' not in text


def test_design_preferences_do_not_require_database_alternatives(tmp_path):
    from bioma.backends.investigation import request_clarification
    import pytest
    context = HarnessContext({'original_user_request': 'Design cyclic peptides',
        'public_resource_discovery': True, 'harness_observations': [
            {'tool_name': 'inspect_inputs', 'status': 'ok', 'metrics': {}}]}, tmp_path)
    args = dict(kind='design_constraint', field='cyclization_chemistry',
        request_quote='Design cyclic peptides', question='Which cyclization chemistry?',
        reason='Scaffold constraints determine candidate design', alternatives=[])
    result = request_clarification(context, args)
    assert result.patch['need_clarification']
    assert not result.patch.get('approved')
    with pytest.raises(ValueError):
        request_clarification(context, {**args, 'field': 'production_approval'})
    with pytest.raises(ValueError):
        request_clarification(context, {**args, 'request_quote': 'invented request'})


def test_structures_have_distinct_deliverable_names(tmp_path, monkeypatch):
    from bioma.peptide import target_research as tr
    from test_web_research import CIF
    monkeypatch.setattr(tr, 'DATA', tmp_path)
    source = tmp_path / 'source.cif'
    monkeypatch.setattr(tr, 'fetch', lambda url: source)
    source.write_text(CIF)
    first = tr.assess('1ABC')
    source.write_text(CIF.replace('1ABC', '2ABC'))
    second = tr.assess('2ABC')
    assert {p.rsplit('/', 1)[-1] for p in first['analysis_inputs']}.isdisjoint(
        {p.rsplit('/', 1)[-1] for p in second['analysis_inputs']})


def test_preview_http_rechecks_identity_and_root(tmp_path):
    from bioma.web import create_server
    from threading import Thread
    from urllib.request import urlopen
    from urllib.error import HTTPError
    import pytest
    class UnusedBioMA:
        pass
    server = create_server(UnusedBioMA(), backend='test', port=0, artifact_root=tmp_path)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        path = tmp_path / 'source.cif'; path.write_text('coordinates')
        run = WebRun(query='target', session_id='session')
        run.preview_artifacts[str(path)] = {'id': 'preview', 'uri': str(path),
            'metadata': {'sha256': sha256(path), 'delivery_review': 'pending'}}
        server.runs[run.id] = run
        url = f'http://127.0.0.1:{server.server_address[1]}/api/runs/{run.id}/artifacts/preview'
        with urlopen(url) as response:
            assert response.read() == b'coordinates'
            assert response.headers['Content-Type'] == 'chemical/x-cif'
        path.write_text('modified')
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        assert error.value.code == 409
        run.preview_artifacts[str(path)]['uri'] = str(tmp_path.parent / 'outside.cif')
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        assert error.value.code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_identical_copies_have_distinct_preview_ids(tmp_path):
    files = []
    for name in ['source', 'package']:
        folder = tmp_path / name; folder.mkdir()
        file = folder / 'chart.svg'; file.write_text('<svg/>'); files.append(file)
    events = []
    with observe(events.append):
        HarnessContext({}, tmp_path).record_observation(ToolObservation('assess', 'ok', '',
            metrics={'preview_sha256': {str(p): sha256(p) for p in files}}))
    assert len({a['id'] for a in events[0]['artifacts']}) == 2
