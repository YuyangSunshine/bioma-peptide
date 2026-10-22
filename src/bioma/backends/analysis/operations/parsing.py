from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from bioma.backends.analysis.workspace import AnalysisState
from bioma.backends.analysis.artifacts import task_artifact_dir
from bioma.backends.analysis.services.dataset_bundle_service import build_dataset_bundle
from bioma.backends.analysis.services.plate_layout_resolver import (
    find_existing_layout_csv,
    load_existing_layout_csv,
    merge_layout_into_tables,
    requirement_text_from_state,
    resolve_plate_layout_from_text,
)


def _resolve_instrument_family(state: AnalysisState) -> str:
    rpt = state.get("resolved_parser_target") or {}
    fam = str(rpt.get("instrument_family") or "").strip()
    if fam:
        return fam
    return str(state.get("instrument_family") or "unknown").strip()


def _sources_have_well_column(paths: list[str]) -> bool:
    for path in paths or []:
        try:
            df = pd.read_csv(path, nrows=3)
        except Exception:
            continue
        if "well" in {str(c).strip().lower() for c in df.columns}:
            return True
    return False


def _layout_output_dir(state: AnalysisState) -> str:
    task_id = state.get("task_id") or "task"
    return str(
        task_artifact_dir(
            state.get("artifact_root") or "artifacts",
            str(task_id),
            "layout_enriched",
        )
    )


def parse_node(state: AnalysisState) -> Dict[str, Any]:
    """
    New-architecture parse node:
    build dataset_bundle from transformed subtables instead of producing one global normalized dataframe.
    """
    transformed_data_sources = state.get("transformed_data_sources") or []
    instrument_family = _resolve_instrument_family(state)

    if not transformed_data_sources:
        return {
            "dataset_bundle": {},
            "bundle_issues": [
                {
                    "code": "MISSING_TRANSFORMED_DATA_SOURCES",
                    "severity": "high",
                    "message": "parse node requires transformed_data_sources from normalize node.",
                }
            ],
            "need_clarification": False,
            "pending_question": None,
            "error_code": "MISSING_TRANSFORMED_DATA_SOURCES",
            "error_detail": "No transformed_data_sources found in state.",
            "status": "failed",
        }

    bundle_issues_prefix: list[dict[str, Any]] = []
    bundle_sources = list(transformed_data_sources)
    if _sources_have_well_column(bundle_sources):
        existing_layout = find_existing_layout_csv(bundle_sources)
        if existing_layout:
            layout_result = load_existing_layout_csv(existing_layout)
        else:
            layout_result = resolve_plate_layout_from_text(
                requirement_text_from_state(state)
            )

        bundle_issues_prefix.extend(layout_result.issues)
        if not layout_result.dataframe.empty:
            bundle_sources, layout_issues = merge_layout_into_tables(
                transformed_data_sources=bundle_sources,
                layout_df=layout_result.dataframe,
                output_dir=_layout_output_dir(state),
            )
            bundle_issues_prefix.extend(layout_issues)

    result = build_dataset_bundle(
        instrument_family=instrument_family,
        transformed_data_sources=bundle_sources,
        data_artifact=state.get("data_artifact") or {},
        data_prep_meta=state.get("data_prep_meta") or {},
    )

    dataset_bundle = result.get("dataset_bundle") or {}
    bundle_issues = bundle_issues_prefix + (result.get("bundle_issues") or [])
    need_clarification = bool(result.get("need_clarification", False))
    question_to_user = str(result.get("question_to_user") or "").strip()
    fatal = bool(result.get("fatal", False))

    if fatal or not dataset_bundle:
        return {
            "dataset_bundle": {},
            "bundle_issues": bundle_issues,
            "need_clarification": need_clarification,
            "pending_question": question_to_user or None,
            "error_code": "DATASET_BUNDLE_BUILD_FAILED",
            "error_detail": "Failed to build dataset_bundle from transformed_data_sources.",
            "status": "failed",
        }

    return {
        "dataset_bundle": dataset_bundle,
        "bundle_issues": bundle_issues,
        "need_clarification": need_clarification,
        "pending_question": question_to_user or None,
        "error_code": None,
        "error_detail": None,
        "status": "waiting_user" if need_clarification else "active",
    }
