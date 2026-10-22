from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from bioma.backends.analysis.workspace import AnalysisState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_user_message(
    messages: List[Dict[str, Any]] | None, content: str
) -> List[Dict[str, Any]]:
    msgs = list(messages or [])
    if content and content.strip():
        msgs.append(
            {
                "role": "user",
                "content": content.strip(),
                "ts": _utc_now_iso(),
            }
        )
    return msgs


def _normalize_file_inputs(raw_data_sources: Any) -> List[str]:
    if raw_data_sources is None:
        return []
    if isinstance(raw_data_sources, list):
        return [str(x) for x in raw_data_sources if str(x).strip()]
    s = str(raw_data_sources).strip()
    return [s] if s else []


def _bootstrap_turn_intent(state: AnalysisState) -> Dict[str, Any]:
    text = (state.get("current_user_request") or "").lower()
    has_new_files = bool(state.get("has_new_files"))
    status = state.get("status") or ""

    if has_new_files:
        if state.get("data_artifact"):
            return {
                "type": "add_files",
                "summary": "User added new files.",
                "confidence": 0.9,
            }
        return {
            "type": "new_task",
            "summary": "New task with uploaded files.",
            "confidence": 0.95,
        }

    if status == "waiting_user":
        return {
            "type": "clarification_reply",
            "summary": "User replied to a clarification question.",
            "confidence": 0.75,
        }

    if any(
        k in text
        for k in [
            "改图",
            "重新画",
            "换成",
            "折线图",
            "柱状图",
            "heatmap",
            "plot",
            "chart",
        ]
    ):
        return {
            "type": "modify_plot",
            "summary": "User requests plot changes.",
            "confidence": 0.7,
        }

    if any(
        k in text
        for k in ["改报告", "重写报告", "简洁一点", "详细一点", "润色", "report"]
    ):
        return {
            "type": "modify_report",
            "summary": "User requests report revision.",
            "confidence": 0.7,
        }

    if any(k in text for k in ["为什么", "解释", "怎么看", "说明一下", "explain"]):
        return {
            "type": "ask_explanation",
            "summary": "User asks for explanation.",
            "confidence": 0.7,
        }

    return {
        "type": "unknown",
        "summary": "Turn intent not determined yet.",
        "confidence": 0.3,
    }


def intake_node(state: AnalysisState) -> Dict[str, Any]:
    current_user_request = (state.get("current_user_request") or "").strip()
    raw_data_sources = _normalize_file_inputs(state.get("raw_data_sources"))

    out: Dict[str, Any] = {
        "messages": _append_user_message(state.get("messages"), current_user_request),
        "current_user_request": current_user_request,
        "raw_data_sources": raw_data_sources,
        "has_new_files": bool(raw_data_sources),
        "updated_at": _utc_now_iso(),
        "status": state.get("status") or "active",
        "error_code": None,
        "error_detail": None,
    }

    # 只有当 turn_intent 还没被外部明确设置时，才做 bootstrap
    if not state.get("turn_intent"):
        bootstrap_state = {**state, **out}
        out["turn_intent"] = _bootstrap_turn_intent(bootstrap_state)

    return out
