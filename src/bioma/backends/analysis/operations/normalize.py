from __future__ import annotations

from typing import Any, Dict

from bioma.backends.analysis.artifacts import task_artifact_dir
from bioma.backends.analysis.workspace import AnalysisState
from bioma.backends.analysis.services.data_prep_service import (
    run_data_prep,
    build_downstream_reset_patch,
)


def _resolve_output_dir(state: AnalysisState) -> str:
    task_id = state.get("task_id") or "task"
    return str(
        task_artifact_dir(
            state.get("artifact_root") or "artifacts",
            str(task_id),
            "data_prep",
        )
    )


def normalize_node(state: AnalysisState) -> Dict[str, Any]:
    raw_data_sources = state.get("raw_data_sources") or []
    output_dir = _resolve_output_dir(state)

    prep_result = run_data_prep(
        raw_data_sources=raw_data_sources,
        output_dir=output_dir,
    )

    out: Dict[str, Any] = {
        "transformed_data_sources": prep_result.get("transformed_data_sources", []),
        "data_artifact": prep_result.get("data_artifact", {}),
        "data_prep_meta": prep_result.get("data_prep_meta", {}),
        "table_count": prep_result.get("table_count", 0),
        "has_new_files": False,
        "error_code": prep_result.get("error_code"),
        "error_detail": prep_result.get("error_detail"),
        "status": "active" if not prep_result.get("error_code") else "failed",
    }

    # 新文件进入后，清空所有下游派生产物
    reset_patch = build_downstream_reset_patch()

    return {
        **reset_patch,
        **out,
    }
