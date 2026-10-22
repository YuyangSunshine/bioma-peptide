from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any

from bioma.core.models import utc_now


class RunStore:
    """Local persistence for complete Web run snapshots."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = Lock()
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, payload: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        encoded = json.dumps(payload, ensure_ascii=False)
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO runs (id, query, status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    payload["run_id"],
                    payload["query"],
                    payload["status"],
                    payload["created_at"],
                    now,
                    encoded,
                ),
            )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload, updated_at FROM runs ORDER BY created_at DESC"
            ).fetchall()
        result = []
        for encoded, updated_at in rows:
            payload = json.loads(encoded)
            state = payload.get("state") or {}
            result.append(
                {
                    "run_id": payload["run_id"],
                    "session_id": payload.get("session_id") or payload["run_id"],
                    "query": payload["query"],
                    "status": payload["status"],
                    "intent": state.get("intent"),
                    "active_agents": state.get("active_agents") or [],
                    "created_at": payload["created_at"],
                    "updated_at": updated_at,
                }
            )
        return result

    def session_messages(
        self,
        session_id: str,
        *,
        exclude_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM runs ORDER BY created_at ASC"
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for (encoded,) in rows:
            payload = json.loads(encoded)
            payload_session = payload.get("session_id") or payload["run_id"]
            if payload_session != session_id or payload["run_id"] == exclude_run_id:
                continue
            messages.append(
                {
                    "role": "user",
                    "content": payload["query"],
                    "run_id": payload["run_id"],
                }
            )
            response = _assistant_response(payload)
            if payload.get("status") != "running" and response:
                messages.append(
                    {
                        "role": "assistant",
                        "content": response,
                        "run_id": payload["run_id"],
                        "status": payload.get("status"),
                    }
                )
        return messages

    def execution_context(
        self,
        session_id: str,
        *,
        exclude_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        messages = self.session_messages(
            session_id,
            exclude_run_id=exclude_run_id,
        )
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload FROM runs ORDER BY created_at DESC"
            ).fetchall()
        for (encoded,) in rows:
            payload = json.loads(encoded)
            payload_session = payload.get("session_id") or payload["run_id"]
            if (
                payload_session != session_id
                or payload["run_id"] == exclude_run_id
                or payload.get("status") == "running"
            ):
                continue
            state = payload.get("state") or {}
            messages.append(
                {
                    "role": "system",
                    "content": "Previous committed scientific state",
                    "state": {
                        "task_id": state.get("task_id"),
                        "intent": state.get("intent"),
                        "status": state.get("status"),
                        "version": state.get("version"),
                        "predictions": (state.get("predictions") or [])[-2:],
                        "protocol": state.get("protocol") or {},
                        "metrics": state.get("metrics") or {},
                        "evidence": (state.get("evidence") or [])[-6:],
                        "artifacts": state.get("artifacts") or [],
                    },
                }
            )
            break
        return messages

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload, updated_at FROM runs ORDER BY created_at ASC"
            ).fetchall()
        sessions: dict[str, dict[str, Any]] = {}
        for encoded, updated_at in rows:
            payload = json.loads(encoded)
            session_id = payload.get("session_id") or payload["run_id"]
            session = sessions.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "title": _session_title(payload["query"]),
                    "created_at": payload["created_at"],
                    "updated_at": updated_at,
                    "status": payload["status"],
                    "turn_count": 0,
                },
            )
            session["updated_at"] = updated_at
            session["status"] = payload["status"]
            session["turn_count"] += 1
        return sorted(
            sessions.values(), key=lambda item: item["updated_at"], reverse=True
        )

    def close(self) -> None:
        with self.lock:
            self.connection.close()


def _assistant_response(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return f"运行失败：{payload['error']}"
    return str(
        payload.get("response")
        or payload.get("conclusion")
        or payload.get("prediction")
        or ""
    )


def _session_title(query: str, limit: int = 84) -> str:
    without_sequence = re.split(
        r"(?:sequence|序列)\s*[:：]", query, maxsplit=1, flags=re.IGNORECASE
    )[0]
    compact = " ".join(without_sequence.split()).strip(" .，。") or "Research session"
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"
