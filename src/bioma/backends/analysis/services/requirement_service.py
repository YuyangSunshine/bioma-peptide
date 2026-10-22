from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from bioma.backends.analysis.workspace import AnalysisState
from bioma.backends.analysis.tools.requirement_tools import (
    DEFAULT_REQUIREMENT,
    issues_to_text,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _as_list_str(x: Any) -> List[str]:
    out: List[str] = []
    for item in _as_list(x):
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _uniq_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _normalize_report_preferences(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    return {
        "tone": str(obj.get("tone") or "").strip(),
        "length": str(obj.get("length") or "").strip(),
        "language": str(obj.get("language") or "").strip(),
        "include_sections": _uniq_keep_order(_as_list_str(obj.get("include_sections"))),
    }


def _normalize_partial_requirement_obj(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    return {
        "primary_intent": str(obj.get("primary_intent") or "").strip(),
        "requested_outputs": _uniq_keep_order(
            _as_list_str(obj.get("requested_outputs"))
        ),
        "analysis_goals": _uniq_keep_order(_as_list_str(obj.get("analysis_goals"))),
        "chart_preferences": _uniq_keep_order(
            _as_list_str(obj.get("chart_preferences"))
        ),
        "report_preferences": _normalize_report_preferences(
            obj.get("report_preferences")
        ),
        "must_have_dimensions": _uniq_keep_order(
            _as_list_str(obj.get("must_have_dimensions"))
        ),
        "user_constraints": _uniq_keep_order(_as_list_str(obj.get("user_constraints"))),
        "notes": _uniq_keep_order(_as_list_str(obj.get("notes"))),
    }


def normalize_requirement_obj(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    return {
        "primary_intent": str(
            obj.get("primary_intent") or DEFAULT_REQUIREMENT["primary_intent"]
        ).strip(),
        "requested_outputs": _uniq_keep_order(
            _as_list_str(obj.get("requested_outputs"))
        ),
        "analysis_goals": _uniq_keep_order(_as_list_str(obj.get("analysis_goals"))),
        "chart_preferences": _uniq_keep_order(
            _as_list_str(obj.get("chart_preferences"))
        ),
        "report_preferences": _normalize_report_preferences(
            obj.get("report_preferences")
        ),
        "must_have_dimensions": _uniq_keep_order(
            _as_list_str(obj.get("must_have_dimensions"))
        ),
        "user_constraints": _uniq_keep_order(_as_list_str(obj.get("user_constraints"))),
        "notes": _uniq_keep_order(_as_list_str(obj.get("notes"))),
    }


def messages_to_dialog_history(
    messages: List[Dict[str, Any]] | None, max_turns: int = 12
) -> str:
    msgs = list(messages or [])
    if not msgs:
        return "<empty>"

    tail = msgs[-max_turns:]
    lines: List[str] = []
    for m in tail:
        role = str(m.get("role") or "unknown").strip()
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "<empty>"


def build_requirement_payload_from_state(state: AnalysisState) -> Dict[str, Any]:
    has_previous_requirement = bool(state.get("stable_requirement"))
    has_pending_question = bool(str(state.get("pending_question") or "").strip())
    mode = "update" if has_previous_requirement or has_pending_question else "initial"

    return {
        "mode": mode,
        "current_user_request": (state.get("current_user_request") or "").strip(),
        "dialog_history": messages_to_dialog_history(state.get("messages")),
        "previous_requirement": normalize_requirement_obj(
            state.get("stable_requirement") or DEFAULT_REQUIREMENT
        ),
        "pending_question": str(state.get("pending_question") or "").strip(),
        "previous_resolved_parser_target": state.get("resolved_parser_target") or {},
        "previous_experiment_background": state.get("experiment_background") or {},
        "data_artifact": state.get("data_artifact") or {},
        "data_prep_meta": state.get("data_prep_meta") or {},
    }


def merge_requirement_update(
    previous_requirement: Dict[str, Any],
    requirement_patch: Dict[str, Any],
) -> Dict[str, Any]:
    prev = normalize_requirement_obj(previous_requirement)
    patch = _normalize_partial_requirement_obj(requirement_patch)

    primary_intent = prev.get("primary_intent") or DEFAULT_REQUIREMENT["primary_intent"]
    patch_primary_intent = str(patch.get("primary_intent") or "").strip()
    if patch_primary_intent and patch_primary_intent != "unknown":
        primary_intent = patch_primary_intent

    merged = {
        "primary_intent": primary_intent,
        "requested_outputs": _uniq_keep_order(
            prev["requested_outputs"] + patch["requested_outputs"]
        ),
        "analysis_goals": _uniq_keep_order(
            prev["analysis_goals"] + patch["analysis_goals"]
        ),
        "chart_preferences": _uniq_keep_order(
            prev["chart_preferences"] + patch["chart_preferences"]
        ),
        "report_preferences": {
            "tone": patch["report_preferences"].get("tone")
            or prev["report_preferences"].get("tone")
            or "",
            "length": patch["report_preferences"].get("length")
            or prev["report_preferences"].get("length")
            or "",
            "language": patch["report_preferences"].get("language")
            or prev["report_preferences"].get("language")
            or "",
            "include_sections": _uniq_keep_order(
                prev["report_preferences"].get("include_sections", [])
                + patch["report_preferences"].get("include_sections", [])
            ),
        },
        "must_have_dimensions": _uniq_keep_order(
            prev["must_have_dimensions"] + patch["must_have_dimensions"]
        ),
        "user_constraints": _uniq_keep_order(
            prev["user_constraints"] + patch["user_constraints"]
        ),
        "notes": _uniq_keep_order(prev["notes"] + patch["notes"]),
    }
    return normalize_requirement_obj(merged)


def build_clarification_question(
    tool_result: Dict[str, Any], state: AnalysisState | None = None
) -> str:
    explicit = str(tool_result.get("question_to_user") or "").strip()
    if explicit:
        return explicit

    issues = tool_result.get("issues") or []
    text = issues_to_text(issues)
    if text:
        return f"为了继续处理这个任务，我还需要你确认这些信息：{text}"

    return "为了继续处理这个任务，我还需要你补充一些关键信息。"


def build_requirement_state_update(
    state: AnalysisState,
    tool_result: Dict[str, Any],
) -> Dict[str, Any]:
    previous_requirement = normalize_requirement_obj(
        state.get("stable_requirement") or DEFAULT_REQUIREMENT
    )
    previous_resolved_target = state.get("resolved_parser_target") or {}
    previous_instrument_family = str(
        previous_resolved_target.get("instrument_family")
        or state.get("instrument_family")
        or "unknown"
    ).strip()
    previous_experiment_kind = str(
        previous_resolved_target.get("experiment_kind")
        or previous_resolved_target.get("experiment_kind_hint")
        or "unknown"
    ).strip()
    previous_context_summary = str(state.get("context_summary") or "").strip()
    previous_experiment_background = state.get("experiment_background") or {}
    if not isinstance(previous_experiment_background, dict):
        previous_experiment_background = {}

    raw_resolved = tool_result.get("resolved_requirement")
    if isinstance(raw_resolved, dict) and raw_resolved:
        merged_requirement = merge_requirement_update(
            previous_requirement=previous_requirement,
            requirement_patch=raw_resolved,
        )
    else:
        merged_requirement = merge_requirement_update(
            previous_requirement=previous_requirement,
            requirement_patch=tool_result.get("requirement_patch") or {},
        )

    raw_instrument_family = str(tool_result.get("instrument_family") or "").strip()
    if raw_instrument_family and raw_instrument_family != "unknown":
        instrument_family = raw_instrument_family
    else:
        instrument_family = previous_instrument_family or "unknown"

    raw_experiment_kind_hint = str(
        tool_result.get("experiment_kind_hint") or ""
    ).strip()
    if raw_experiment_kind_hint and raw_experiment_kind_hint != "unknown":
        experiment_kind_hint = raw_experiment_kind_hint
    else:
        experiment_kind_hint = previous_experiment_kind or "unknown"

    context_summary = (
        str(tool_result.get("context_summary") or "").strip()
        or previous_context_summary
    )
    experiment_background = tool_result.get("experiment_background") or {}
    if not isinstance(experiment_background, dict):
        experiment_background = {}
    experiment_background = {
        **previous_experiment_background,
        **experiment_background,
    }

    issues = tool_result.get("issues") or []
    if not isinstance(issues, list):
        issues = []

    need_clarification = bool(tool_result.get("need_clarification", False))
    fatal = bool(tool_result.get("fatal", False))
    pending_question = (
        build_clarification_question(tool_result, state) if need_clarification else None
    )

    history = list(state.get("requirement_history") or [])
    history.append(
        {
            "ts": _utc_now_iso(),
            "user_request": state.get("current_user_request") or "",
            "resolved_requirement": merged_requirement,
            "instrument_family": instrument_family,
            "experiment_kind_hint": experiment_kind_hint,
            "need_clarification": need_clarification,
            "fatal": fatal,
        }
    )

    error_code = None
    error_detail = None
    if fatal:
        error_code = "REQUIREMENT_UNSATISFIABLE"
        error_detail = issues_to_text(issues) or "The current request is unsatisfiable."

    return {
        "stable_requirement": merged_requirement,
        "context_summary": context_summary,
        "experiment_background": experiment_background,
        "instrument_family": instrument_family,
        "resolved_parser_target": {
            "instrument_family": instrument_family,
            "experiment_kind": experiment_kind_hint or "unknown",
            "confidence": 0.75 if instrument_family != "unknown" else 0.35,
            "need_clarification": need_clarification,
            "source": "requirement_llm",
            "evidence": {},
        },
        "requirement_history": history,
        "requirement_issues": issues,
        "need_clarification": need_clarification,
        "pending_question": pending_question,
        "next_action": "ask_user" if need_clarification else None,
        "status": "waiting_user" if need_clarification else "active",
        "error_code": error_code,
        "error_detail": error_detail,
    }


def build_requirement_failure_state(
    state: AnalysisState,
    last_tool_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    last_tool_result = last_tool_result or {}
    issues = last_tool_result.get("issues") or []
    text = issues_to_text(issues)

    return {
        "error_code": "REQUIREMENT_ANALYSIS_FAILED",
        "error_detail": text
        or "Requirement understanding did not produce a usable result.",
        "status": "failed",
    }
