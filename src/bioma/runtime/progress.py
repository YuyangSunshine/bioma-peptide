"""Bound model loops by useful observations, independently of total budgets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


# Diagnostics and orchestration bookkeeping are not scientific progress.
_DIAGNOSTICS = {
    "summary",
    "warnings",
    "timestamp",
    "created_at",
    "updated_at",
    "elapsed_seconds",
    "duration_seconds",
    "stage_log",
    "messages",
    "harness_observations",
    "harness_telemetry",
    "harness_workflow_recommendation",
    "next_recommendation",
    "workflow_recommendation",
    "error_code",
    "error_detail",
}


def _content(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _content(v) for k, v in value.items() if k not in _DIAGNOSTICS}
    if isinstance(value, (list, tuple)):
        return [_content(v) for v in value]
    return value


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def observation_failed(observation: Any) -> bool:
    return isinstance(observation, Mapping) and (
        observation.get("success") is False
        or bool(observation.get("error"))
        or observation.get("status") in ("error", "failed", "blocked")
    )


class ProgressGuard:
    """Count consecutive rounds without new successful results or state values.

    Arguments identify independent objects. Failed calls never buy more retries by
    varying arguments or error prose. Seen values survive progress resets so cycles
    cannot masquerade as progress. No wall-clock threshold interrupts long tools.
    """

    def __init__(self, max_stalled_steps: int = 3, *, state: Any = None) -> None:
        if max_stalled_steps < 1:
            raise ValueError("max_stalled_steps must be positive")
        self.limit = max_stalled_steps
        self.stalled_steps = 0
        self._seen_results: set[str] = set()
        self._seen_state: set[str] = set()
        self._record_state(state)
        self._advanced = False
        self.last_tool: str | None = None
        self.last_status: str | None = None

    def _record_state(self, state: Any) -> bool:
        values = _content(state or {})
        novel = False
        for key, value in values.items():
            token = _fingerprint([key, value])
            novel |= token not in self._seen_state
            self._seen_state.add(token)
        return novel

    def observe(
        self, name: str, arguments: Any, observation: Any, *, state: Any = None
    ) -> None:
        self.last_tool = name
        failed = observation_failed(observation)
        self.last_status = "failed" if failed else "ok"
        if failed:
            return
        token = _fingerprint([name, arguments, _content(observation)])
        new_result = token not in self._seen_results
        self._seen_results.add(token)
        new_state = self._record_state(state)
        self._advanced |= new_result or new_state

    def finish_step(self, *, count_stall: bool = True) -> bool:
        self.stalled_steps = 0 if self._advanced else self.stalled_steps + int(count_stall)
        self._advanced = False
        return self.stalled_steps >= self.limit

    def diagnostic(self) -> dict[str, Any]:
        return {
            "reason": "no_progress",
            "stalled_steps": self.stalled_steps,
            "limit": self.limit,
            "last_tool": self.last_tool,
            "last_status": self.last_status,
        }

    def feedback(self) -> str:
        return (
            f"No effective progress for {self.stalled_steps}/{self.limit} rounds. "
            "Repeated results and failed calls do not advance the task. "
            "Resolve the reported prerequisite or change the approach; "
            "otherwise stop and explain the blocker without claiming completion."
        )
