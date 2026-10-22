import json
import time
from importlib.resources import files
from threading import Lock, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from bioma.core.models import Artifact, ScientificState, ScientificTask
from bioma.web import create_server


class FailingBioMA:
    def query(self, query, *, files, event_sink):
        event_sink(
            {
                "timestamp": "2026-08-28T00:00:00Z",
                "type": "agent.started",
                "agent": "predagent",
                "message": {
                    "sender": "user",
                    "recipient": "predagent",
                    "message_type": "task.submitted",
                    "payload": {"objective": query},
                    "ttl": 5,
                },
                "state": {
                    "version": 0,
                    "status": "running",
                    "predictions": [],
                    "evidence": [],
                },
            }
        )
        raise RuntimeError("coordinator returned invalid output")


class ConcurrentProbeBioMA:
    def __init__(self):
        self.lock = Lock()
        self.active = 0
        self.max_active = 0

    def query(self, query, *, files, event_sink):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        state = ScientificState.from_task(
            ScientificTask(objective=query, initial_agent="predagent")
        )
        return state.model_copy(update={"status": "completed"})


class FileProbeBioMA:
    def __init__(self):
        self.files = []

    def query(self, query, *, files, event_sink):
        self.files = files
        state = ScientificState.from_task(
            ScientificTask(objective=query, initial_agent="comagent")
        )
        return state.model_copy(update={"status": "completed"})


class ArtifactProbeBioMA:
    def __init__(self, path):
        self.path = path

    def query(self, query, *, files, event_sink):
        state = ScientificState.from_task(
            ScientificTask(objective=query, initial_agent="comagent")
        )
        return state.model_copy(
            update={
                "status": "completed",
                "artifacts": [
                    Artifact(
                        id="report-json",
                        kind="analysis_json",
                        uri=str(self.path),
                        source="comagent",
                    )
                ],
            }
        )


class SessionProbeBioMA:
    def __init__(self):
        self.contexts = []
        self.execution_attribution = {
            "schema_version": 1,
            "record_status": "recorded_at_run_start",
            "backend": "llm",
            "agent_engine": "bioma",
            "harness_mode": "governed",
            "models": {"scientific_worker": "fixture-model"},
        }

    def query(self, query, **kwargs):
        self.contexts.append(list(kwargs.get("session_context", [])))
        state = ScientificState.from_task(
            ScientificTask(objective=query, initial_agent="predagent")
        )
        return state.model_copy(
            update={
                "status": "completed",
                "predictions": [{"design": f"answer: {query}"}],
            }
        )


def test_web_serves_page_and_reports_live_run_state():
    server = create_server(SessionProbeBioMA(), backend="llm", port=0)
    thread = Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base_url) as response:
            assert b"BioMA" in response.read()

        request = Request(
            f"{base_url}/api/query",
            data=json.dumps({"query": "Test a web query"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            run_id = json.load(response)["run_id"]

        for _ in range(200):
            with urlopen(f"{base_url}/api/runs/{run_id}") as response:
                payload = json.load(response)
            if payload["status"] != "running":
                break
            time.sleep(0.02)

        assert payload["backend"] == "llm"
        assert payload["status"] == "completed"
        assert payload["active_agent"] is None
        assert payload["live_phase"] is None
        assert payload["state"]["status"] == "completed"
        assert payload["response"] == "answer: Test a web query"
        assert payload["execution_attribution"]["agent_engine"] == "bioma"
        assert payload["execution_attribution"]["request_origin"] == "web_api"
        assert payload["execution_attribution"]["host_contributions"] == {
            "query_supplied": True,
            "initial_files_supplied": False,
            "initial_file_count": 0,
            "initial_file_paths": [],
            "prior_session_context_available": False,
        }
        with urlopen(f"{base_url}/api/runs") as response:
            history = json.load(response)["runs"]
        assert history[0]["run_id"] == run_id
        assert history[0]["status"] == "completed"
        with urlopen(f"{base_url}/api/runtime") as response:
            runtime = json.load(response)
        assert runtime["execution_attribution"]["models"] == {
            "scientific_worker": "fixture-model"
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_featured_case_endpoint_reconciles_catalog_with_backend_history():
    server = create_server(SessionProbeBioMA(), backend="llm", port=0)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/api/featured-cases"
        ) as response:
            payload = json.load(response)
        assert payload["cases"]
        assert all(
            {"status", "available", "artifact_count", "reason"} <= set(item)
            for item in payload["cases"]
        )
        assert all(item["available"] is False for item in payload["cases"])
        assert all(item["artifact_count"] is None for item in payload["cases"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_reports_failure_context_and_traceback():
    server = create_server(FailingBioMA(), backend="llm", port=0)
    run = server.start_run("test failure")
    for _ in range(50):
        payload = run.payload("llm")
        if payload["status"] == "failed":
            break
        time.sleep(0.01)

    assert payload["failure"]["agent"] == "predagent"
    assert payload["failure"]["state_version"] == 0
    assert payload["failure"]["exception_type"] == "RuntimeError"
    assert "coordinator returned invalid output" in payload["failure"]["traceback"]
    assert payload["events"][-1]["type"] == "run.failed"
    server.server_close()


def test_web_serializes_runs_over_shared_agents():
    bioma = ConcurrentProbeBioMA()
    server = create_server(bioma, backend="toy", port=0)
    first = server.start_run("first")
    second = server.start_run("second")
    for _ in range(100):
        if first.status != "running" and second.status != "running":
            break
        time.sleep(0.01)

    assert first.status == "completed"
    assert second.status == "completed"
    assert bioma.max_active == 1
    server.server_close()


def test_web_passes_only_project_scoped_table_files_to_query_api(tmp_path):
    bioma = FileProbeBioMA()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    table = data_dir / "results.csv"
    table.write_text("sample,value\na,1\n", encoding="utf-8")
    server = create_server(
        bioma, backend="toy", port=0, artifact_root=tmp_path
    )
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/query",
            data=json.dumps(
                {"query": "Analyze this table", "files": ["data/results.csv"]}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request):
            pass
        for _ in range(50):
            if bioma.files:
                break
            time.sleep(0.01)
        assert bioma.files == [str(table.resolve())]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_rejects_input_files_outside_the_configured_root(tmp_path):
    bioma = FileProbeBioMA()
    server = create_server(
        bioma, backend="toy", port=0, artifact_root=tmp_path
    )
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/query",
            data=json.dumps(
                {"query": "Analyze this table", "files": [str(__file__)]}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 400
        else:  # pragma: no cover - protects the HTTP boundary contract
            raise AssertionError("Out-of-root input path was accepted")
        assert bioma.files == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_uses_fast_polling_and_visual_peer_network():
    page = files("bioma").joinpath("static/index.html").read_text(encoding="utf-8")

    from html.parser import HTMLParser

    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()
        def handle_starttag(self, tag, attrs):
            self.ids.update(v for k, v in attrs if k == "id")

    elements = Elements()
    elements.feed(page)
    assert {"peer-network", "history-list", "new-session", "activity-timeline",
            "gateway-view", "agent-list", "backend-health", "backend-mode"} <= elements.ids
    assert "setTimeout(()=>poll(id,token),200)" in page
    assert "api/runtime" in page


def test_homepage_keeps_original_four_cases_before_dynamic_examples():
    page = files("bioma").joinpath("static/index.html").read_text(encoding="utf-8")

    from html.parser import HTMLParser

    class HomepageCases(HTMLParser):
        def __init__(self):
            super().__init__()
            self.keys = []

        def handle_starttag(self, tag, attrs):
            if tag == "button":
                values = dict(attrs)
                if "data-home-case" in values:
                    self.keys.append(values["data-home-case"])

    cases = HomepageCases()
    cases.feed(page)
    assert cases.keys == ["analysis", "gateway", "structure", "multi"]
    assert 'id="featured-cases"' in page


def test_workspace_does_not_render_unknown_backend_state_as_zero():
    page = files("bioma").joinpath("static/index.html").read_text(encoding="utf-8")
    workspace = files("bioma").joinpath("static/workspace.js").read_text(encoding="utf-8")
    assert "routing —" in page
    assert "hasState?v:'—'" in page
    assert "execution_attribution?.agent_engine" in page
    assert "d.state?(d.state.artifacts||[]).length:'—'" in page
    assert "d.run_id&&d.state?buildEvidenceGraph(d).nodes.length:'—'" in workspace
    assert "api/featured-cases" in workspace
    assert "entries=catalog.cases||[]" in workspace
    assert ".filter(entry=>entry.available||entry.prompt)" not in workspace
    assert "else{card.disabled=true" in workspace
    assert "loadSession(entry.session_id,true,entry.run_id)" in workspace
    assert "artifactType==='pdf'" in workspace
    assert "card.type='button'" in workspace


def test_web_serves_local_ngl_bundle(tmp_path):
    server = create_server(
        ArtifactProbeBioMA(tmp_path / "unused"),
        backend="toy",
        port=0,
        artifact_root=tmp_path,
    )
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/static/vendor/ngl.js"
        ) as response:
            content = response.read()
        assert len(content) > 1_000_000
        assert b"NGL" in content[:5000]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_serves_registered_artifacts_within_the_configured_root(tmp_path):
    artifact = tmp_path / "analysis.json"
    artifact.write_text('{"result":"ok"}', encoding="utf-8")
    server = create_server(
        ArtifactProbeBioMA(artifact),
        backend="toy",
        port=0,
        artifact_root=tmp_path,
    )
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        run = server.start_run("artifact probe")
        for _ in range(50):
            if run.status != "running":
                break
            time.sleep(0.01)
        url = (
            f"http://127.0.0.1:{server.server_port}/api/runs/"
            f"{run.id}/artifacts/report-json"
        )
        with urlopen(url) as response:
            assert response.headers["Content-Type"] == "application/json"
            assert json.load(response) == {"result": "ok"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_history_survives_server_restart(tmp_path):
    history_path = tmp_path / "history.sqlite3"
    first = create_server(
        SessionProbeBioMA(),
        backend="toy",
        port=0,
        history_path=history_path,
    )
    run = first.start_run("Predict a GFP variant")
    for _ in range(50):
        if run.status != "running":
            break
        time.sleep(0.01)
    run_id = run.id
    first.server_close()

    second = create_server(
        SessionProbeBioMA(),
        backend="toy",
        port=0,
        history_path=history_path,
    )
    try:
        restored = second.history.get(run_id)
        assert restored["query"] == "Predict a GFP variant"
        assert restored["status"] == "completed"
        assert second.history.list_runs()[0]["run_id"] == run_id
    finally:
        second.server_close()


def test_web_session_keeps_multiple_turns_and_passes_prior_context():
    bioma = SessionProbeBioMA()
    server = create_server(bioma, backend="toy", port=0)
    thread = Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(
            Request(f"{base_url}/api/sessions", data=b"{}", method="POST")
        ) as response:
            session_id = json.load(response)["session_id"]

        run_ids = []
        for query in ("first question", "follow up on that result"):
            request = Request(
                f"{base_url}/api/sessions/{session_id}/query",
                data=json.dumps({"query": query}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                run_id = json.load(response)["run_id"]
            run_ids.append(run_id)
            for _ in range(50):
                with urlopen(f"{base_url}/api/runs/{run_id}") as response:
                    run = json.load(response)
                if run["status"] != "running":
                    break
                time.sleep(0.01)

        with urlopen(f"{base_url}/api/sessions/{session_id}") as response:
            session = json.load(response)
        assert [item["role"] for item in session["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert bioma.contexts[0] == []
        assert [item["content"] for item in bioma.contexts[1][:2]] == [
            "first question",
            "answer: first question",
        ]
        assert bioma.contexts[1][2]["role"] == "system"
        assert bioma.contexts[1][2]["state"]["status"] == "completed"
        assert [item["run_id"] for item in session["runs"]] == run_ids
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('body', [
    [], {'query': ['not text']}, {'query': 'hello', 'files': ''},
    {'query': 'hello', 'session_id': {}},
    {'query': 'hello', 'session_id': 'bad/id'},
])
def test_web_rejects_invalid_request_shapes_without_starting_work(body):
    server = create_server(SessionProbeBioMA(), backend='llm', port=0)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        request = Request(f'http://127.0.0.1:{server.server_port}/api/query',
                          data=json.dumps(body).encode(), method='POST')
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=2)
        assert exc.value.code == 400
        assert json.load(exc.value)['error']
        assert not server.runs
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_idle_browser_connection_does_not_block_api():
    import socket
    from types import SimpleNamespace
    app = SessionProbeBioMA()
    app.runtime = SimpleNamespace(agents={
        'compute_provider': SimpleNamespace(family='scientific.compute', bindings={'sequence_descriptors': None})
    })
    app.query_actor = 'query_contract_compiler'
    server = create_server(app, backend='llm', host='0.0.0.0', port=0)
    thread = Thread(target=server.serve_forever)
    thread.start()
    idle = socket.create_connection(('127.0.0.1', server.server_port))
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/runtime', timeout=2) as response:
            payload = json.load(response)
        assert payload['backend'] == 'llm'
        assert payload['query_actor'] == 'query_contract_compiler'
        assert payload['providers'] == [{'id': 'compute_provider', 'family': 'scientific.compute',
                                          'methods': ['sequence_descriptors']}]
        assert 'api_key' not in json.dumps(payload)
    finally:
        idle.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_preserves_blocked_reason_and_unqualified_result():
    from bioma.web import _run_response
    blocked = _run_response({'status': 'blocked', 'status_reason': {'code': 'missing_information',
                                                                 'fields': ['sequence']}})
    assert 'sequence' in blocked and 'missing_information' in blocked
    result = _run_response({'status': 'completed', 'observations': [
        {'kind': 'work_result', 'value': {'kind': 'computation', 'authority': 'unqualified'}}]})
    assert 'computation' in result and '不等同于已核验的科学证据' in result
    assert _run_response({'intent': 'protocol_design', 'protocol': {'id': 'empty'}, 'evidence': []}) == ''


def test_web_cli_passes_host_config_and_private_env(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from bioma import web
    calls = {}
    def build(**kwargs):
        calls['bioma'] = kwargs
        return 'production-instance'
    def serve(instance, **kwargs):
        calls['instance'] = instance
        calls['server'] = kwargs
        return SimpleNamespace(server_port=8123, serve_forever=lambda: None, server_close=lambda: None)
    monkeypatch.setattr(web, 'BioMA', build)
    monkeypatch.setattr(web, 'create_server', serve)
    monkeypatch.setenv('BIOMA_ENV_FILE', '')
    monkeypatch.setattr('sys.argv', ['bioma.web', '--host', '0.0.0.0', '--port', '8123',
                                    '--config', str(tmp_path/'config.yaml'),
                                    '--env-file', str(tmp_path/'private.env'),
                                    '--task-deadline-seconds', '1200',
                                    '--query-deadline-seconds', '1800'])
    web.main()
    assert calls['bioma'] == {'backend': 'llm', 'config_path': tmp_path/'config.yaml',
                              'task_deadline_seconds': 1200.0, 'query_deadline_seconds': 1800.0}
    assert calls['server']['host'] == '0.0.0.0'
    assert calls['server']['backend'] == 'llm'
    assert web.os.environ['BIOMA_ENV_FILE'] == str(tmp_path/'private.env')


@pytest.mark.parametrize('asset,mime', [
    ('workspace.css', 'text/css'),
    ('workspace-data.js', 'text/javascript'),
    ('workspace.js', 'text/javascript'),
])
def test_web_serves_workspace_assets(asset, mime):
    server = create_server(SessionProbeBioMA(), backend='llm', port=0)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/static/{asset}') as response:
            assert response.status == 200
            assert response.headers['Content-Type'].startswith(mime)
            assert response.headers['Cache-Control'] == 'no-store'
            assert len(response.read()) > 100
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_web_cli_rejects_unbounded_or_invalid_deadlines(monkeypatch, value):
    from bioma import web
    monkeypatch.setattr('sys.argv', ['bioma.web', '--task-deadline-seconds', value])
    with pytest.raises(SystemExit) as error:
        web.main()
    assert error.value.code == 2
