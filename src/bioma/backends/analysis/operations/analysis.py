from __future__ import annotations

import json
from typing import Any, Dict

from bioma.backends.analysis.workspace import AnalysisState
from bioma.backends.analysis.services.analysis_service import (
    execute_analysis_plan,
    plan_analysis_v3,
)


def plan_analysis_node(state: AnalysisState) -> Dict[str, Any]:
    dataset_bundle = state.get("dataset_bundle") or {}
    requirement = state.get("stable_requirement") or {}
    experiment_background = state.get("experiment_background") or {}

    if not dataset_bundle:
        return {
            "data_profile": {},
            "chart_plan": [],
            "analysis_plan": {},
            "planning_issues": [
                {
                    "code": "MISSING_DATASET_BUNDLE",
                    "severity": "high",
                    "message": "plan_analysis_node requires dataset_bundle from parse node.",
                }
            ],
            "need_clarification": False,
            "pending_question": None,
            "error_code": "MISSING_DATASET_BUNDLE",
            "error_detail": "No dataset_bundle found in state.",
            "status": "failed",
        }

    try:
        result = (
            plan_analysis_v3(
                dataset_bundle=dataset_bundle,
                requirement=requirement,
                experiment_background=experiment_background,
            )
            or {}
        )
    except Exception as e:
        return {
            "data_profile": {},
            "chart_plan": [],
            "analysis_plan": {},
            "planning_issues": [
                {
                    "code": "PLAN_ANALYSIS_EXCEPTION",
                    "severity": "high",
                    "message": repr(e),
                }
            ],
            "need_clarification": False,
            "pending_question": None,
            "error_code": "ANALYSIS_PLANNING_FAILED",
            "error_detail": repr(e),
            "status": "failed",
        }

    data_profile = result.get("data_profile") or {}
    chart_plan = result.get("chart_plan") or []
    analysis_plan = result.get("analysis_plan") or {}
    issues = result.get("issues") or []
    need_clarification = bool(result.get("need_clarification", False))
    pending_question = str(result.get("question_to_user") or "").strip()
    fatal = bool(result.get("fatal", False))

    if fatal:
        return {
            "data_profile": data_profile,
            "chart_plan": chart_plan,
            "analysis_plan": analysis_plan,
            "planning_issues": issues,
            "need_clarification": need_clarification,
            "pending_question": pending_question or None,
            "error_code": "ANALYSIS_PLANNING_FAILED",
            "error_detail": "; ".join(str(issue.get("message", "")) for issue in issues)
            or "Analysis planner exhausted its correction attempts.",
            "status": "failed",
        }

    return {
        "data_profile": data_profile,
        "chart_plan": chart_plan,
        "analysis_plan": analysis_plan,
        "planning_issues": issues,
        "need_clarification": need_clarification,
        "pending_question": pending_question or None,
        "error_code": None,
        "error_detail": None,
        "status": "waiting_user" if need_clarification else "active",
    }


def run_analysis_node(state: AnalysisState) -> Dict[str, Any]:
    dataset_bundle = state.get("dataset_bundle") or {}
    data_profile = state.get("data_profile") or {}
    analysis_plan = state.get("analysis_plan") or {}
    chart_plan = state.get("chart_plan") or []
    task_id = str(state.get("task_id") or "task")
    requirement_context_text = "\n\n".join(
        [
            str(state.get("current_user_request") or ""),
            json.dumps(state.get("stable_requirement") or {}, ensure_ascii=False),
            json.dumps(state.get("experiment_background") or {}, ensure_ascii=False),
        ]
    )

    if not dataset_bundle:
        return {
            "analysis_results": {},
            "chart_data_ready_results": {},
            "error_code": "MISSING_DATASET_BUNDLE",
            "error_detail": "run_analysis_node requires dataset_bundle.",
            "status": "failed",
        }

    if not data_profile:
        return {
            "analysis_results": {},
            "chart_data_ready_results": {},
            "error_code": "MISSING_DATA_PROFILE",
            "error_detail": "run_analysis_node requires data_profile.",
            "status": "failed",
        }

    if not analysis_plan:
        return {
            "analysis_results": {},
            "chart_data_ready_results": {},
            "error_code": "MISSING_ANALYSIS_PLAN",
            "error_detail": "run_analysis_node requires analysis_plan.",
            "status": "failed",
        }

    try:
        out = execute_analysis_plan(
            dataset_bundle=dataset_bundle,
            data_profile=data_profile,
            analysis_plan=analysis_plan,
            chart_plan=chart_plan,
            task_id=task_id,
            requirement_context_text=requirement_context_text,
            artifact_root=str(state.get("artifact_root") or "artifacts"),
        )

        issues = out.get("issues") or []
        fatal = bool(out.get("fatal", False))

        return {
            "analysis_results": out.get("analysis_results") or {},
            "chart_data_ready_results": out.get("chart_data_ready_results") or {},
            "analysis_log": out.get("analysis_log") or [],
            "analysis_code_paths": out.get("analysis_code_paths") or [],
            "error_code": "ANALYSIS_EXEC_FAILED" if fatal else None,
            "error_detail": "Analysis execution failed." if fatal else None,
            "status": "failed" if fatal else "active",
            "analysis_execution_issues": issues,
        }
    except Exception as e:
        return {
            "analysis_results": {},
            "chart_data_ready_results": {},
            "error_code": "ANALYSIS_EXEC_EXCEPTION",
            "error_detail": repr(e),
            "status": "failed",
        }
