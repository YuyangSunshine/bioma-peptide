from __future__ import annotations

import time
from typing import Any, Dict

from bioma.backends.analysis.workspace import AnalysisState
from bioma.backends.analysis.services.requirement_service import (
    build_requirement_failure_state,
    build_requirement_payload_from_state,
    build_requirement_state_update,
)
from bioma.backends.analysis.tools.requirement_tools import (
    extract_initial_requirement_tool,
    update_requirement_tool,
)


def _is_usable_requirement_update(patch: Dict[str, Any]) -> bool:
    if patch.get("need_clarification"):
        return True
    if patch.get("error_code"):
        return True

    req = patch.get("stable_requirement") or {}
    fam = (
        (patch.get("resolved_parser_target") or {}).get("instrument_family") or ""
    ).strip()

    if isinstance(req, dict) and req:
        return True
    if fam:
        return True
    return False


def update_requirement_node(state: AnalysisState) -> Dict[str, Any]:
    """
    Unified node for:
    - initial requirement extraction
    - incremental requirement update
    - requirement + family resolution
    """
    payload = build_requirement_payload_from_state(state)
    mode = str(payload.get("mode") or "initial").strip().lower()

    max_retry = 3
    backoff = [0.5, 1.0, 2.0]
    last_tool_result: Dict[str, Any] | None = None

    for i in range(max_retry):
        try:
            if mode == "update":
                tool_result = (
                    update_requirement_tool.invoke(
                        {
                            "current_user_request": payload.get("current_user_request")
                            or "",
                            "dialog_history": payload.get("dialog_history")
                            or "<empty>",
                            "previous_requirement": payload.get("previous_requirement")
                            or {},
                            "pending_question": payload.get("pending_question") or "",
                            "previous_resolved_parser_target": payload.get(
                                "previous_resolved_parser_target"
                            )
                            or {},
                            "previous_experiment_background": payload.get(
                                "previous_experiment_background"
                            )
                            or {},
                            "data_artifact": payload.get("data_artifact") or {},
                            "data_prep_meta": payload.get("data_prep_meta") or {},
                        }
                    )
                    or {}
                )
            else:
                tool_result = (
                    extract_initial_requirement_tool.invoke(
                        {
                            "current_user_request": payload.get("current_user_request")
                            or "",
                            "dialog_history": payload.get("dialog_history")
                            or "<empty>",
                            "data_artifact": payload.get("data_artifact") or {},
                            "data_prep_meta": payload.get("data_prep_meta") or {},
                        }
                    )
                    or {}
                )
            if not isinstance(tool_result, dict):
                tool_result = {
                    "issues": [
                        {
                            "code": "INVALID_TOOL_RESULT",
                            "severity": "high",
                            "message": str(tool_result),
                        }
                    ]
                }

            last_tool_result = tool_result
            patch = build_requirement_state_update(state, tool_result)

            if _is_usable_requirement_update(patch):
                return patch

        except Exception as e:
            last_tool_result = {
                "issues": [
                    {
                        "code": "REQUIREMENT_TOOL_EXCEPTION",
                        "severity": "high",
                        "message": repr(e),
                    }
                ]
            }

        if i < max_retry - 1:
            time.sleep(backoff[i])

    return build_requirement_failure_state(state, last_tool_result)
