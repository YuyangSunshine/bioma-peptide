from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import mimetypes
import os
import sys
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from bioma import BioMA, ScientificState
from bioma.core.models import ScientificTask, utc_now
from bioma.history import RunStore

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_QUERY_CHARS = 20_000
MAX_INPUT_FILES = 32


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, Any]:
    """Describe the concrete Python implementation serving this Web process.

    Report resolved module paths and hashes to detect stale server processes
    or an editable install pointing at another checkout.
    """
    modules = (
        "bioma",
        "bioma.api",
        "bioma.core.models",
        "bioma.gateway",
        "bioma.runtime.engine",
    )
    loaded: list[dict[str, str]] = []
    for name in modules:
        module = importlib.import_module(name)
        origin = str(getattr(getattr(module, "__spec__", None), "origin", "") or getattr(module, "__file__", ""))
        path = Path(origin)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        loaded.append(
            {
                "module": name,
                "origin": origin,
                "kind": "bytecode" if origin.endswith(".pyc") else "source",
                "sha256": digest,
            }
        )
    return {
        "python": sys.version,
        "executable": sys.executable,
        "cwd": str(Path.cwd()),
        "modules": loaded,
        # Connection diagnostics deliberately expose only presence, never proxy
        # addresses, credentials, or API keys.  A Web worker may inherit a
        # different network environment from an interactive terminal.
        "network_environment": {
            key: bool(os.environ.get(key))
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
        },
    }


@dataclass
class WebRun:
    query: str
    session_id: str
    files: list[str] = field(default_factory=list)
    execution_attribution: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "running"
    active_agent: str | None = None
    message: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    failure: dict[str, Any] | None = None
    live_phase: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: utc_now().isoformat())

    def receive(self, event: dict[str, Any]) -> None:
        if event.get("type") == "gateway.interpretation.started":
            # A pre-runtime phase: retain it for the live UI without changing the
            # durable runtime event sequence or its protocol ordering.
            self.active_agent = str(event.get("agent") or "queryagent")
            self.live_phase = dict(event)
            return
        if event.get('type') in {'llm.started', 'llm.completed', 'llm.failed', 'plan.step.started', 'tool.started', 'tool.completed', 'tool.failed', 'skill.loaded'}:
            self.live_phase = {**event, 'detail': event.get('detail') or ' / '.join(str(event.get(k)) for k in ('type','tool','model','skill') if event.get(k))}
        if "state" in event:
            self.state = event["state"]
        summary = {
            key: value
            for key, value in event.items()
            if key not in {"state", "delta", "message"}
        }
        summary["state_version"] = event.get(
            "state_version", (self.state or {}).get("version", 0)
        )
        if event["type"] == "agent.started":
            self.active_agent = event["agent"]
            self.message = event["message"]
            summary.update(
                {
                    "sender": event["message"]["sender"],
                    "recipient": event["message"]["recipient"],
                    "message_type": event["message"]["message_type"],
                }
            )
        elif event["type"] == "state.committed":
            self.active_agent = None
            delta = event["delta"]
            summary.update(
                {
                    "outcome": delta["outcome"],
                    "evidence": delta.get("evidence", []),
                    "artifacts": delta.get("artifacts", []),
                    "task_requests": delta.get("task_requests", []),
                    "offers": delta.get("offers", []),
                    "commitments": delta.get("commitments", []),
                }
            )
        elif event.get("agent"):
            self.active_agent = event["agent"]
        self.events.append(summary)
        if len(self.events) > 500:
            del self.events[:-500]

    def finish(self, state: ScientificState) -> None:
        self.state = state.model_dump(mode="json")
        self.status = state.status
        self.active_agent = None
        self.live_phase = None

    def fail(self, exc: Exception) -> None:
        state_version = (self.state or {}).get("version", 0)
        self.failure = {
            "exception_type": type(exc).__name__,
            "message": _public_failure_message(exc),
            "technical_message": str(exc),
            "traceback": traceback.format_exc(),
            "agent": self.active_agent,
            "message_type": (self.message or {}).get("message_type"),
            "state_version": state_version,
        }
        self.events.append(
            {
                "timestamp": utc_now().isoformat(),
                "type": "run.failed",
                "agent": self.active_agent,
                "message_type": (self.message or {}).get("message_type"),
                "state_version": state_version,
                "exception_type": type(exc).__name__,
            }
        )
        self.status = "failed"
        self.active_agent = None
        self.error = _public_failure_message(exc)

    def payload(self, backend: str) -> dict[str, Any]:
        predictions = (self.state or {}).get("predictions") or []
        evidence = (self.state or {}).get("evidence") or []
        return {
            "run_id": self.id,
            "session_id": self.session_id,
            "query": self.query,
            "files": self.files,
            "backend": backend,
            "created_at": self.created_at,
            "status": self.status,
            "active_agent": self.active_agent,
            "message": self.message,
            "state": self.state,
            "events": self.events,
            "prediction": predictions[-1].get("design") if predictions else None,
            "conclusion": evidence[-1].get("claim") if evidence else None,
            "response": _run_response(self.state),
            "error": self.error,
            "failure": self.failure,
            "live_phase": self.live_phase,
            "execution_attribution": self.execution_attribution,
            "runtime_identity": runtime_identity(),
        }


class BioMAWebServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        bioma: BioMA,
        backend: str,
        history: RunStore,
        artifact_root: Path,
    ) -> None:
        super().__init__(address, QueryHandler)
        self.bioma = bioma
        self.backend = backend
        self.history = history
        self.artifact_root = artifact_root.resolve()
        self.runs: dict[str, WebRun] = {}
        self.sessions: set[str] = set()
        self.run_lock = Lock()
        self.session_lock = Lock()
        self._workers: list[Thread] = []
        self._restore_pending_runs()

    def _restore_pending_runs(self) -> None:
        ledger = getattr(getattr(self.bioma, "runtime", None), "ledger", None)
        if ledger is None:
            return
        for summary in self.history.list_runs():
            if summary["status"] != "running":
                continue
            saved = self.history.get(summary["run_id"])
            task_data = next((e.get("task") for e in saved.get("events", [])
                              if e.get("type") == "goal.submitted"), None)
            task_id = (task_data or {}).get("id") or (saved.get("state") or {}).get("task_id")
            if not task_id:
                continue  # Pre-admission requests have no durable task/outbox yet.
            initialized = next((e for e in ledger.events(task_id)
                                if e.kind == "RunInitialized"), None)
            if initialized is None:
                continue
            run = WebRun(query=saved["query"], session_id=saved["session_id"],
                         id=saved["run_id"], files=saved.get("files", []),
                         execution_attribution=saved.get("execution_attribution", {}),
                         events=saved.get("events", []), state=saved.get("state"),
                         created_at=saved["created_at"])
            self.runs[run.id] = run
            self.sessions.add(run.session_id)
            worker = Thread(target=self._resume_run,
                            args=(run, initialized.payload["task"]), daemon=True)
            self._workers.append(worker)
            worker.start()

    def _resume_run(self, run: WebRun, task_data: dict[str, Any]) -> None:
        import time

        from bioma.runtime.task_lease import TaskLeaseBusy
        try:
            with self.run_lock:
                deadline = time.monotonic() + 35
                while True:
                    try:
                        run.finish(self.bioma.run(ScientificTask.model_validate(task_data),
                            event_sink=lambda event: self._receive(run, event)))
                        break
                    except TaskLeaseBusy:
                        if time.monotonic() >= deadline:
                            raise
                        self._receive(run, {"type": "runtime.lease_wait", "agent": "harness",
                            "detail": "Waiting for the previous task owner lease to expire."})
                        time.sleep(1)
        except Exception as exc:  # noqa: BLE001 - capture resumed run failures
            run.fail(exc)
        self._persist(run)

    def start_session(self) -> str:
        session_id = str(uuid4())
        with self.session_lock:
            self.sessions.add(session_id)
        return session_id

    def runtime_payload(self) -> dict[str, Any]:
        providers = getattr(getattr(self.bioma, "runtime", None), "agents", {})
        return {
            **runtime_identity(),
            "backend": self.backend,
            "execution_attribution": getattr(self.bioma, "execution_attribution", {
                "record_status": "unavailable",
                "backend": self.backend,
            }),
            "query_actor": getattr(self.bioma, "query_actor", "query_contract_compiler"),
            "providers": [
                {"id": name, "family": getattr(provider, "family", ""),
                 "methods": sorted(getattr(provider, "bindings", {}))}
                for name, provider in providers.items()
            ],
        }

    def start_run(
        self,
        query: str,
        files: list[str] | None = None,
        *,
        session_id: str | None = None,
    ) -> WebRun:
        session_id = session_id or self.start_session()
        with self.session_lock:
            self.sessions.add(session_id)
        supplied_files = files or []
        attribution = dict(getattr(self.bioma, "execution_attribution", {}))
        attribution["request_origin"] = "web_api"
        attribution["host_contributions"] = {
            "query_supplied": True,
            "initial_files_supplied": bool(supplied_files),
            "initial_file_count": len(supplied_files),
            "initial_file_paths": list(supplied_files),
            "prior_session_context_available": bool(
                self.history.execution_context(session_id)
            ),
        }
        attribution["autonomy_scope"] = (
            "Execution begins after the host supplies the query and initial files. "
            "This record does not classify unrecorded work performed before submission."
        )
        run = WebRun(query=query, session_id=session_id, files=supplied_files,
                     execution_attribution=attribution)
        self.runs[run.id] = run
        self._persist(run)
        worker = Thread(target=self._execute, args=(run,), daemon=True)
        self._workers.append(worker)
        worker.start()
        return run

    def _execute(self, run: WebRun) -> None:
        try:
            # Show queueing immediately, including when another shared-runtime
            # task still owns the execution lock.
            self._receive(
                run,
                {
                    "timestamp": utc_now().isoformat(),
                    "type": "gateway.interpretation.started",
                    "agent": getattr(self.bioma, "query_actor", "query_contract_compiler"),
                    "state_version": 0,
                    "message_type": "query_interpretation",
                    "detail": "Queued for Gateway interpretation; the shared runtime will start this request in order.",
                },
            )
            with self.run_lock:
                # Query interpretation happens before the decentralized runtime
                # emits its first peer event.  Surface that otherwise invisible
                # interval in the Web timeline rather than leaving the user with
                # a blank "running" page while the Gateway calls its LLM.
                self._receive(
                    run,
                    {
                        "timestamp": utc_now().isoformat(),
                        "type": "gateway.interpretation.started",
                        "agent": getattr(self.bioma, "query_actor", "query_contract_compiler"),
                        "state_version": 0,
                        "message_type": "query_interpretation",
                        "detail": "Interpreting goals, constraints, missing information, and capability requirements.",
                    },
                )
                kwargs = {
                    "files": run.files,
                    "event_sink": lambda event: self._receive(run, event),
                }
                parameters = inspect.signature(self.bioma.query).parameters
                if "session_context" in parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
                ):
                    kwargs["session_context"] = self.history.execution_context(
                        run.session_id,
                        exclude_run_id=run.id,
                    )
                run.finish(self.bioma.query(run.query, **kwargs))
        except Exception as exc:  # noqa: BLE001 - web run captures extension failures
            run.fail(exc)
        self._persist(run)

    def _receive(self, run: WebRun, event: dict[str, Any]) -> None:
        run.receive(event)
        self._persist(run)

    def _persist(self, run: WebRun) -> None:
        self.history.save(run.payload(self.backend))

    def session_payload(self, session_id: str) -> dict[str, Any] | None:
        messages = self.history.session_messages(session_id)
        if not messages and session_id not in self.sessions:
            return None
        runs = [
            item
            for item in self.history.list_runs()
            if item["session_id"] == session_id
        ]
        runs.reverse()
        return {
            "session_id": session_id,
            "messages": messages,
            "runs": runs,
            "active_run_id": next(
                (item["run_id"] for item in runs if item["status"] == "running"),
                None,
            ),
        }

    def featured_cases_payload(self) -> dict[str, Any]:
        catalog = json.loads(
            files("bioma").joinpath("static/featured-cases.json").read_text(encoding="utf-8")
        )
        cases: list[dict[str, Any]] = []
        for entry in catalog:
            run = self.runs.get(entry["run_id"])
            payload = run.payload(self.backend) if run else self.history.get(entry["run_id"])
            state = (payload or {}).get("state") or {}
            artifacts = state.get("artifacts") or []
            by_name = {Path(str(item.get("uri") or "")).name: item for item in artifacts}

            def available_artifact(name: str) -> dict[str, Any] | None:
                artifact = by_name.get(name)
                if artifact is None:
                    return None
                target = Path(str(artifact.get("uri") or ""))
                target = target.resolve() if target.is_absolute() else (self.artifact_root / target).resolve()
                try:
                    target.relative_to(self.artifact_root)
                except ValueError:
                    return None
                return artifact if target.is_file() else None

            target = available_artifact(entry["target"])
            cover = available_artifact(entry["cover"])
            status = (payload or {}).get("status", "unavailable")
            ready = status == "completed" and target is not None
            cases.append({
                **entry,
                "status": status,
                "available": ready,
                "artifact_count": len(artifacts) if payload is not None else None,
                "cover_artifact": cover,
                "target_artifact": target,
                "reason": None if ready else (
                    "执行尚未完成" if status in {"queued", "running"}
                    else "已保存的目标交付不可用" if payload is not None
                    else "本机没有这条执行记录"
                ),
            })
        return {"cases": cases}

    def server_close(self) -> None:
        # Do not close the durable event store underneath a still-running Web
        # task.  This is also the isolation boundary for a graceful restart.
        super().server_close()
        for worker in tuple(self._workers):
            if worker.is_alive():
                worker.join()
        self.history.close()


class QueryHandler(BaseHTTPRequestHandler):
    server: BioMAWebServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            content = files("bioma").joinpath("static/index.html").read_bytes()
            self._send(200, content, "text/html; charset=utf-8")
            return
        if path == "/static/vendor/ngl.js":
            content = files("bioma").joinpath("static/vendor/ngl.js").read_bytes()
            self._send(200, content, "text/javascript; charset=utf-8")
            return
        workspace_assets = {
            "/static/featured-cases.json": "application/json; charset=utf-8",
            "/static/workspace.css": "text/css; charset=utf-8",
            "/static/workspace-data.js": "text/javascript; charset=utf-8",
            "/static/workspace.js": "text/javascript; charset=utf-8",
        }
        if path in workspace_assets:
            content = files("bioma").joinpath(path.lstrip("/")).read_bytes()
            self._send(200, content, workspace_assets[path])
            return
        if path == "/api/runs":
            self._json(200, {"runs": self.server.history.list_runs()})
            return
        if path == "/api/runtime":
            self._json(200, self.server.runtime_payload())
            return
        if path == "/api/featured-cases":
            self._json(200, self.server.featured_cases_payload())
            return
        if path == "/api/sessions":
            sessions = self.server.history.list_sessions()
            known = {item["session_id"] for item in sessions}
            with self.server.session_lock:
                empty_sessions = self.server.sessions - known
            sessions.extend(
                {
                    "session_id": session_id,
                    "title": "New research session",
                    "status": "idle",
                    "turn_count": 0,
                    "created_at": utc_now().isoformat(),
                    "updated_at": utc_now().isoformat(),
                }
                for session_id in empty_sessions
            )
            self._json(200, {"sessions": sessions})
            return
        if path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/")
            payload = self.server.session_payload(session_id)
            if payload is not None:
                self._json(200, payload)
                return
        if path.startswith("/api/runs/"):
            parts = path.removeprefix("/api/runs/").split("/")
            run_id = parts[0]
            run = self.server.runs.get(run_id)
            payload = (
                run.payload(self.server.backend)
                if run
                else self.server.history.get(run_id)
            )
            if len(parts) == 3 and parts[1] == "artifacts" and payload is not None:
                self._artifact(payload, unquote(parts[2]))
                return
            if len(parts) == 1 and payload is not None:
                self._json(200, payload)
                return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/sessions":
            self._json(201, {"session_id": self.server.start_session()})
            return
        session_id = None
        if path.startswith("/api/sessions/") and path.endswith("/query"):
            session_id = (
                path.removeprefix("/api/sessions/").removesuffix("/query").strip("/")
            )
            if not session_id:
                self.send_error(404)
                return
        elif path != "/api/query":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("Request body must be a non-empty JSON object.")
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "Request body is too large."})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise TypeError("Request body must be a JSON object.")
            query = payload.get("query", "")
            if not isinstance(query, str):
                raise TypeError("query must be a string.")
            query = query.strip()
            files = payload.get("files", [])
            session_id = session_id or payload.get("session_id")
            if session_id is not None and (
                not isinstance(session_id, str) or not session_id.strip()
                or len(session_id) > 128 or "/" in session_id
            ):
                raise ValueError("session_id must be a non-empty string of at most 128 characters without slashes.")
            if not query:
                raise ValueError("Query cannot be empty.")
            if len(query) > MAX_QUERY_CHARS:
                raise ValueError(
                    f"Query exceeds the {MAX_QUERY_CHARS}-character limit."
                )
            if not isinstance(files, list) or any(
                not isinstance(path, str) for path in files
            ):
                raise ValueError("files must be a list of paths.")
            if len(files) > MAX_INPUT_FILES:
                raise ValueError(f"At most {MAX_INPUT_FILES} input files are allowed.")
            files = _validated_input_files(self.server.artifact_root, files)
        except (AttributeError, TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        run = self.server.start_run(query, files, session_id=session_id)
        self._json(202, {"run_id": run.id, "session_id": run.session_id})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )

    def _artifact(self, payload: dict[str, Any], artifact_id: str) -> None:
        artifacts = (payload.get("state") or {}).get("artifacts") or []
        artifact = next(
            (item for item in artifacts if item.get("id") == artifact_id), None
        )
        if artifact is None:
            self.send_error(404)
            return
        target = Path(str(artifact.get("uri") or ""))
        target = (
            target.resolve()
            if target.is_absolute()
            else (self.server.artifact_root / target).resolve()
        )
        try:
            target.relative_to(self.server.artifact_root)
        except ValueError:
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        content_type = (
            mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        # Give coordinate files an explicit representation type.  This makes
        # their use as an NGL resource unambiguous to browsers and proxies.
        if target.suffix.casefold() == ".pdb":
            content_type = "chemical/x-pdb"
        elif target.suffix.casefold() in {".cif", ".mmcif"}:
            content_type = "chemical/x-cif"
        self._send(200, target.read_bytes(), content_type)

    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def create_server(
    bioma: BioMA,
    *,
    backend: str,
    host: str = "127.0.0.1",
    port: int = 8014,
    history_path: str | Path = ":memory:",
    artifact_root: str | Path | None = None,
) -> BioMAWebServer:
    root = Path(artifact_root) if artifact_root else Path.cwd()
    # Method tools are registered by the production Provider factory. The Web
    # adapter must not inject tools into retired Role implementations.
    return BioMAWebServer((host, port), bioma, backend, RunStore(history_path), root)


def _run_response(state: dict[str, Any] | None) -> str:
    state = state or {}
    if state.get("status") in {"blocked", "failed"}:
        reason = state.get("status_reason") or {}
        return "执行已停止：" + (json.dumps(reason, ensure_ascii=False) if reason else state["status"])
    predictions = state.get("predictions") or []
    evidence = state.get("evidence") or []
    intent = state.get("intent")
    if intent == "prediction_only" and predictions:
        return str(predictions[-1].get("design") or "")
    if intent == "protocol_design" and state.get("protocol"):
        return str(state["protocol"].get("text") or (evidence[-1].get("claim") if evidence else "") or "")
    if intent == "table_analysis" and evidence:
        value = evidence[-1].get("value") or {}
        return str(value.get("final_report") or evidence[-1].get("claim") or "")
    if evidence:
        return str(evidence[-1].get("claim") or "")
    if predictions:
        return str(predictions[-1].get("design") or "")
    observations = [o for o in state.get("observations", []) if o.get("kind") == "work_result"]
    if observations:
        kinds = list(dict.fromkeys(o.get("value", {}).get("kind", "work_result") for o in observations))
        return ("已记录工作结果：" + "、".join(kinds) + "。可在中间产物和科学状态中查看；"
                "工作结果本身不等同于已核验的科学证据。")
    if state.get("status") == "completed":
        return "本轮执行完成，请查看已记录的状态和产物。"
    return ""


def _public_failure_message(exc: Exception) -> str:
    """Expose an actionable category while retaining full diagnostics."""
    raw = str(exc)
    lowered = f"{type(exc).__name__}: {raw}".lower()
    if "account_overdue" in lowered:
        return "模型服务账户欠费（403 account_overdue）。请为百度千帆账户充值或配置有效凭据后重试；本轮未生成分析结果。"
    if "apiconnectionerror" in lowered or "connection error" in lowered:
        return (
            "LLM provider connection failed after bounded retries. No synthetic "
            "result was committed. Check the API endpoint and outbound network "
            "permission of the Web process."
        )
    if "ratelimit" in lowered or "429" in lowered:
        return (
            "LLM provider rate limit was reached. Retry after the provider window "
            "resets; the Harness did not commit an incomplete result."
        )
    if "scopeviolation" in lowered or "cannot terminate before" in lowered:
        return (
            "Harness rejected an incomplete terminal transition after bounded "
            "recovery because required evidence was still missing."
        )
    return raw


def _validated_input_files(root: Path, values: list[str]) -> list[str]:
    """Resolve HTTP-supplied paths inside the configured project boundary."""
    resolved: list[str] = []
    for value in values:
        raw = value.strip()
        if not raw:
            continue
        path = Path(raw)
        target = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Input file is outside the allowed root: {raw}") from exc
        if not target.is_file():
            raise ValueError(f"Input file does not exist: {raw}")
        resolved.append(str(target))
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local BioMA web interface.")
    parser.add_argument("--peptide-registry", type=Path, help="Host-maintained scientific profile/input registry")
    parser.add_argument("--peptide-job-root", type=Path, help="Persistent host-owned job directory")
    parser.add_argument("--control-policy", choices=["legacy", "adaptive_v3", "egvoi"], default="legacy", help="Research decision policy; independent of scientific method order")
    parser.add_argument("--toy", action="store_true", help="use deterministic backends")
    parser.add_argument("--host", default="127.0.0.1", help="listen address; use 0.0.0.0 for LAN access")
    parser.add_argument("--config", type=Path, help="BioMA YAML configuration path")
    parser.add_argument("--env-file", type=Path, help="explicit private environment file")
    parser.add_argument("--port", type=int, default=8014)
    parser.add_argument(
        "--history-path",
        type=Path,
        default=None,
        help="SQLite history path (defaults to .bioma/history.sqlite3)",
    )
    parser.add_argument("--query-deadline-seconds", type=float, default=600.0,
                        help="overall query deadline, including interpretation")
    parser.add_argument("--task-deadline-seconds", type=float, default=300.0,
                        help="scientific execution deadline for long analyses")
    args = parser.parse_args()
    import math
    if any(not math.isfinite(v) or v <= 0 for v in
           (args.query_deadline_seconds, args.task_deadline_seconds)):
        parser.error("deadlines must be positive finite seconds")
    if args.env_file:
        os.environ["BIOMA_ENV_FILE"] = str(args.env_file.expanduser().resolve())
    if bool(args.peptide_registry) != bool(args.peptide_job_root):
        parser.error("--peptide-registry and --peptide-job-root must be provided together")
    if args.peptide_registry:
        os.environ["BIOMA_PEPTIDE_REGISTRY"] = str(args.peptide_registry.resolve(strict=True))
        os.environ["BIOMA_PEPTIDE_JOB_ROOT"] = str(args.peptide_job_root.resolve())
    backend = "toy" if args.toy else "llm"
    print(f"Initializing BioMA ({backend})...", flush=True)
    history_path = args.history_path or (
        Path.cwd() / ".bioma" / "history.sqlite3"
    )
    server = create_server(
        BioMA(backend=backend, config_path=args.config, control_policy=args.control_policy,
              query_deadline_seconds=args.query_deadline_seconds,
              task_deadline_seconds=args.task_deadline_seconds),
        backend=backend,
        host=args.host,
        port=args.port,
        history_path=history_path,
    )
    print(f"Ready: http://{args.host}:{server.server_port}", flush=True)
    if args.host == "0.0.0.0":
        print(f"LAN: http://<server-LAN-IP>:{server.server_port}", flush=True)
    print("Keep this terminal open. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
