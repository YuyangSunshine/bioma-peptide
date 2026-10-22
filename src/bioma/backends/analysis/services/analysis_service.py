from __future__ import annotations

import json
import ast
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bioma.backends.analysis.processes import run_python
from bioma.backends.analysis.artifacts import task_artifact_dir

try:
    from bioma.backends.analysis.services.lightcycler_melt_analysis import (
        run_lightcycler_melt_analysis,
    )
    from bioma.backends.analysis.services.prediction_review_service import run_prediction_review
except ImportError:  # pragma: no cover - package import path
    from bioma.backends.analysis.services.lightcycler_melt_analysis import (
        run_lightcycler_melt_analysis,
    )
    from bioma.backends.analysis.services.prediction_review_service import run_prediction_review


# =========================================================
# Generic helpers
# =========================================================


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


def _safe_preview_dict(d: Dict[str, Any], max_keys: int = 10) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    out: Dict[str, Any] = {}
    for i, (k, v) in enumerate(d.items()):
        if i >= max_keys:
            break
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(type(v).__name__)
    return out


def _table_map(bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in bundle.get("tables") or []:
        tid = str(t.get("table_id") or "")
        if tid:
            out[tid] = t
    return out


def _load_table_df(table: Dict[str, Any]) -> pd.DataFrame:
    path = str(table.get("path") or "")
    if not path:
        raise ValueError("Table path missing.")
    json_source = (table.get("source_ref") or {}).get("json_source")
    if json_source:
        from bioma.backends.analysis.services.json_data import read_json_view

        return read_json_view(json_source)
    return pd.read_csv(path, dtype=object, encoding="utf-8-sig")


def _matrix_to_long(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert matrix-like table into long view:
    first column -> matrix_row_label
    remaining columns -> matrix_col_label / value
    """
    if df.empty or df.shape[1] < 2:
        return df.copy()

    row_col = str(df.columns[0])
    long_df = df.melt(
        id_vars=[row_col],
        var_name="matrix_col_label",
        value_name="value",
    ).rename(columns={row_col: "matrix_row_label"})
    return long_df


def _materialize_table_view(table: Dict[str, Any]) -> pd.DataFrame:
    """
    Create a task-ready view from one bundle table.
    Strips all-NaN rows/cols and converts matrix layouts to long form.
    Columns are kept under their original names; downstream LLM-codegen
    analyses reference columns via the bundle's open-vocabulary hints.
    """
    df = _load_table_df(table).copy()
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")

    if str(table.get("layout_type") or "") == "matrix":
        df = _matrix_to_long(df)

    df["__source_table_id"] = str(table.get("table_id") or "")
    return df


# =========================================================
# Planning helpers
# =========================================================


_LLM_PLAN_MAX_RETRY = 3


def _valid_column_names(dataset_bundle: Dict[str, Any]) -> Dict[str, set]:
    """Return {table_id: set(col_names)} for validation."""
    out: Dict[str, set] = {}
    for t in dataset_bundle.get("tables") or []:
        tid = str(t.get("table_id") or "")
        if not tid:
            continue
        names = set()
        for c in t.get("columns") or []:
            nm = str(c.get("name") or "")
            if nm:
                names.add(nm)
        out[tid] = names
    return out


def _infer_result_scope(
    *, analysis_id: str = "", goal: str = "", outputs: Any = None
) -> str:
    text_parts = [analysis_id, goal]
    for item in _as_list(outputs):
        text_parts.append(str(item or ""))
    text = _norm_name(" ".join(text_parts))
    if any(
        token in text
        for token in ("prediction", "predicted", "observed", "residual", "anomaly")
    ):
        return "prediction_vs_observed"
    if any(
        token in text
        for token in (
            "tm",
            "melt",
            "melting",
            "qc",
            "group_stat",
            "delta_tm",
            "negative_derivative",
        )
    ):
        return "tm_qc_group_stats"
    if any(token in text for token in ("curve", "fluorescence", "normalized")):
        return "curve_normalization"
    if any(token in text for token in ("chart", "plot", "figure", "visual")):
        return "chart_ready_data"
    if any(
        token in text
        for token in ("electropherogram", "virtual_gel", "labchip", "fragment", "band")
    ):
        return "electrophoresis_peak_metrics"
    return "exploratory_analysis"


def _result_registry_entry(
    *,
    analysis_id: str,
    scope: str,
    trust_level: str,
    summary: Optional[Dict[str, Any]] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    output_files: Optional[List[str]] = None,
    notes: str = "",
) -> Dict[str, Any]:
    return {
        "analysis_id": analysis_id,
        "scope": scope,
        "trust_level": trust_level,
        "summary": summary or {},
        "artifacts": artifacts or {},
        "output_files": list(output_files or []),
        "notes": notes,
    }


def _derived_output_norm_variants(value: Any) -> set[str]:
    norm = _norm_name(value)
    if not norm:
        return set()

    variants = {norm}
    if norm.endswith("s"):
        variants.add(norm[:-1])
    return variants


def _table_has_any_column(
    valid_cols: Dict[str, set],
    table_id: Optional[str],
    candidates: Tuple[str, ...],
) -> bool:
    if not table_id or table_id not in valid_cols:
        return False
    raw_cols = valid_cols.get(table_id) or set()
    norm_cols = {_norm_name(col) for col in raw_cols}
    for candidate in candidates:
        if candidate in raw_cols or _norm_name(candidate) in norm_cols:
            return True
    return False


_FRET_MP_VALUE_NAMES = {
    "mp",
    "m_p",
    "millipolarization",
    "milli_polarization",
    "polarization",
    "polarization_mp",
    "mean_mp",
    "avg_mp",
    "average_mp",
    "group_mean_mp",
    "value",
    "fret_value",
}


_QPCR_RELATIVE_EXPRESSION_VALUE_NAMES = {
    "value",
    "fold_change",
    "foldchange",
    "relative_expression",
    "relative_expr",
    "expression",
    "2_ddct",
    "2_delta_delta_ct",
    "ddct_value",
}


def _chart_field_can_be_derived(
    *,
    chart_key: str,
    chart_spec: Dict[str, Any],
    table_id: Optional[str],
    field: str,
    valid_cols: Dict[str, set],
) -> bool:
    """Return True when a chart role can be built deterministically from raw roles."""
    if chart_key == "qpcr_relative_expression_bar":
        field_norm = _norm_name(field)
        if field_norm not in _QPCR_RELATIVE_EXPRESSION_VALUE_NAMES:
            return False
        required = (
            ("Gene", "gene", "target", "target_gene"),
            ("Group", "group", "condition", "treatment"),
            ("Sample", "sample", "sample_id"),
            ("Ct", "ct", "cq"),
        )
        return all(
            _table_has_any_column(valid_cols, table_id, candidates)
            for candidates in required
        )

    if chart_key != "plate_fret_time_curve":
        return False

    field_norm = _norm_name(field)
    if field_norm not in _FRET_MP_VALUE_NAMES:
        return False

    parallel_candidates = (
        str(chart_spec.get("parallel_signal") or ""),
        str(chart_spec.get("parallel") or ""),
        str(chart_spec.get("p_signal") or ""),
        "P",
        "parallel",
        "parallel_signal",
        "p_signal",
        "parallel_intensity",
    )
    perpendicular_candidates = (
        str(chart_spec.get("perpendicular_signal") or ""),
        str(chart_spec.get("perpendicular") or ""),
        str(chart_spec.get("s_signal") or ""),
        "S",
        "perpendicular",
        "perpendicular_signal",
        "s_signal",
        "vertical",
        "vertical_signal",
    )
    return _table_has_any_column(
        valid_cols, table_id, parallel_candidates
    ) and _table_has_any_column(
        valid_cols,
        table_id,
        perpendicular_candidates,
    )


def _resolve_primary_table_id(dataset_bundle: Dict[str, Any]) -> Optional[str]:
    tables = dataset_bundle.get("tables") or []
    if not tables:
        return None
    return str(tables[0].get("table_id") or "") or None


def _validate_llm_plan(
    llm_out: Dict[str, Any],
    dataset_bundle: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Sanity-check LLM outputs against real bundle columns/tables.

    Returns (cleaned_analyses, cleaned_charts, validation_issues).
    Items referencing unknown table_id or columns are dropped with an issue.
    """
    issues: List[Dict[str, Any]] = []
    valid_cols = _valid_column_names(dataset_bundle)
    derived_output_names: set[str] = set()
    derived_output_norms: set[str] = set()
    for a in llm_out.get("analyses") or []:
        if not isinstance(a, dict):
            continue
        for output_name in a.get("outputs") or []:
            text = str(output_name or "").strip()
            if text:
                derived_output_names.add(text)
                derived_output_norms.update(_derived_output_norm_variants(text))

    def _derived_col_ok(col: Optional[str]) -> bool:
        if not col:
            return True
        text = str(col)
        norm = _norm_name(text)
        return text in derived_output_names or norm in derived_output_norms

    def _col_ok(table_id: Optional[str], col: Optional[str]) -> bool:
        if not col:
            return True
        if not table_id or table_id not in valid_cols:
            return False
        return col in valid_cols[table_id]

    cleaned_analyses: List[Dict[str, Any]] = []
    for a in llm_out.get("analyses") or []:
        if not isinstance(a, dict):
            continue
        inputs = a.get("inputs") or {}
        tid = str(inputs.get("table_id") or "") or None
        if tid and tid not in valid_cols:
            issues.append(
                {
                    "code": "ANALYSIS_UNKNOWN_TABLE",
                    "severity": "medium",
                    "message": f"analysis {a.get('analysis_id')}: unknown table_id {tid}, dropping.",
                }
            )
            continue
        bad_col = None
        references_upstream = bool(str(inputs.get("from_analysis") or "").strip())
        for col_key in ("x", "y"):
            c = inputs.get(col_key)
            if references_upstream:
                continue
            if c and not _col_ok(tid, str(c)) and not _derived_col_ok(str(c)):
                bad_col = (col_key, str(c))
                break
        if bad_col is None:
            for gb in inputs.get("group_by") or []:
                if not _col_ok(tid, str(gb)):
                    bad_col = ("group_by", str(gb))
                    break
        if bad_col is not None:
            issues.append(
                {
                    "code": "ANALYSIS_UNKNOWN_COLUMN",
                    "severity": "medium",
                    "message": f"analysis {a.get('analysis_id')}: column {bad_col[1]} (role={bad_col[0]}) not in table {tid}, dropping.",
                }
            )
            continue
        cleaned_analyses.append(a)

    cleaned_charts: List[Dict[str, Any]] = []
    for c in llm_out.get("charts") or []:
        if not isinstance(c, dict):
            continue
        tid = str(c.get("source_table_id") or "") or None
        spec = c.get("chart_spec") or {}
        chart_key = str(c.get("chart_key") or spec.get("chart_key") or "")
        if tid and tid not in valid_cols:
            issues.append(
                {
                    "code": "CHART_UNKNOWN_TABLE",
                    "severity": "medium",
                    "message": f"chart {c.get('chart_id')}: unknown table_id {tid}, dropping.",
                }
            )
            continue
        bad_col = None
        for key in ("x", "y", "series_by"):
            v = spec.get(key)
            if (
                v
                and not _col_ok(tid, str(v))
                and not _derived_col_ok(str(v))
                and not _chart_field_can_be_derived(
                    chart_key=chart_key,
                    chart_spec=spec,
                    table_id=tid,
                    field=str(v),
                    valid_cols=valid_cols,
                )
            ):
                bad_col = (key, str(v))
                break
        if bad_col is not None:
            issues.append(
                {
                    "code": "CHART_UNKNOWN_COLUMN",
                    "severity": "medium",
                    "message": f"chart {c.get('chart_id')}: column {bad_col[1]} (role={bad_col[0]}) not in table {tid}, dropping.",
                }
            )
            continue
        cleaned_charts.append(c)

    return cleaned_analyses, cleaned_charts, issues


def _requires_chart(requirement: Dict[str, Any]) -> bool:
    """Only structured visualization requests create a chart obligation."""
    return bool(requirement.get("chart_preferences"))


def _dataset_has_lightcycler_melt_tables(dataset_bundle: Dict[str, Any]) -> bool:
    has_peaks = False
    has_curves = False
    for table in (dataset_bundle or {}).get("tables") or []:
        cols = {_norm_name(col.get("name")) for col in table.get("columns") or []}
        if {"well", "temperature", "negative_derivative"} <= cols:
            has_peaks = True
        if {"well", "temperature", "fluorescence"} <= cols:
            has_curves = True
    return has_peaks or has_curves


def _analyses_to_plan_steps(
    analyses: List[Dict[str, Any]],
    primary_table_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Convert open-vocabulary `analyses[]` into `analysis_plan.steps[]`.

    Prepends a compat `materialize_view` step so current run_analysis still
    materializes the primary view (needed by run_plotting indirectly).
    Open-vocabulary items use `method: ""` — current run_analysis skips them
    gracefully. Future run_analysis refactor will codegen from goal/method_hint.
    """
    steps: List[Dict[str, Any]] = []
    if primary_table_id:
        steps.append(
            {
                "step": "materialize_primary_view",
                "description": "Materialize the primary table into a working view.",
                "method": "materialize_view",
                "inputs": ["dataset_bundle"],
                "outputs": ["primary_view"],
                "params": {"table_id": primary_table_id},
            }
        )

    for i, a in enumerate(analyses):
        if not isinstance(a, dict):
            continue
        steps.append(
            {
                "step": str(a.get("analysis_id") or f"a{i + 1}"),
                "description": str(a.get("goal") or ""),
                "method": "",
                "method_hint": str(a.get("method_hint") or ""),
                "inputs": a.get("inputs") or {},
                "outputs": list(a.get("outputs") or []),
                "params": {},
            }
        )

    return steps


def _charts_to_chart_plan(
    charts: List[Dict[str, Any]],
    primary_table_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Convert open-vocabulary `charts[]` into run_plotting-compatible `chart_plan[]`.

    Shape preserved: {chart_id, chart_key, source_table_id, source_view_id,
    priority, title, chart_note, rationale, preferred_fields}.
    `chart_note` is packed with title + rationale + chart_spec JSON so the
    downstream codegen LLM (run_plotting) has all the guidance it needs
    without a closed chart_key vocabulary.
    """
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(charts):
        if not isinstance(c, dict):
            continue
        chart_id = str(c.get("chart_id") or f"chart_{i + 1:02d}")
        title = str(c.get("title") or chart_id)
        rationale = str(c.get("rationale") or "")
        spec = c.get("chart_spec") or {}
        kind = str(spec.get("kind") or "line")
        chart_key = str(c.get("chart_key") or spec.get("chart_key") or kind)

        note_parts: List[str] = []
        if title:
            note_parts.append(f"Title: {title}")
        if rationale:
            note_parts.append(f"Rationale: {rationale}")
        note_parts.append(f"chart_spec: {json.dumps(spec, ensure_ascii=False)}")
        chart_note = "\n".join(note_parts)

        preferred_fields: List[str] = []
        for k in ("x", "y", "series_by", "error", "significance", "label"):
            v = spec.get(k)
            if v and str(v) not in preferred_fields:
                preferred_fields.append(str(v))

        out.append(
            {
                "chart_id": chart_id,
                "chart_key": chart_key,
                "priority": i + 1,
                "source_table_id": str(
                    c.get("source_table_id") or primary_table_id or ""
                ),
                "source_view_id": None,
                "title": title,
                "chart_note": chart_note,
                "rationale": rationale,
                "chart_spec": spec,
                "preferred_fields": preferred_fields,
            }
        )
    return out


def _preferred_fields_from_chart_spec(spec: Dict[str, Any]) -> List[str]:
    preferred_fields: List[str] = []
    for k in (
        "x",
        "y",
        "series_by",
        "error",
        "significance",
        "label",
        "tm",
        "peak_label",
        "peak_count",
        "ct",
        "cp",
        "threshold",
        "fit_value",
        "lloq",
        "uloq",
        "r_squared",
        "km",
        "vmax",
        "value",
        "sample",
        "lane",
        "size",
        "size_bp",
        "size_nt",
        "intensity",
        "peak_size_bp",
        "peak_size_nt",
        "peak_height",
        "peak_area",
        "main_band_size",
        "smear_index",
        "ratio_28s_18s",
    ):
        v = spec.get(k)
        if v and str(v) not in preferred_fields:
            preferred_fields.append(str(v))
    return preferred_fields


def _refresh_chart_note(chart: Dict[str, Any]) -> None:
    spec = chart.get("chart_spec") or {}
    note_parts: List[str] = []
    title = str(chart.get("title") or "")
    rationale = str(chart.get("rationale") or "")
    if title:
        note_parts.append(f"Title: {title}")
    if rationale:
        note_parts.append(f"Rationale: {rationale}")
    note_parts.append(f"chart_spec: {json.dumps(spec, ensure_ascii=False)}")
    chart["chart_note"] = "\n".join(note_parts)


def _canonical_spec_for_derived_chart(
    chart_key: str,
    original_spec: Dict[str, Any],
    out_df: pd.DataFrame,
) -> Dict[str, Any]:
    columns = {str(col) for col in out_df.columns}
    canonical_by_key = {
        "qpcr_relative_expression_bar": {
            "kind": "bar",
            "x": "gene",
            "y": "value",
            "series_by": "group",
            "error": "error",
            "label": "label",
        },
        "qpcr_melt_curve_neg_derivative": {
            "kind": "line",
            "x": "temperature",
            "y": "value",
            "series_by": "series",
            "tm": "tm",
            "peak_label": "peak_label",
            "peak_count": "peak_count",
            "peak_type": "peak_type",
        },
        "plate_fret_time_curve": {
            "kind": "line",
            "x": "time",
            "y": "value",
            "series_by": "series",
            "mp": "mp",
            "ratio": "ratio",
        },
        "qpcr_amplification_curve": {
            "kind": "line",
            "x": "cycle",
            "y": "value",
            "series_by": "series",
            "ct": "ct",
            "cp": "cp",
            "threshold": "threshold",
        },
        "plate_standard_curve": {
            "kind": "scatter",
            "x": "concentration",
            "y": "response",
            "series_by": "series",
            "error": "error",
            "fit_value": "fit_value",
            "lloq": "lloq",
            "uloq": "uloq",
            "equation": "equation",
            "r_squared": "r_squared",
        },
        "plate_michaelis_menten_curve": {
            "kind": "scatter",
            "x": "substrate_concentration",
            "y": "velocity",
            "series_by": "series",
            "error": "error",
            "fit_value": "fit_value",
            "km": "km",
            "vmax": "vmax",
        },
        "plate_sample_concentration_bar": {
            "kind": "bar",
            "x": "sample",
            "y": "value",
            "series_by": "group",
            "error": "error",
            "label": "label",
        },
        "ce_dna_electropherogram": {
            "kind": "line",
            "x": "size_bp",
            "y": "intensity",
            "series_by": "series",
            "peak_size_bp": "peak_size_bp",
            "peak_height": "peak_height",
            "peak_area": "peak_area",
        },
        "ce_rna_electropherogram": {
            "kind": "line",
            "x": "size_nt",
            "y": "intensity",
            "series_by": "series",
            "peak_size_nt": "peak_size_nt",
            "peak_height": "peak_height",
            "peak_area": "peak_area",
            "ratio_28s_18s": "ratio_28s_18s",
        },
        "ce_virtual_gel": {
            "kind": "heatmap",
            "x": "lane",
            "y": "size",
            "value": "intensity",
            "sample": "sample",
            "main_band_size": "main_band_size",
            "smear_index": "smear_index",
        },
    }
    canonical = canonical_by_key.get(chart_key)
    if not canonical:
        return dict(original_spec or {})

    spec = {"kind": (original_spec or {}).get("kind") or canonical.get("kind")}
    for key, value in canonical.items():
        if key == "kind" or value in columns:
            spec[key] = value
    return spec


def _build_data_profile_shim(
    dataset_bundle: Dict[str, Any],
    primary_table_id: Optional[str],
    experiment_kind: str,
    requirement: Dict[str, Any],
    experiment_background: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Slim data_profile kept ONLY for run_plotting compatibility.

    run_plotting reads:
      - data_profile.run_context.primary_table_id  (default source table when chart item lacks source_table_id)
      - data_profile_json is passed into viz codegen as context — keep tables slim.
    """
    tables_slim: List[Dict[str, Any]] = []
    for t in dataset_bundle.get("tables") or []:
        tables_slim.append(
            {
                "table_id": t.get("table_id"),
                "path": t.get("path"),
                "layout_type": t.get("layout_type"),
                "shape": t.get("shape") or {},
                "columns": [
                    {
                        "name": c.get("name"),
                        "dtype_hint": c.get("dtype_hint"),
                        "axis_role_hints": c.get("axis_role_hints") or [],
                        "sample_values": (c.get("sample_values") or [])[:4],
                    }
                    for c in (t.get("columns") or [])
                ],
                "preview": t.get("preview") or {},
            }
        )

    return {
        "profile_version": "3.0",
        "run_context": {
            "instrument_family_hint": dataset_bundle.get("instrument_family_hint"),
            "primary_table_id": primary_table_id,
            "experiment_kind": experiment_kind,
        },
        "tables": tables_slim,
        "relations": list(dataset_bundle.get("relations") or []),
        "requirement_snapshot": requirement or {},
        "experiment_background": experiment_background or {},
    }


def plan_analysis_v3(
    dataset_bundle: Dict[str, Any],
    requirement: Dict[str, Any],
    experiment_background: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Orchestrate open-vocabulary analysis/chart planning.

    Returns:
      {
        "data_profile": {...shim for run_plotting...},
        "chart_plan":   [...run_plotting compatible items...],
        "analysis_plan": {"plan_version": "3.0", "experiment_kind", "steps", ...},
        "issues":         [...],
        "need_clarification": bool,
        "question_to_user": str,
        "fatal":          bool,
      }
    """
    from bioma.backends.analysis.tools.planning_tools import (
        plan_analysis_tool,
    )  # local import to avoid cycles

    experiment_background = experiment_background or {}
    issues: List[Dict[str, Any]] = []

    if not isinstance(dataset_bundle, dict) or not (dataset_bundle.get("tables") or []):
        return {
            "data_profile": {},
            "chart_plan": [],
            "analysis_plan": {
                "plan_version": "3.0",
                "steps": [],
                "experiment_kind": "unknown_or_generic",
                "issues": [],
            },
            "issues": [
                {
                    "code": "EMPTY_DATASET_BUNDLE",
                    "severity": "high",
                    "message": "dataset_bundle has no tables; cannot plan.",
                }
            ],
            "need_clarification": True,
            "question_to_user": "当前没有可用的数据表，无法进行分析规划。",
            "fatal": True,
        }

    primary_table_id = _resolve_primary_table_id(dataset_bundle)

    from bioma.runtime.deadline import check, ExecutionTimeout
    from bioma.runtime.telemetry import emit

    feedback = None
    llm_out = {}
    cleaned_analyses, cleaned_charts = [], []
    requires_chart = _requires_chart(requirement) and not _dataset_has_lightcycler_melt_tables(dataset_bundle)
    fatal = True
    for attempt in range(_LLM_PLAN_MAX_RETRY):
        check()
        try:
            payload = {"dataset_bundle": dataset_bundle, "requirement": requirement or {},
                       "experiment_background": experiment_background}
            if feedback:
                payload["feedback"] = feedback
            llm_out = plan_analysis_tool.invoke(payload)
            if not isinstance(llm_out, dict):
                raise ValueError("Planner response must be an object.")
            cleaned_analyses, cleaned_charts, validation_issues = _validate_llm_plan(llm_out, dataset_bundle)
            issues = list(llm_out.get("issues") or []) + validation_issues
            if not cleaned_analyses and not cleaned_charts:
                issues.append({"code": "PLANNER_PLAN_INVALID", "severity": "high",
                               "message": "Return at least one valid analysis or chart for the requested task."})
            if requires_chart and not cleaned_charts:
                issues.append({"code": "REQUIRED_CHART_MISSING", "severity": "high",
                               "message": "The requested chart is missing or invalid. Declare its computed columns in the producing analysis outputs."})
            fatal = bool(validation_issues) or not (cleaned_analyses or cleaned_charts) or (requires_chart and not cleaned_charts)
        except ExecutionTimeout:
            raise
        except Exception as exc:
            issues = [{"code": "PLANNER_EXCEPTION", "severity": "high", "message": str(exc)}]
            fatal = True
        if not fatal:
            emit("analysis.plan_validated", attempt=attempt + 1)
            break
        emit("analysis.plan_rejected", attempt=attempt + 1, issues=issues, proposal=llm_out)
        feedback = {"previous_plan": llm_out, "validation_issues": issues}

    experiment_kind = str(llm_out.get("experiment_kind") or "unknown_or_generic") if isinstance(llm_out, dict) else "unknown_or_generic"
    if fatal:
        return {"data_profile": {}, "chart_plan": [],
                "analysis_plan": {"plan_version": "3.0", "steps": [], "experiment_kind": experiment_kind},
                "issues": issues, "need_clarification": False, "question_to_user": "", "fatal": True}

    analysis_plan = {
        "plan_version": "3.0",
        "experiment_kind": experiment_kind,
        "steps": _analyses_to_plan_steps(cleaned_analyses, primary_table_id),
        "analyses": cleaned_analyses,
        "issues": [],
    }

    chart_plan = _charts_to_chart_plan(cleaned_charts, primary_table_id)

    data_profile = _build_data_profile_shim(
        dataset_bundle=dataset_bundle,
        primary_table_id=primary_table_id,
        experiment_kind=experiment_kind,
        requirement=requirement or {},
        experiment_background=experiment_background,
    )

    fatal = not cleaned_analyses and not cleaned_charts
    need_clarification = False

    return {
        "data_profile": data_profile,
        "chart_plan": chart_plan,
        "analysis_plan": analysis_plan,
        "issues": issues,
        "need_clarification": need_clarification,
        "question_to_user": "",
        "fatal": fatal,
    }


def _prepare_chart_ready_results(
    chart_plan: List[Dict[str, Any]],
    analysis_results: Dict[str, Any],
) -> Dict[str, Any]:
    chart_inputs = []
    views = analysis_results.get("derived_views") or {}
    primary_view_id = analysis_results.get("primary_view_id")
    analysis_outputs = analysis_results.get("analysis_outputs") or {}

    def _find_view_for_field(field: Any) -> Optional[str]:
        target = _norm_name(field)
        if not target:
            return None
        for view_id, view in views.items():
            if view_id == primary_view_id:
                continue
            for col in view.get("columns") or []:
                if _norm_name(col) == target:
                    return view_id
        for analysis_id, paths in analysis_outputs.items():
            for path in paths or []:
                if not isinstance(path, str) or not os.path.exists(path):
                    continue
                try:
                    cols = [str(c) for c in pd.read_csv(path, nrows=0).columns.tolist()]
                except Exception:
                    continue
                if any(_norm_name(col) == target for col in cols):
                    view_id = f"view_{analysis_id}__auto"
                    if view_id not in views:
                        _register_derived_view(
                            derived_views=views,
                            view_id=view_id,
                            csv_path=path,
                            analysis_id=str(analysis_id),
                            note=f"Auto-bound derived view for chart field {field}.",
                        )
                    return view_id
        for view_id, view in views.items():
            for col in view.get("columns") or []:
                if _norm_name(col) == target:
                    return view_id
        return None

    for item in chart_plan or []:
        source_view_id = item.get("source_view_id")
        chart_key = str(item.get("chart_key") or "")
        preferred_fields = list(item.get("preferred_fields") or [])
        if not source_view_id and chart_key == "qpcr_relative_expression_bar":
            for field in ("relative_expression", "fold_change", "value"):
                matched_view_id = _find_view_for_field(field)
                if matched_view_id:
                    source_view_id = matched_view_id
                    item["source_view_id"] = matched_view_id
                    break
        if not source_view_id:
            for field in preferred_fields:
                matched_view_id = _find_view_for_field(field)
                if matched_view_id:
                    source_view_id = matched_view_id
                    item["source_view_id"] = matched_view_id
                    break
        source_view_id = source_view_id or primary_view_id
        source_table_id = item.get("source_table_id")
        source_view = views.get(source_view_id) if source_view_id in views else {}
        chart_inputs.append(
            {
                "chart_id": item.get("chart_id"),
                "chart_key": item.get("chart_key"),
                "source_view_id": source_view_id,
                "source_view_csv_path": source_view.get("csv_path"),
                "source_table_id": source_table_id,
                "preferred_fields": item.get("preferred_fields") or [],
                "status": "ready" if source_view_id in views else "missing",
            }
        )
    return {
        "chart_inputs": chart_inputs,
        "deterministic_artifacts": analysis_results.get("deterministic_artifacts")
        or {},
        "result_registry": analysis_results.get("result_registry") or [],
    }


# =========================================================
# Analysis execution (LLM code-gen v3.0)
# =========================================================

_ANALYSIS_EXEC_MAX_RETRY = 3
_ANALYSIS_EXEC_BACKOFF = [0.5, 1.0, 2.0]
_ANALYSIS_EXEC_TIMEOUT = 180


def _utc_now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_dir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def _extract_stdout_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _exec_python_code(
    code_path: str, timeout: int = _ANALYSIS_EXEC_TIMEOUT, *, cwd: str | Path | None = None, log_dir: Path | None = None
) -> Dict[str, Any]:
    exit_code = None
    try:
        code_file = Path(code_path).resolve()
        policy_error = _validate_code_action(code_file.read_text(encoding="utf-8"))
        if policy_error:
            return {"ok": False, "stdout": "", "stderr": f"CodeActionPolicyRejected: {policy_error}"}
        proc = run_python(code_file, timeout=timeout, cwd=cwd)
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / 'stdout.txt').write_text(proc.stdout or '', encoding='utf-8')
            (log_dir / 'stderr.txt').write_text(proc.stderr or '', encoding='utf-8')
        exit_code = proc.returncode
        ok = exit_code == 0
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
    except Exception as e:
        from bioma.runtime.deadline import ExecutionTimeout
        from bioma.runtime.admission import ExecutionAuthorizationError
        import subprocess
        if isinstance(e, (ExecutionTimeout, ExecutionAuthorizationError, OSError)):
            raise
        if isinstance(e, subprocess.TimeoutExpired):
            raise ExecutionTimeout("Analysis process timed out; execution requires reconciliation") from e
        ok = False
        stdout = ""
        stderr = f"Exception when running code: {repr(e)}"

    return {
        "ok": ok,
        "exit_code": exit_code,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
    }


_DENIED_CODE_IMPORTS = {"subprocess", "socket", "requests", "urllib", "http", "ftplib", "paramiko", "winreg"}
_DENIED_CODE_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}


def _validate_code_action(source: str) -> str | None:
    """Reject generated analysis code with process, network, or dynamic-code powers."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"invalid Python syntax: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split('.')[0] for alias in node.names] if isinstance(node, ast.Import) else [str(node.module or '').split('.')[0]]
            denied = sorted(set(names) & _DENIED_CODE_IMPORTS)
            if denied:
                return f"denied imports: {', '.join(denied)}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _DENIED_CODE_CALLS:
                return f"denied dynamic call: {node.func.id}"
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen", "spawn", "remove", "unlink", "rmtree"}:
                return f"denied side-effect call: {node.func.attr}"
    return None


def _slim_profile_for_analysis(data_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Slim data_profile to what the codegen LLM actually needs."""
    tables_slim: List[Dict[str, Any]] = []
    for t in data_profile.get("tables") or []:
        cols_slim = []
        for c in t.get("columns") or []:
            cols_slim.append(
                {
                    "name": c.get("name"),
                    "dtype_hint": c.get("dtype_hint"),
                    "axis_role_hints": c.get("axis_role_hints") or [],
                    "sample_values": (c.get("sample_values") or [])[:4],
                }
            )
        tables_slim.append(
            {
                "table_id": t.get("table_id"),
                "layout_type": t.get("layout_type"),
                "shape": t.get("shape") or {},
                "columns": cols_slim,
            }
        )
    return {
        "profile_version": data_profile.get("profile_version"),
        "run_context": data_profile.get("run_context") or {},
        "tables": tables_slim,
    }


def _register_derived_view(
    derived_views: Dict[str, Dict[str, Any]],
    view_id: str,
    csv_path: str,
    analysis_id: Optional[str],
    note: str,
) -> None:
    """Load a CSV produced by an analysis step and register it as a derived view."""
    try:
        df = pd.read_csv(csv_path, dtype=object, encoding="utf-8-sig")
    except Exception:
        return
    derived_views[view_id] = {
        "view_id": view_id,
        "table_id": None,
        "analysis_id": analysis_id,
        "csv_path": csv_path,
        "shape": {"rows": int(df.shape[0]), "cols": int(df.shape[1])},
        "columns": [str(c) for c in df.columns.tolist()],
        "preview_rows": df.head(10).to_dict(orient="records"),
        "note": note,
    }


def _norm_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    out: List[str] = []
    prev_us = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_")


def _find_column(df: pd.DataFrame, candidates: List[Any]) -> Optional[str]:
    by_norm = {_norm_name(col): str(col) for col in df.columns}
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        if text in df.columns:
            return text
        found = by_norm.get(_norm_name(text))
        if found:
            return found
    return None


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _role_candidates(
    chart: Dict[str, Any],
    role: str,
    role_keys: Dict[str, Tuple[str, ...]],
    aliases: Dict[str, Tuple[str, ...]],
) -> List[Any]:
    spec = chart.get("chart_spec") or {}
    candidates: List[Any] = []
    for key in role_keys.get(role, ()):
        value = spec.get(key)
        if isinstance(value, dict):
            candidates.extend(value.values())
        else:
            candidates.append(value)
    candidates.extend(aliases.get(role, ()))
    return candidates


def _resolve_roles(
    df: pd.DataFrame,
    chart: Dict[str, Any],
    role_keys: Dict[str, Tuple[str, ...]],
    aliases: Dict[str, Tuple[str, ...]],
    roles: Tuple[str, ...],
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for role in roles:
        col = _find_column(df, _role_candidates(chart, role, role_keys, aliases))
        if col:
            out[role] = col
    return out


_MELT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "temperature": ("temperature", "temp", "temperature_c", "temperature_celsius", "x"),
    "fluorescence": ("fluorescence", "fluor", "rfu", "signal", "intensity"),
    "derivative": (
        "negative_derivative",
        "neg_derivative",
        "-df_dt",
        "minus_df_dt",
        "derivative",
        "df_dt",
    ),
    "value": (
        "negative_derivative",
        "neg_derivative",
        "-df_dt",
        "minus_df_dt",
        "derivative",
        "value",
        "y",
    ),
    "series": (
        "series",
        "trace",
        "sample",
        "sample_id",
        "well",
        "well_id",
        "genotype",
        "group",
    ),
}


_FRET_ALIASES: Dict[str, Tuple[str, ...]] = {
    "donor": ("donor", "donor_signal", "donor_intensity", "donor_fluorescence"),
    "acceptor": (
        "acceptor",
        "acceptor_signal",
        "acceptor_intensity",
        "acceptor_fluorescence",
    ),
    "ratio": ("fret_ratio", "ratio", "ratio_value", "normalized_fret"),
    "mp": ("mp", "mP", "polarization", "polarization_mp", "anisotropy"),
    "parallel": ("p", "parallel", "parallel_signal", "p_signal", "parallel_intensity"),
    "perpendicular": (
        "s",
        "perpendicular",
        "perpendicular_signal",
        "s_signal",
        "vertical",
        "vertical_signal",
    ),
    "value": (
        "fret_ratio",
        "ratio",
        "ratio_value",
        "normalized_fret",
        "mp",
        "mP",
        "fret",
        "signal",
        "value",
        "y",
    ),
    "time": ("time", "time_s", "time_sec", "time_min", "minute", "cycle", "x"),
    "series": (
        "series",
        "trace",
        "sample",
        "sample_id",
        "well",
        "well_id",
        "condition",
        "group",
    ),
}


_AMP_ALIASES: Dict[str, Tuple[str, ...]] = {
    "cycle": ("cycle", "cycles", "cycle_number", "pcr_cycle", "x"),
    "value": (
        "delta_rn",
        "drn",
        "rn",
        "fluorescence",
        "fluor",
        "rfu",
        "signal",
        "value",
        "y",
    ),
    "series": (
        "series",
        "trace",
        "sample",
        "sample_id",
        "well",
        "well_id",
        "target",
        "replicate",
        "group",
    ),
    "ct": ("ct", "cq", "threshold_cycle", "crossing_point"),
    "cp": ("cp", "crossing_point", "ct", "cq"),
    "threshold": ("threshold", "threshold_value", "threshold_y", "ct_threshold"),
}


_STANDARD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "concentration": (
        "concentration",
        "conc",
        "standard_concentration",
        "dose",
        "amount",
        "x",
    ),
    "response": (
        "response",
        "signal",
        "od450",
        "od",
        "absorbance",
        "fluorescence",
        "intensity",
        "value",
        "y",
    ),
    "sample_type": ("sample_type", "type", "kind", "standard_or_sample"),
    "fit_value": ("fit_value", "fitted", "predicted", "predicted_response", "y_fit"),
    "r_squared": ("r_squared", "r2", "r_sq", "rsquared"),
    "adjusted_r_squared": (
        "adjusted_r_squared",
        "adjusted_r2",
        "adj_r_squared",
        "adj_r2",
    ),
    "rmse": ("rmse", "root_mean_squared_error"),
    "fit_model": ("fit_model", "model", "regression_model", "curve_model"),
    "lloq": ("lloq", "lower_limit", "lower_limit_of_quantification"),
    "uloq": ("uloq", "upper_limit", "upper_limit_of_quantification"),
}


_MM_ALIASES: Dict[str, Tuple[str, ...]] = {
    "substrate": (
        "substrate_concentration",
        "substrate",
        "s",
        "s_concentration",
        "concentration",
        "conc",
        "x",
    ),
    "velocity": (
        "velocity",
        "initial_velocity",
        "v0",
        "v",
        "rate",
        "initial_rate",
        "reaction_rate",
        "value",
        "y",
    ),
    "fit_value": ("fit_value", "fitted", "predicted", "predicted_velocity", "y_fit"),
    "km": ("km", "k_m", "michaelis_constant"),
    "vmax": ("vmax", "v_max", "maximum_velocity"),
}


_SAMPLE_CONCENTRATION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "sample": ("sample", "sample_id", "well", "well_id", "name", "label", "x"),
    "group": ("group", "condition", "treatment", "sample_group", "series", "class"),
    "replicate": (
        "replicate",
        "replicate_id",
        "rep",
        "technical_replicate",
        "bio_replicate",
    ),
    "value": (
        "concentration",
        "sample_concentration",
        "calculated_concentration",
        "back_calculated_concentration",
        "amount",
        "ng_ml",
        "ug_ml",
        "response",
        "signal",
        "value",
        "mean_value",
        "y",
    ),
    "error": ("error", "err", "sd", "std", "stddev", "sem", "ci", "ci95"),
    "p_value": ("p_value", "pvalue", "p", "padj", "adjusted_p_value"),
    "significance": ("significance", "significant", "sig", "stars"),
}


_CE_DNA_ALIASES: Dict[str, Tuple[str, ...]] = {
    "size": ("size_bp", "fragment_size", "fragment_size_bp", "bp", "size", "x"),
    "intensity": ("intensity", "fluorescence", "rfu", "signal", "value", "height", "y"),
    "series": ("series", "sample", "sample_id", "lane", "well", "trace", "library"),
    "peak_size": ("peak_size_bp", "main_peak_bp", "peak_bp", "peak_size"),
    "peak_height": ("peak_height", "height", "max_intensity"),
    "peak_area": ("peak_area", "area"),
    "concentration": ("concentration", "ng_ul", "ng_per_ul", "ngul", "amount"),
    "gqs": ("gqs", "quality_score", "quality"),
}


_CE_RNA_ALIASES: Dict[str, Tuple[str, ...]] = {
    "size": ("size_nt", "fragment_size_nt", "nt", "size", "size_bp", "x"),
    "intensity": ("intensity", "fluorescence", "rfu", "signal", "value", "height", "y"),
    "series": ("series", "sample", "sample_id", "lane", "well", "trace"),
    "peak_size": ("peak_size_nt", "main_peak_nt", "peak_nt", "peak_size"),
    "peak_height": ("peak_height", "height", "max_intensity"),
    "peak_area": ("peak_area", "area"),
    "rqs": ("rqs", "rin", "rna_integrity", "quality_score", "quality"),
    "ratio": ("ratio_28s_18s", "28s_18s_ratio", "ratio"),
}


_VIRTUAL_GEL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "lane": ("lane", "lane_id", "sample", "sample_id", "well", "series", "x"),
    "sample": ("sample", "sample_id", "well", "lane"),
    "size": ("size", "size_bp", "size_nt", "fragment_size", "bp", "nt", "y"),
    "intensity": (
        "intensity",
        "fluorescence",
        "rfu",
        "signal",
        "value",
        "band_intensity",
    ),
    "marker": ("marker", "is_marker", "sample_type", "lane_type"),
}


def _linear_fit(x: pd.Series, y: pd.Series) -> Optional[Dict[str, float]]:
    frame = pd.DataFrame({"x": _num(x), "y": _num(y)}).dropna()
    if frame.shape[0] < 2:
        return None
    x_mean = float(frame["x"].mean())
    y_mean = float(frame["y"].mean())
    ss_xx = float(((frame["x"] - x_mean) ** 2).sum())
    if ss_xx == 0:
        return None
    slope = float(((frame["x"] - x_mean) * (frame["y"] - y_mean)).sum() / ss_xx)
    intercept = y_mean - slope * x_mean
    pred = slope * frame["x"] + intercept
    ss_res = float(((frame["y"] - pred) ** 2).sum())
    ss_tot = float(((frame["y"] - y_mean) ** 2).sum())
    r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return {"slope": slope, "intercept": intercept, "r_squared": r_squared}


_STANDARD_FIT_MODEL_ORDER = (
    "linear",
    "logarithmic",
    "exponential",
    "power",
    "polynomial2",
    "inverse",
)


def _standard_curve_requested_models(chart: Dict[str, Any]) -> Tuple[str, ...]:
    requested = _chart_literal(
        chart, ("fit_model", "model", "regression_model", "curve_model")
    )
    if not requested or _norm_name(requested) == "auto":
        return _STANDARD_FIT_MODEL_ORDER
    model = _norm_name(requested)
    aliases = {
        "log": "logarithmic",
        "ln": "logarithmic",
        "quadratic": "polynomial2",
        "poly2": "polynomial2",
        "polynomial": "polynomial2",
        "reciprocal": "inverse",
    }
    model = aliases.get(model, model)
    if model in _STANDARD_FIT_MODEL_ORDER:
        return (model,)
    return _STANDARD_FIT_MODEL_ORDER


def _fit_metrics(
    *,
    model: str,
    x: pd.Series,
    y: pd.Series,
    y_pred: pd.Series,
    n_parameters: int,
    params: Dict[str, float],
    equation: str,
) -> Optional[Dict[str, Any]]:
    frame = pd.DataFrame({"x": x, "y": y, "fit_value": y_pred}).dropna()
    if frame.shape[0] <= n_parameters:
        return None
    y_mean = float(frame["y"].mean())
    ss_res = float(((frame["y"] - frame["fit_value"]) ** 2).sum())
    ss_tot = float(((frame["y"] - y_mean) ** 2).sum())
    r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    n = int(frame.shape[0])
    if n - n_parameters - 1 <= 0:
        adjusted_r_squared = r_squared
    else:
        adjusted_r_squared = 1.0 - (1.0 - r_squared) * (n - 1) / (n - n_parameters - 1)
    rmse = math.sqrt(ss_res / n)
    return {
        "fit_model": model,
        "n_points": n,
        "n_parameters": n_parameters,
        "r_squared": float(r_squared),
        "adjusted_r_squared": float(adjusted_r_squared),
        "rmse": float(rmse),
        "equation": equation,
        "params": params,
        "fit_values": pd.Series(y_pred, index=y_pred.index),
    }


def _fit_linear_transformed(
    *,
    model: str,
    x: pd.Series,
    y: pd.Series,
    z: pd.Series,
    predict,
    equation_template: str,
) -> Optional[Dict[str, Any]]:
    frame = pd.DataFrame({"x": x, "y": y, "z": z}).dropna()
    if frame.shape[0] < 3:
        return None
    fit = _linear_fit(frame["z"], frame["y"])
    if not fit:
        return None
    slope = float(fit["slope"])
    intercept = float(fit["intercept"])
    y_pred = pd.Series(index=x.index, dtype=float)
    valid = z.notna()
    y_pred.loc[valid] = predict(z.loc[valid], slope, intercept)
    equation = equation_template.format(slope=slope, intercept=intercept)
    result = _fit_metrics(
        model=model,
        x=x,
        y=y,
        y_pred=y_pred,
        n_parameters=2,
        params={"slope": slope, "intercept": intercept},
        equation=equation,
    )
    if result:
        result["slope"] = slope
        result["intercept"] = intercept
    return result


def _fit_exponential(x: pd.Series, y: pd.Series) -> Optional[Dict[str, Any]]:
    positive_y = y.where(y > 0)
    frame = pd.DataFrame({"x": x, "y": y, "ln_y": np.log(positive_y)}).dropna()
    if frame.shape[0] < 3:
        return None
    fit = _linear_fit(frame["x"], frame["ln_y"])
    if not fit:
        return None
    a = float(math.exp(fit["intercept"]))
    b = float(fit["slope"])
    y_pred = pd.Series(index=x.index, dtype=float)
    valid = x.notna()
    y_pred.loc[valid] = a * np.exp(b * x.loc[valid])
    result = _fit_metrics(
        model="exponential",
        x=x,
        y=y,
        y_pred=y_pred,
        n_parameters=2,
        params={"a": a, "b": b},
        equation=f"y = {a:.6g} * exp({b:.6g}x)",
    )
    return result


def _fit_power(x: pd.Series, y: pd.Series) -> Optional[Dict[str, Any]]:
    positive_x = x.where(x > 0)
    positive_y = y.where(y > 0)
    frame = pd.DataFrame(
        {"x": x, "y": y, "ln_x": np.log(positive_x), "ln_y": np.log(positive_y)}
    ).dropna()
    if frame.shape[0] < 3:
        return None
    fit = _linear_fit(frame["ln_x"], frame["ln_y"])
    if not fit:
        return None
    a = float(math.exp(fit["intercept"]))
    b = float(fit["slope"])
    y_pred = pd.Series(index=x.index, dtype=float)
    valid = x.gt(0) & x.notna()
    y_pred.loc[valid] = a * (x.loc[valid] ** b)
    result = _fit_metrics(
        model="power",
        x=x,
        y=y,
        y_pred=y_pred,
        n_parameters=2,
        params={"a": a, "b": b},
        equation=f"y = {a:.6g} * x^{b:.6g}",
    )
    return result


def _fit_polynomial2(x: pd.Series, y: pd.Series) -> Optional[Dict[str, Any]]:
    frame = pd.DataFrame({"x": x, "y": y}).dropna()
    if frame.shape[0] < 4:
        return None
    if float(frame["x"].nunique()) < 3:
        return None
    coeffs = np.polyfit(frame["x"].astype(float), frame["y"].astype(float), deg=2)
    a, b, c = (float(coeffs[0]), float(coeffs[1]), float(coeffs[2]))
    y_pred = pd.Series(index=x.index, dtype=float)
    valid = x.notna()
    y_pred.loc[valid] = a * (x.loc[valid] ** 2) + b * x.loc[valid] + c
    return _fit_metrics(
        model="polynomial2",
        x=x,
        y=y,
        y_pred=y_pred,
        n_parameters=3,
        params={"a": a, "b": b, "c": c},
        equation=f"y = {a:.6g}x^2 + {b:.6g}x + {c:.6g}",
    )


def _try_standard_curve_fits(
    x: pd.Series,
    y: pd.Series,
    models: Tuple[str, ...],
) -> List[Dict[str, Any]]:
    x_num = _num(x)
    y_num = _num(y)
    results: List[Dict[str, Any]] = []
    for model in models:
        fit: Optional[Dict[str, Any]] = None
        if model == "linear":
            fit = _fit_linear_transformed(
                model="linear",
                x=x_num,
                y=y_num,
                z=x_num,
                predict=lambda z, slope, intercept: slope * z + intercept,
                equation_template="y = {slope:.6g}x + {intercept:.6g}",
            )
        elif model == "logarithmic":
            z = pd.Series(np.log(x_num.where(x_num > 0)), index=x_num.index)
            fit = _fit_linear_transformed(
                model="logarithmic",
                x=x_num,
                y=y_num,
                z=z,
                predict=lambda z, slope, intercept: slope * z + intercept,
                equation_template="y = {slope:.6g}ln(x) + {intercept:.6g}",
            )
        elif model == "exponential":
            fit = _fit_exponential(x_num, y_num)
        elif model == "power":
            fit = _fit_power(x_num, y_num)
        elif model == "polynomial2":
            fit = _fit_polynomial2(x_num, y_num)
        elif model == "inverse":
            z = 1.0 / x_num.where(x_num != 0)
            fit = _fit_linear_transformed(
                model="inverse",
                x=x_num,
                y=y_num,
                z=z,
                predict=lambda z, slope, intercept: slope * z + intercept,
                equation_template="y = {slope:.6g}/x + {intercept:.6g}",
            )
        if fit:
            results.append(fit)
    return results


def _select_standard_curve_fit(fits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not fits:
        return None
    order = {model: idx for idx, model in enumerate(_STANDARD_FIT_MODEL_ORDER)}
    best = max(
        fits, key=lambda fit: float(fit.get("adjusted_r_squared", float("-inf")))
    )
    best_adjusted_r2 = float(best.get("adjusted_r_squared", float("-inf")))
    best_parameters = int(best.get("n_parameters", 99))
    simpler_near_best = [
        fit
        for fit in fits
        if best_adjusted_r2 - float(fit.get("adjusted_r_squared", float("-inf")))
        <= 0.02
        and int(fit.get("n_parameters", 99)) < best_parameters
    ]

    def sort_key(fit: Dict[str, Any]) -> Tuple[float, int, int, float]:
        return (
            float(fit.get("adjusted_r_squared", float("-inf"))),
            -int(fit.get("n_parameters", 99)),
            -order.get(str(fit.get("fit_model")), 99),
            -float(fit.get("rmse", float("inf"))),
        )

    if simpler_near_best:
        return max(simpler_near_best, key=sort_key)
    return best


def _predict_standard_curve_fit(fit: Dict[str, Any], x: pd.Series) -> pd.Series:
    x_num = _num(x)
    model = str(fit.get("fit_model") or "")
    params = fit.get("params") or {}
    pred = pd.Series(index=x_num.index, dtype=float)
    if model == "linear":
        pred.loc[x_num.notna()] = (
            params["slope"] * x_num.loc[x_num.notna()] + params["intercept"]
        )
    elif model == "logarithmic":
        valid = x_num.gt(0) & x_num.notna()
        pred.loc[valid] = (
            params["slope"] * np.log(x_num.loc[valid]) + params["intercept"]
        )
    elif model == "exponential":
        valid = x_num.notna()
        pred.loc[valid] = params["a"] * np.exp(params["b"] * x_num.loc[valid])
    elif model == "power":
        valid = x_num.gt(0) & x_num.notna()
        pred.loc[valid] = params["a"] * (x_num.loc[valid] ** params["b"])
    elif model == "polynomial2":
        valid = x_num.notna()
        pred.loc[valid] = (
            params["a"] * (x_num.loc[valid] ** 2)
            + params["b"] * x_num.loc[valid]
            + params["c"]
        )
    elif model == "inverse":
        valid = x_num.ne(0) & x_num.notna()
        pred.loc[valid] = params["slope"] / x_num.loc[valid] + params["intercept"]
    return pred


def _is_log_molecular_weight_column(column_name: str) -> bool:
    text = str(column_name or "").strip().lower()
    normalized = _norm_name(text)
    return (
        "log" in normalized
        and (
            "mw" in normalized or "mr" in normalized or "molecular_weight" in normalized
        )
    ) or ("分子量" in text and "对数" in text)


_RELATIVE_EXPRESSION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "gene": ("gene", "genes", "target", "target_gene", "assay", "基因", "目标基因"),
    "group": (
        "group",
        "condition",
        "treatment",
        "sample_group",
        "组别",
        "分组",
        "处理",
    ),
    "sample": (
        "sample",
        "sample_id",
        "well",
        "well_id",
        "replicate",
        "生物学重复",
        "样本",
    ),
    "ct": ("ct", "cq", "cp", "threshold_cycle", "crossing_point"),
    "value": (
        "relative_expression",
        "relative_expr",
        "expression",
        "fold_change",
        "foldchange",
        "2_ddct",
        "2_delta_delta_ct",
        "value",
        "相对表达量",
    ),
}


def _chart_literal(chart: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    spec = chart.get("chart_spec") or {}
    for source in (spec, chart):
        for key in keys:
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _derive_relative_expression_bar(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "gene": ("x", "gene", "target"),
            "group": ("series_by", "group", "condition"),
            "sample": ("sample", "sample_id", "well"),
            "ct": ("ct", "cq", "cp"),
            "value": ("y", "value", "relative_expression"),
        },
        _RELATIVE_EXPRESSION_ALIASES,
        ("gene", "group", "sample", "ct", "value"),
    )

    ct_alias_names = {_norm_name(name) for name in _RELATIVE_EXPRESSION_ALIASES["ct"]}
    value_is_ct = "value" in roles and _norm_name(roles["value"]) in ct_alias_names
    if all(role in roles for role in ("gene", "group", "value")) and not value_is_ct:
        values = _num(out[roles["value"]])
        if values.notna().sum() == 0:
            return out, {
                "status": "skipped",
                "reason": f"provided relative expression column {roles['value']!r} has no numeric values",
            }
        out["gene"] = out[roles["gene"]]
        out["target_gene"] = out["gene"]
        out["group"] = out[roles["group"]]
        out["value"] = values
        return out, {
            "status": "success",
            "value_source": "provided_relative_expression",
        }

    required = ("gene", "group", "sample", "ct")
    missing = [role for role in required if role not in roles]
    if missing:
        return out, {
            "status": "skipped",
            "reason": f"missing required Ct role(s): {', '.join(missing)}",
        }

    reference_gene = _chart_literal(
        chart,
        (
            "reference_gene",
            "reference",
            "ref_gene",
            "housekeeping_gene",
            "internal_control_gene",
        ),
    )
    control_group = _chart_literal(
        chart,
        ("control_group", "control", "baseline_group", "calibrator_group"),
    )
    if not reference_gene or not control_group:
        return out, {
            "status": "skipped",
            "reason": "reference_gene and control_group are required to derive 2^-ddCt from Ct data",
        }

    work = pd.DataFrame(
        {
            "sample": out[roles["sample"]],
            "group": out[roles["group"]],
            "gene": out[roles["gene"]],
            "ct": _num(out[roles["ct"]]),
        }
    ).dropna(subset=["ct"])
    work["gene_norm"] = work["gene"].map(_norm_name)
    work["group_norm"] = work["group"].map(_norm_name)
    reference_norm = _norm_name(reference_gene)
    control_norm = _norm_name(control_group)

    reference_rows = work[work["gene_norm"] == reference_norm][["sample", "ct"]].rename(
        columns={"ct": "reference_ct"}
    )
    if reference_rows.empty:
        return out, {
            "status": "skipped",
            "reason": f"reference_gene {reference_gene!r} was not found in gene column",
        }
    reference_rows = reference_rows.groupby("sample", dropna=False, as_index=False)[
        "reference_ct"
    ].mean()

    target_rows = work[work["gene_norm"] != reference_norm].copy()
    if target_rows.empty:
        return out, {
            "status": "skipped",
            "reason": "no target gene rows remain after excluding reference_gene",
        }

    merged = target_rows.merge(reference_rows, on="sample", how="left")
    merged = merged.dropna(subset=["reference_ct"])
    if merged.empty:
        return out, {
            "status": "skipped",
            "reason": "no samples have both target Ct and reference_gene Ct",
        }

    merged["delta_ct"] = merged["ct"] - merged["reference_ct"]
    control_delta = (
        merged[merged["group_norm"] == control_norm]
        .groupby("gene", dropna=False)["delta_ct"]
        .mean()
        .rename("control_mean_delta_ct")
        .reset_index()
    )
    if control_delta.empty:
        return out, {
            "status": "skipped",
            "reason": f"control_group {control_group!r} was not found for target genes",
        }

    merged = merged.merge(control_delta, on="gene", how="left").dropna(
        subset=["control_mean_delta_ct"]
    )
    if merged.empty:
        return out, {
            "status": "skipped",
            "reason": "target genes have no matching control_group delta Ct",
        }
    merged["delta_delta_ct"] = merged["delta_ct"] - merged["control_mean_delta_ct"]
    merged["relative_expression"] = 2 ** (-merged["delta_delta_ct"])

    summary_df = (
        merged.groupby(["gene", "group"], dropna=False)
        .agg(
            value=("relative_expression", "mean"),
            error=("relative_expression", "std"),
            n=("relative_expression", "count"),
            mean_delta_ct=("delta_ct", "mean"),
            mean_delta_delta_ct=("delta_delta_ct", "mean"),
        )
        .reset_index()
    )
    summary_df["error"] = summary_df["error"].fillna(0.0)
    summary_df["label"] = summary_df["value"].map(
        lambda v: f"{float(v):.2f}" if pd.notna(v) else ""
    )
    summary_df["target_gene"] = summary_df["gene"]

    return summary_df, {
        "status": "success",
        "value_source": "computed_2^-ddct",
        "reference_gene": reference_gene,
        "control_group": control_group,
        "target_genes": sorted(
            str(gene) for gene in summary_df["gene"].dropna().unique()
        ),
        "n_samples": int(merged["sample"].nunique()),
        "n_bars": int(summary_df.shape[0]),
    }


def _derive_melt_curve(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "temperature": ("x", "temperature"),
            "value": ("y", "value", "derivative"),
            "fluorescence": ("fluorescence", "y"),
            "derivative": ("derivative",),
            "series": ("series_by", "series", "sample", "well"),
        },
        _MELT_ALIASES,
        ("temperature", "value", "fluorescence", "derivative", "series"),
    )
    if "temperature" not in roles:
        return out, {"status": "skipped", "reason": "temperature column missing"}

    temp_col = roles["temperature"]
    series_col = roles.get("series")
    if series_col:
        out["series"] = out[series_col]
    if "value" in roles:
        out["value"] = _num(out[roles["value"]])
    if "fluorescence" in roles:
        out["fluorescence"] = _num(out[roles["fluorescence"]])

    if "derivative" in roles:
        out["derivative"] = _num(out[roles["derivative"]])
        out["value"] = out["derivative"]
    elif "fluorescence" in out.columns:
        out["temperature"] = _num(out[temp_col])
        group_key = series_col if series_col else (lambda x: 0)
        deriv = pd.Series(index=out.index, dtype="float64")
        for _, idx in out.groupby(group_key, dropna=False).groups.items():
            sub = out.loc[list(idx)].copy().sort_values("temperature")
            dtemp = sub["temperature"].diff()
            dfluor = sub["fluorescence"].diff()
            deriv.loc[sub.index] = -(dfluor / dtemp.replace(0, pd.NA))
        out["derivative"] = deriv
        out["value"] = out["derivative"]
    elif "value" not in out.columns:
        return out, {
            "status": "skipped",
            "reason": "value/fluorescence/derivative column missing",
        }

    out["temperature"] = _num(out[temp_col])
    if "series" not in out.columns:
        out["series"] = "series_1"

    out["peak_count"] = 0
    out["peak_type"] = "no_clear_peak"
    out["peak_label"] = ""

    def _local_peak_indices(sub: pd.DataFrame) -> List[Any]:
        clean = sub.dropna(subset=["temperature", "value"]).sort_values("temperature")
        if clean.shape[0] < 2:
            return []
        values = clean["value"].astype(float).tolist()
        max_value = max(values)
        if max_value <= 0:
            return []
        if clean.shape[0] == 2:
            return [clean["value"].astype(float).idxmax()]
        min_peak_height = max_value * 0.10
        peak_indices: List[Any] = []
        for pos in range(1, len(values) - 1):
            prev_value = values[pos - 1]
            cur_value = values[pos]
            next_value = values[pos + 1]
            is_local_peak = (
                cur_value >= min_peak_height
                and cur_value >= prev_value
                and cur_value > next_value
            )
            if is_local_peak:
                peak_indices.append(clean.index[pos])
        return peak_indices

    tm_by_series: Dict[Any, Any] = {}
    peak_count_by_series: Dict[Any, int] = {}
    peak_type_by_series: Dict[Any, str] = {}
    for series, sub in out.dropna(subset=["temperature", "value"]).groupby(
        "series", dropna=False
    ):
        if sub.empty:
            continue
        peak_indices = _local_peak_indices(sub)
        peak_count = len(peak_indices)
        peak_count_by_series[series] = peak_count
        if peak_count == 0:
            peak_type_by_series[series] = "no_clear_peak"
            continue
        peak_type_by_series[series] = "single_peak" if peak_count == 1 else "multi_peak"
        dominant_idx = out.loc[peak_indices, "value"].astype(float).idxmax()
        tm_by_series[series] = out.loc[dominant_idx, "temperature"]
        for peak_idx in peak_indices:
            tm_value = out.loc[peak_idx, "temperature"]
            if pd.notna(tm_value):
                out.loc[peak_idx, "peak_label"] = f"Tm={float(tm_value):.2f}"

    out["peak_count"] = out["series"].map(peak_count_by_series).fillna(0).astype(int)
    out["peak_type"] = out["series"].map(peak_type_by_series).fillna("no_clear_peak")
    if tm_by_series:
        out["tm"] = out["series"].map(tm_by_series)
    return out, {
        "status": "success",
        "tm_by_series": {
            str(k): float(v) for k, v in tm_by_series.items() if pd.notna(v)
        },
        "peak_count_by_series": {
            str(k): int(v) for k, v in peak_count_by_series.items()
        },
        "peak_type_by_series": {str(k): str(v) for k, v in peak_type_by_series.items()},
    }


def _derive_fret_curve(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "ratio": ("ratio", "y"),
            "mp": ("mp", "y"),
            "parallel": ("parallel_signal", "parallel", "p_signal", "p"),
            "perpendicular": ("perpendicular_signal", "perpendicular", "s_signal", "s"),
            "value": ("y", "value"),
            "donor": ("donor_signal", "donor"),
            "acceptor": ("acceptor_signal", "acceptor"),
            "time": ("x", "time"),
            "series": ("series_by", "series", "sample", "well"),
        },
        _FRET_ALIASES,
        (
            "ratio",
            "mp",
            "parallel",
            "perpendicular",
            "value",
            "donor",
            "acceptor",
            "time",
            "series",
        ),
    )
    if "time" in roles:
        out["time"] = _num(out[roles["time"]])
    if "series" in roles:
        out["series"] = out[roles["series"]]
    if "donor" in roles:
        out["donor_signal"] = _num(out[roles["donor"]])
    if "acceptor" in roles:
        out["acceptor_signal"] = _num(out[roles["acceptor"]])

    if "ratio" in roles:
        out["ratio"] = _num(out[roles["ratio"]])
        out["value"] = out["ratio"]
        return out, {"status": "success", "value_source": "provided_ratio"}
    if "mp" in roles:
        out["mp"] = _num(out[roles["mp"]])
        out["value"] = out["mp"]
        return out, {"status": "success", "value_source": "provided_mp"}
    if "parallel" in roles and "perpendicular" in roles:
        parallel = _num(out[roles["parallel"]])
        perpendicular = _num(out[roles["perpendicular"]])
        denominator = (parallel + perpendicular).replace(0, pd.NA)
        out["parallel_signal"] = parallel
        out["perpendicular_signal"] = perpendicular
        out["mp"] = (parallel - perpendicular) / denominator * 1000
        out["value"] = out["mp"]
        return out, {
            "status": "success",
            "value_source": "computed_mp_from_parallel_perpendicular",
        }
    elif "value" in roles:
        value_col = roles["value"]
        donor_acceptor_cols = {roles.get("donor"), roles.get("acceptor")}
        if value_col in donor_acceptor_cols:
            return out, {
                "status": "skipped",
                "reason": "donor/acceptor signals were provided without an explicit FRET ratio or mP formula",
            }
        out["value"] = _num(out[value_col])
        return out, {"status": "success", "value_source": "provided_value"}
    else:
        if "donor" in roles or "acceptor" in roles:
            return out, {
                "status": "skipped",
                "reason": "donor/acceptor signals were provided without an explicit FRET ratio or mP formula",
            }
        return out, {
            "status": "skipped",
            "reason": "FRET value missing: provide ratio/mP or parallel(P) and perpendicular(S) signals",
        }


def _derive_amplification_curve(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "cycle": ("x", "cycle"),
            "value": ("y", "value", "fluorescence"),
            "series": ("series_by", "series", "sample", "well"),
            "ct": ("ct",),
            "cp": ("cp",),
            "threshold": ("threshold",),
        },
        _AMP_ALIASES,
        ("cycle", "value", "series", "ct", "cp", "threshold"),
    )
    if "cycle" not in roles or "value" not in roles:
        return out, {"status": "skipped", "reason": "cycle or value column missing"}
    out["cycle"] = _num(out[roles["cycle"]])
    out["value"] = _num(out[roles["value"]])
    out["series"] = out[roles["series"]] if "series" in roles else "series_1"

    if "ct" in roles:
        out["ct"] = _num(out[roles["ct"]])
    if "cp" in roles:
        out["cp"] = _num(out[roles["cp"]])

    if "threshold" in roles:
        out["threshold"] = _num(out[roles["threshold"]])
        has_numeric_threshold = out["threshold"].notna().any()
    else:
        has_numeric_threshold = False

    if "ct" not in out.columns and "cp" not in out.columns and has_numeric_threshold:
        ct_by_series: Dict[Any, float] = {}
        for series, sub in out.dropna(subset=["cycle", "value", "threshold"]).groupby(
            "series", dropna=False
        ):
            sub = sub.sort_values("cycle")
            threshold = float(sub["threshold"].iloc[0])
            hit = sub[sub["value"] >= threshold]
            if hit.empty:
                continue
            hit_idx = hit.index[0]
            pos = list(sub.index).index(hit_idx)
            if pos == 0:
                ct = float(sub.loc[hit_idx, "cycle"])
            else:
                prev_idx = sub.index[pos - 1]
                x0, y0 = (
                    float(sub.loc[prev_idx, "cycle"]),
                    float(sub.loc[prev_idx, "value"]),
                )
                x1, y1 = (
                    float(sub.loc[hit_idx, "cycle"]),
                    float(sub.loc[hit_idx, "value"]),
                )
                ct = x1 if y1 == y0 else x0 + (threshold - y0) * (x1 - x0) / (y1 - y0)
            ct_by_series[series] = ct
        out["ct"] = out["series"].map(ct_by_series)
        out["cp"] = out["ct"]

    summary: Dict[str, Any] = {"status": "success"}
    if "ct" in out.columns:
        summary["ct_by_series"] = {
            str(k): float(v)
            for k, v in out.groupby("series")["ct"].first().dropna().items()
        }
        summary["ct_source"] = "provided" if "ct" in roles else "threshold_crossing"
    if "cp" in out.columns:
        summary["cp_by_series"] = {
            str(k): float(v)
            for k, v in out.groupby("series")["cp"].first().dropna().items()
        }
        summary["cp_source"] = "provided" if "cp" in roles else "threshold_crossing"
    if "threshold" in out.columns:
        summary["threshold_source"] = "provided"
    if (
        "threshold" not in out.columns
        and "ct" not in out.columns
        and "cp" not in out.columns
    ):
        summary["annotation_status"] = "curve_only_no_threshold_or_ct_cp"
    return out, summary


def _derive_standard_curve(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "concentration": ("x", "concentration"),
            "response": ("y", "response", "value"),
            "sample_type": ("sample_type", "type"),
            "fit_value": ("fit_value",),
            "r_squared": ("r_squared", "r2"),
            "adjusted_r_squared": ("adjusted_r_squared", "adjusted_r2"),
            "rmse": ("rmse",),
            "fit_model": ("fit_model",),
            "lloq": ("lloq",),
            "uloq": ("uloq",),
        },
        _STANDARD_ALIASES,
        (
            "concentration",
            "response",
            "sample_type",
            "fit_value",
            "r_squared",
            "adjusted_r_squared",
            "rmse",
            "fit_model",
            "lloq",
            "uloq",
        ),
    )
    if "concentration" not in roles or "response" not in roles:
        return out, {
            "status": "skipped",
            "reason": "concentration or response column missing",
        }
    out["concentration"] = _num(out[roles["concentration"]])
    out["response"] = _num(out[roles["response"]])

    fit_frame = out
    standard_point_source = "all_rows_no_sample_type"
    if "sample_type" in roles:
        out["sample_type"] = out[roles["sample_type"]]
        sample_type_norm = out["sample_type"].astype(str).str.strip().str.lower()
        standard_mask = sample_type_norm.isin(
            {
                "standard",
                "std",
                "calibrator",
                "calibration",
                "standard_point",
                "standard point",
                "标准品",
                "标准",
            }
        )
        fit_frame = out[standard_mask].copy()
        standard_point_source = "sample_type_standard_rows"
        if fit_frame.empty:
            return out, {
                "status": "skipped",
                "reason": "sample_type column exists but no standard/calibrator rows were found",
                "standard_point_source": standard_point_source,
            }

    candidate_models = _standard_curve_requested_models(chart)
    fits = _try_standard_curve_fits(
        fit_frame["concentration"], fit_frame["response"], candidate_models
    )
    fit = _select_standard_curve_fit(fits)
    if fit:
        model = str(fit["fit_model"])
        out["fit_value"] = _predict_standard_curve_fit(fit, out["concentration"])
        if _is_log_molecular_weight_column(roles["response"]):
            out["predicted_molecular_weight"] = 10 ** out["fit_value"]
        out["r_squared"] = fit["r_squared"]
        out["adjusted_r_squared"] = fit["adjusted_r_squared"]
        out["rmse"] = fit["rmse"]
        out["fit_model"] = model
        out["equation"] = fit["equation"]
        if "slope" in fit:
            out["slope"] = fit["slope"]
        if "intercept" in fit:
            out["intercept"] = fit["intercept"]
    if "lloq" in roles:
        out["lloq"] = _num(out[roles["lloq"]])
    if "uloq" in roles:
        out["uloq"] = _num(out[roles["uloq"]])

    summary: Dict[str, Any] = {
        "status": "success" if fit else "skipped",
        "fit": {
            key: value for key, value in (fit or {}).items() if key != "fit_values"
        },
        "candidate_fits": [
            {key: value for key, value in item.items() if key != "fit_values"}
            for item in fits
        ],
        "selected_fit_model": str(fit["fit_model"]) if fit else "",
        "standard_point_source": standard_point_source,
        "n_fit_points": int(fit_frame[["concentration", "response"]].dropna().shape[0]),
    }
    if "lloq" in out.columns:
        summary["lloq_source"] = "provided"
    if "uloq" in out.columns:
        summary["uloq_source"] = "provided"
    if not fit:
        summary["reason"] = "not enough numeric standard points for fit"
    return out, summary


def _derive_michaelis_menten_curve(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "substrate": ("x", "substrate_concentration", "concentration"),
            "velocity": ("y", "velocity", "rate", "value"),
            "km": ("km",),
            "vmax": ("vmax",),
            "fit_value": ("fit_value",),
        },
        _MM_ALIASES,
        ("substrate", "velocity", "km", "vmax", "fit_value"),
    )
    if "substrate" not in roles or "velocity" not in roles:
        return out, {
            "status": "skipped",
            "reason": "substrate or velocity column missing",
        }
    out["substrate_concentration"] = _num(out[roles["substrate"]])
    out["velocity"] = _num(out[roles["velocity"]])
    if "fit_value" in roles:
        out["fit_value"] = _num(out[roles["fit_value"]])

    if "km" in roles and "vmax" in roles:
        out["km"] = _num(out[roles["km"]])
        out["vmax"] = _num(out[roles["vmax"]])
        km = float(out["km"].dropna().iloc[0]) if out["km"].notna().any() else None
        vmax = (
            float(out["vmax"].dropna().iloc[0]) if out["vmax"].notna().any() else None
        )
        if km is None or vmax is None or km < 0 or vmax <= 0:
            return out, {
                "status": "skipped",
                "reason": "provided km/vmax values are invalid",
            }
        out["fit_value"] = (
            vmax
            * out["substrate_concentration"]
            / (km + out["substrate_concentration"])
        )
        return out, {
            "status": "success",
            "km": float(km),
            "vmax": float(vmax),
            "fit_method": "provided_km_vmax",
        }

    positive = out[(out["substrate_concentration"] > 0) & (out["velocity"] > 0)].copy()
    if positive.shape[0] < 3:
        return out, {"status": "skipped", "reason": "not enough positive points"}

    try:
        from scipy.optimize import curve_fit
    except Exception as exc:
        return out, {
            "status": "skipped",
            "reason": f"scipy curve_fit unavailable: {exc!r}",
        }

    def _michaelis_menten(substrate: Any, vmax_param: float, km_param: float) -> Any:
        return vmax_param * substrate / (km_param + substrate)

    x = positive["substrate_concentration"].astype(float).to_numpy()
    y = positive["velocity"].astype(float).to_numpy()
    vmax_initial = float(max(y))
    half_vmax = vmax_initial / 2.0
    km_initial = (
        float(x[(abs(y - half_vmax)).argmin()])
        if len(x)
        else float(pd.Series(x).median())
    )
    if km_initial <= 0:
        km_initial = float(pd.Series(x).median())
    if km_initial <= 0 or vmax_initial <= 0:
        return out, {
            "status": "skipped",
            "reason": "invalid initial parameters for nonlinear fit",
        }

    try:
        params, _cov = curve_fit(
            _michaelis_menten,
            x,
            y,
            p0=[vmax_initial, km_initial],
            bounds=([0.0, 0.0], [float("inf"), float("inf")]),
            maxfev=10000,
        )
    except Exception as exc:
        return out, {
            "status": "skipped",
            "reason": f"nonlinear michaelis-menten fit failed: {exc!r}",
        }

    vmax = float(params[0])
    km = float(params[1])
    if vmax <= 0 or km < 0:
        return out, {"status": "skipped", "reason": "invalid_km_vmax"}

    out["km"] = km
    out["vmax"] = vmax
    out["fit_value"] = (
        vmax * out["substrate_concentration"] / (km + out["substrate_concentration"])
    )
    pred = (
        vmax
        * positive["substrate_concentration"]
        / (km + positive["substrate_concentration"])
    )
    y_mean = float(positive["velocity"].mean())
    ss_res = float(((positive["velocity"] - pred) ** 2).sum())
    ss_tot = float(((positive["velocity"] - y_mean) ** 2).sum())
    r_squared = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return out, {
        "status": "success",
        "km": float(km),
        "vmax": float(vmax),
        "fit_method": "nonlinear_michaelis_menten",
        "r_squared": r_squared,
        "n_fit_points": int(positive.shape[0]),
    }


def _derive_sample_concentration_bar(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "sample": ("x", "sample"),
            "group": ("series_by", "group"),
            "replicate": ("replicate",),
            "value": ("y", "value", "concentration"),
            "error": ("error", "y_error"),
            "p_value": ("p_value",),
            "significance": ("significance",),
        },
        _SAMPLE_CONCENTRATION_ALIASES,
        ("sample", "group", "replicate", "value", "error", "p_value", "significance"),
    )
    if "sample" not in roles or "value" not in roles:
        return out, {"status": "skipped", "reason": "sample or value column missing"}

    work = pd.DataFrame(
        {
            "sample": out[roles["sample"]].astype(str),
            "value": _num(out[roles["value"]]),
        }
    )
    work["group"] = out[roles["group"]].astype(str) if "group" in roles else "Sample"
    if "replicate" in roles:
        work["replicate"] = out[roles["replicate"]].astype(str)
    if "p_value" in roles:
        work["p_value"] = _num(out[roles["p_value"]])
    if "significance" in roles:
        work["significance"] = out[roles["significance"]].astype(str)
    work = work.dropna(subset=["value"])
    if work.empty:
        return out, {
            "status": "skipped",
            "reason": "no numeric sample concentration values",
        }

    group_cols = ["sample", "group"]
    summary = (
        work.groupby(group_cols, dropna=False)["value"]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    summary = summary.rename(columns={"mean": "value", "std": "error", "count": "n"})
    summary["error"] = summary["error"].fillna(0.0)
    summary["cv"] = summary["error"] / summary["value"].replace(0, pd.NA)
    summary["label"] = summary["value"].map(
        lambda x: f"{float(x):.3g}" if pd.notna(x) else ""
    )
    if "p_value" in work.columns:
        pvals = work.groupby(group_cols, dropna=False)["p_value"].first().reset_index()
        summary = summary.merge(pvals, on=group_cols, how="left")
    if "significance" in work.columns:
        sigs = (
            work.groupby(group_cols, dropna=False)["significance"].first().reset_index()
        )
        summary = summary.merge(sigs, on=group_cols, how="left")

    return summary, {
        "status": "success",
        "value_source": "provided_or_back_calculated_concentration",
        "n_bars": int(summary.shape[0]),
        "n_input_rows": int(work.shape[0]),
        "groups": sorted(str(x) for x in summary["group"].dropna().unique()),
    }


def _local_peak_count(values: pd.Series, min_fraction: float = 0.1) -> int:
    nums = _num(values).reset_index(drop=True)
    if nums.dropna().empty or nums.shape[0] < 3:
        return int(nums.dropna().shape[0] > 0)
    max_value = float(nums.max())
    if max_value <= 0:
        return 0
    threshold = max_value * min_fraction
    count = 0
    for idx in range(1, len(nums) - 1):
        current = nums.iloc[idx]
        if pd.isna(current) or current < threshold:
            continue
        if current >= nums.iloc[idx - 1] and current >= nums.iloc[idx + 1]:
            count += 1
    return count or 1


def _peak_area(size: pd.Series, intensity: pd.Series) -> float:
    frame = (
        pd.DataFrame({"size": _num(size), "intensity": _num(intensity)})
        .dropna()
        .sort_values("size")
    )
    if frame.shape[0] < 2:
        return 0.0
    return float(
        np.trapezoid(frame["intensity"].astype(float), frame["size"].astype(float))
    )


def _derive_ce_electropherogram(
    df: pd.DataFrame,
    chart: Dict[str, Any],
    *,
    chart_key: str,
    aliases: Dict[str, Tuple[str, ...]],
    size_col_name: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "size": ("x", "size", size_col_name),
            "intensity": ("y", "intensity", "value"),
            "series": ("series_by", "series", "sample", "lane"),
            "peak_size": ("peak_size", f"peak_{size_col_name}"),
            "peak_height": ("peak_height",),
            "peak_area": ("peak_area",),
            "concentration": ("concentration",),
            "gqs": ("gqs",),
            "rqs": ("rqs", "rin"),
            "ratio": ("ratio_28s_18s",),
        },
        aliases,
        (
            "size",
            "intensity",
            "series",
            "peak_size",
            "peak_height",
            "peak_area",
            "concentration",
            "gqs",
            "rqs",
            "ratio",
        ),
    )
    if "size" not in roles or "intensity" not in roles or "series" not in roles:
        return out, {
            "status": "skipped",
            "reason": "size, intensity, or series column missing",
        }

    out[size_col_name] = _num(out[roles["size"]])
    out["intensity"] = _num(out[roles["intensity"]])
    out["series"] = out[roles["series"]].astype(str)
    if "concentration" in roles:
        out["concentration"] = _num(out[roles["concentration"]])
    if "gqs" in roles:
        out["gqs"] = _num(out[roles["gqs"]])
    if "rqs" in roles:
        out["rqs"] = _num(out[roles["rqs"]])

    peak_size_by_series: Dict[str, float] = {}
    peak_height_by_series: Dict[str, float] = {}
    peak_area_by_series: Dict[str, float] = {}
    peak_count_by_series: Dict[str, int] = {}
    ratio_by_series: Dict[str, float] = {}

    for series, sub in out.dropna(subset=[size_col_name, "intensity"]).groupby(
        "series", dropna=True
    ):
        sub = sub.sort_values(size_col_name)
        if sub.empty:
            continue
        peak_idx = sub["intensity"].idxmax()
        peak_size_by_series[str(series)] = float(out.loc[peak_idx, size_col_name])
        peak_height_by_series[str(series)] = float(out.loc[peak_idx, "intensity"])
        peak_area_by_series[str(series)] = _peak_area(
            sub[size_col_name], sub["intensity"]
        )
        peak_count_by_series[str(series)] = _local_peak_count(sub["intensity"])
        if chart_key == "ce_rna_electropherogram":
            peak_18s = sub[(sub[size_col_name] >= 1500) & (sub[size_col_name] <= 2500)]
            peak_28s = sub[(sub[size_col_name] >= 3500) & (sub[size_col_name] <= 6000)]
            if not peak_18s.empty and not peak_28s.empty:
                h18 = float(peak_18s["intensity"].max())
                h28 = float(peak_28s["intensity"].max())
                if h18 > 0:
                    ratio_by_series[str(series)] = h28 / h18

    out[f"peak_{size_col_name}"] = out["series"].map(peak_size_by_series)
    out["peak_height"] = out["series"].map(peak_height_by_series)
    out["peak_area"] = out["series"].map(peak_area_by_series)
    out["peak_count"] = out["series"].map(peak_count_by_series)
    out["peak_label"] = ""
    if ratio_by_series:
        out["ratio_28s_18s"] = out["series"].map(ratio_by_series)
    for series, peak_size in peak_size_by_series.items():
        mask = out["series"].eq(series) & out[size_col_name].eq(peak_size)
        out.loc[mask, "peak_label"] = f"Peak={peak_size:.0f}"

    return out, {
        "status": "success",
        "chart_key": chart_key,
        "n_series": len(peak_size_by_series),
        f"peak_{size_col_name}_by_series": peak_size_by_series,
        "peak_height_by_series": peak_height_by_series,
        "peak_area_by_series": peak_area_by_series,
        "peak_count_by_series": peak_count_by_series,
        "ratio_28s_18s_by_series": ratio_by_series,
    }


def _derive_ce_dna_electropherogram(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    return _derive_ce_electropherogram(
        df,
        chart,
        chart_key="ce_dna_electropherogram",
        aliases=_CE_DNA_ALIASES,
        size_col_name="size_bp",
    )


def _derive_ce_rna_electropherogram(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    return _derive_ce_electropherogram(
        df,
        chart,
        chart_key="ce_rna_electropherogram",
        aliases=_CE_RNA_ALIASES,
        size_col_name="size_nt",
    )


def _derive_ce_virtual_gel(
    df: pd.DataFrame, chart: Dict[str, Any]
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    out = df.copy()
    roles = _resolve_roles(
        out,
        chart,
        {
            "lane": ("x", "lane", "sample", "series_by"),
            "sample": ("sample",),
            "size": ("y", "size", "size_bp", "size_nt"),
            "intensity": ("value", "intensity", "band_intensity"),
            "marker": ("marker", "sample_type"),
        },
        _VIRTUAL_GEL_ALIASES,
        ("lane", "sample", "size", "intensity", "marker"),
    )
    if "lane" not in roles or "size" not in roles or "intensity" not in roles:
        return out, {
            "status": "skipped",
            "reason": "lane, size, or intensity column missing",
        }

    out["lane"] = out[roles["lane"]].astype(str)
    out["sample"] = (
        out[roles["sample"]].astype(str) if "sample" in roles else out["lane"]
    )
    out["size"] = _num(out[roles["size"]])
    out["intensity"] = _num(out[roles["intensity"]])
    if "marker" in roles:
        out["marker"] = out[roles["marker"]].astype(str)

    main_band_size: Dict[str, float] = {}
    main_band_intensity: Dict[str, float] = {}
    smear_index: Dict[str, float] = {}
    for lane, sub in out.dropna(subset=["size", "intensity"]).groupby(
        "lane", dropna=True
    ):
        if sub.empty:
            continue
        peak_idx = sub["intensity"].idxmax()
        main_band_size[str(lane)] = float(out.loc[peak_idx, "size"])
        main_band_intensity[str(lane)] = float(out.loc[peak_idx, "intensity"])
        total = float(sub["intensity"].clip(lower=0).sum())
        if total > 0:
            weighted_mean = float(
                (sub["size"] * sub["intensity"].clip(lower=0)).sum() / total
            )
            weighted_var = float(
                (
                    ((sub["size"] - weighted_mean) ** 2)
                    * sub["intensity"].clip(lower=0)
                ).sum()
                / total
            )
            size_range = float(sub["size"].max() - sub["size"].min()) or 1.0
            smear_index[str(lane)] = math.sqrt(max(weighted_var, 0.0)) / size_range

    out["main_band_size"] = out["lane"].map(main_band_size)
    out["main_band_intensity"] = out["lane"].map(main_band_intensity)
    out["smear_index"] = out["lane"].map(smear_index)
    return out, {
        "status": "success",
        "n_lanes": len(main_band_size),
        "main_band_size_by_lane": main_band_size,
        "main_band_intensity_by_lane": main_band_intensity,
        "smear_index_by_lane": smear_index,
    }


_DETERMINISTIC_CHART_DERIVERS = {
    "qpcr_relative_expression_bar": _derive_relative_expression_bar,
    "qpcr_melt_curve_neg_derivative": _derive_melt_curve,
    "plate_fret_time_curve": _derive_fret_curve,
    "qpcr_amplification_curve": _derive_amplification_curve,
    "plate_standard_curve": _derive_standard_curve,
    "plate_michaelis_menten_curve": _derive_michaelis_menten_curve,
    "plate_sample_concentration_bar": _derive_sample_concentration_bar,
    "ce_dna_electropherogram": _derive_ce_dna_electropherogram,
    "ce_rna_electropherogram": _derive_ce_rna_electropherogram,
    "ce_virtual_gel": _derive_ce_virtual_gel,
}


def _run_deterministic_chart_derivations(
    chart_plan: List[Dict[str, Any]],
    table_views: Dict[str, str],
    primary_csv_path: Optional[str],
    outputs_dir: str,
    derived_views: Dict[str, Dict[str, Any]],
    findings: List[Dict[str, Any]],
    issues: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    analysis_outputs: Dict[str, List[str]] = {}
    for idx, chart in enumerate(chart_plan or []):
        chart_key = str(chart.get("chart_key") or "")
        deriver = _DETERMINISTIC_CHART_DERIVERS.get(chart_key)
        if deriver is None:
            continue
        chart_id = str(chart.get("chart_id") or f"chart_{idx + 1:02d}")
        table_id = str(chart.get("source_table_id") or "")
        source_csv = table_views.get(table_id) or primary_csv_path
        if not source_csv or not os.path.exists(source_csv):
            issues.append(
                {
                    "code": "DETERMINISTIC_DERIVATION_SOURCE_MISSING",
                    "severity": "medium",
                    "message": f"{chart_id}: source CSV missing for deterministic {chart_key} derivation.",
                }
            )
            continue
        try:
            df = pd.read_csv(source_csv, dtype=object, encoding="utf-8-sig")
            out_df, summary = deriver(df, chart)
            if summary.get("status") != "success":
                issues.append(
                    {
                        "code": "DETERMINISTIC_DERIVATION_SKIPPED",
                        "severity": "low",
                        "message": f"{chart_id}: {summary.get('reason') or 'not enough fields'}",
                    }
                )
                continue
            analysis_id = f"det_{chart_id}"
            csv_path = os.path.join(outputs_dir, f"{analysis_id}__derived.csv")
            out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            view_id = f"view_{analysis_id}"
            _register_derived_view(
                derived_views=derived_views,
                view_id=view_id,
                csv_path=csv_path,
                analysis_id=analysis_id,
                note=f"Deterministic derived metrics for {chart_key}.",
            )
            chart["source_view_id"] = view_id
            chart["deterministic_derived_path"] = csv_path
            chart["chart_spec"] = _canonical_spec_for_derived_chart(
                chart_key,
                chart.get("chart_spec") or {},
                out_df,
            )
            chart["rationale"] = (
                f"Use the chart-ready derived view for {chart_key}. "
                "The CSV has standardized columns and should not be re-derived from raw input columns."
            )
            chart["preferred_fields"] = _preferred_fields_from_chart_spec(
                chart["chart_spec"]
            )
            _refresh_chart_note(chart)
            analysis_outputs[analysis_id] = [csv_path]
            findings.append(
                {
                    "step": analysis_id,
                    "analysis_id": analysis_id,
                    "scope": _infer_result_scope(
                        analysis_id=analysis_id,
                        goal=f"Compute deterministic derived metrics for {chart_key}.",
                        outputs=[csv_path],
                    ),
                    "trust_level": "derived",
                    "goal": f"Compute deterministic derived metrics for {chart_key}.",
                    "method_hint": "Deterministic service-side derivation.",
                    "summary": summary,
                    "output_files": [csv_path],
                    "notes": "",
                }
            )
        except Exception as exc:
            issues.append(
                {
                    "code": "DETERMINISTIC_DERIVATION_FAILED",
                    "severity": "medium",
                    "message": f"{chart_id}: {exc!r}",
                }
            )
    return analysis_outputs


def _execute_materialize_step(
    step: Dict[str, Any],
    tables: Dict[str, Dict[str, Any]],
    primary_table_id: str,
    primary_view_dir: str,
    derived_views: Dict[str, Dict[str, Any]],
    issues: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
    """
    Materialize every table in the bundle into its own CSV so downstream
    analysis steps can read any of them. Designates one as the "primary view"
    (by params.table_id or primary_table_id) for default reads.

    Returns (primary_view_id, primary_csv_path, table_views) where
    table_views maps table_id -> csv_path for ALL successfully materialized
    tables (including the primary one). On failure of the primary table,
    returns (None, None, table_views) with any other tables that did succeed.
    """
    params = step.get("params") or {}
    requested_primary = str(params.get("table_id") or primary_table_id or "")
    table_views: Dict[str, str] = {}

    for table_id, table in tables.items():
        if not table:
            continue
        try:
            df = _materialize_table_view(table)
        except Exception as e:
            issues.append(
                {
                    "code": "MATERIALIZE_VIEW_FAILED",
                    "severity": "high" if table_id == requested_primary else "medium",
                    "message": f"table {table_id}: {e!r}",
                }
            )
            continue

        view_id = f"view_{table_id}_primary"
        csv_path = os.path.join(primary_view_dir, f"{view_id}.csv")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        table_views[table_id] = csv_path

        derived_views[view_id] = {
            "view_id": view_id,
            "table_id": table_id,
            "analysis_id": None,
            "csv_path": csv_path,
            "shape": {"rows": int(df.shape[0]), "cols": int(df.shape[1])},
            "columns": [str(c) for c in df.columns.tolist()],
            "preview_rows": df.head(30).to_dict(orient="records"),
            "note": (
                "Primary table materialized for analysis."
                if table_id == requested_primary
                else "Secondary table materialized for cross-table access."
            ),
        }

    if requested_primary and requested_primary in table_views:
        primary_csv_path = table_views[requested_primary]
        primary_view_id = f"view_{requested_primary}_primary"
    elif table_views:
        first_tid = next(iter(table_views.keys()))
        primary_csv_path = table_views[first_tid]
        primary_view_id = f"view_{first_tid}_primary"
        if requested_primary:
            issues.append(
                {
                    "code": "MISSING_PRIMARY_TABLE",
                    "severity": "medium",
                    "message": (
                        f"Requested primary table_id={requested_primary!r} failed; "
                        f"falling back to {first_tid!r}."
                    ),
                }
            )
    else:
        issues.append(
            {
                "code": "MATERIALIZE_ALL_FAILED",
                "severity": "high",
                "message": "No tables could be materialized.",
            }
        )
        return None, None, table_views

    return primary_view_id, primary_csv_path, table_views


def _resolve_step_input_csv(
    step: Dict[str, Any],
    primary_csv_path: Optional[str],
    analysis_outputs: Dict[str, List[str]],
    table_views: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Pick the CSV an analysis step should read from, in priority order:
    1) inputs.from_analysis → upstream step's first output CSV
    2) inputs.table_id → that table's materialized view (multi-table support)
    3) fall back to the primary view CSV
    """
    inputs = step.get("inputs") or {}
    upstream_id = str(inputs.get("from_analysis") or "").strip()
    if (
        upstream_id
        and upstream_id in analysis_outputs
        and analysis_outputs[upstream_id]
    ):
        return analysis_outputs[upstream_id][0]

    table_views = table_views or {}
    table_id = str(inputs.get("table_id") or "").strip()
    if table_id and table_id in table_views:
        return table_views[table_id]

    return primary_csv_path


def _build_upstream_outputs_map(
    step: Dict[str, Any],
    analysis_outputs: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """
    Pass all upstream-step CSVs the current step might want to cross-reference.
    Keeps the payload small by only including known upstream ids present in outputs.
    """
    out: Dict[str, List[str]] = {}
    inputs = step.get("inputs") or {}
    upstream_id = str(inputs.get("from_analysis") or "").strip()
    if upstream_id and upstream_id in analysis_outputs:
        out[upstream_id] = list(analysis_outputs[upstream_id])
    for aid, paths in analysis_outputs.items():
        if aid not in out and paths:
            out[aid] = list(paths)
    return out


def _peek_csv_schema(path: str, max_cols: int = 40) -> Dict[str, Any]:
    """
    Read CSV header + row count cheaply so the LLM knows actual column names
    written by upstream steps (prevents downstream steps from hallucinating
    column names like '-dF/dT' when upstream actually wrote 'neg_dF_dT').
    """
    try:
        df = pd.read_csv(path, nrows=0)
        columns = [str(c) for c in df.columns.tolist()][:max_cols]
        n_rows = sum(1 for _ in open(path, "r", encoding="utf-8", errors="replace")) - 1
        if n_rows < 0:
            n_rows = 0
        return {"path": path, "columns": columns, "n_rows": n_rows}
    except Exception as e:
        return {"path": path, "columns": [], "n_rows": 0, "error": repr(e)[:200]}


def _build_upstream_schemas(
    upstream_map: Dict[str, List[str]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    For each upstream analysis id, return a small schema per output CSV.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for aid, paths in (upstream_map or {}).items():
        schemas: List[Dict[str, Any]] = []
        for p in paths or []:
            if isinstance(p, str) and os.path.exists(p):
                schemas.append(_peek_csv_schema(p))
        if schemas:
            out[aid] = schemas
    return out


def _build_table_schemas(
    table_views: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """
    For each materialized table view, return {table_id: {path, columns, n_rows}}
    so the codegen LLM knows the actual schema of every table available for
    cross-table joins/merges.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for tid, path in (table_views or {}).items():
        if isinstance(path, str) and os.path.exists(path):
            out[tid] = _peek_csv_schema(path)
    return out


def _run_codegen_step(
    step: Dict[str, Any],
    step_name: str,
    parsed_data_path: str,
    output_dir: str,
    code_dir: str,
    slim_profile: Dict[str, Any],
    analysis_outputs: Dict[str, List[str]],
    table_views: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Invoke the analysis codegen tool, save code to disk, execute it, and return
    a structured step result plus any output CSVs registered.
    """
    from bioma.backends.analysis.tools.analysis_tools import (
        generate_analysis_python_code_tool,
    )

    analysis_id = step_name
    goal = str(step.get("description") or "")
    method_hint = str(step.get("method_hint") or "")
    inputs = step.get("inputs") or {}
    upstream_map = _build_upstream_outputs_map(step, analysis_outputs)
    upstream_schemas = _build_upstream_schemas(upstream_map)
    table_views = table_views or {}
    table_schemas = _build_table_schemas(table_views)

    tool_resp: Dict[str, Any] = {}
    for attempt in range(_ANALYSIS_EXEC_MAX_RETRY):
        try:
            tool_resp = (
                generate_analysis_python_code_tool.invoke(
                    {
                        "analysis_id": analysis_id,
                        "goal": goal,
                        "method_hint": method_hint,
                        "inputs": inputs,
                        "parsed_data_path": parsed_data_path,
                        "output_dir": output_dir,
                        "data_profile": slim_profile,
                        "upstream_outputs": upstream_map,
                        "upstream_schemas": upstream_schemas,
                        "available_tables": table_views,
                        "available_table_schemas": table_schemas,
                    }
                )
                or {}
            )
            if isinstance(tool_resp, dict) and (tool_resp.get("scripts") or []):
                break
        except Exception as e:
            tool_resp = {"scripts": [], "error": f"codegen_exception: {e!r}"}
        if attempt < _ANALYSIS_EXEC_MAX_RETRY - 1:
            time.sleep(_ANALYSIS_EXEC_BACKOFF[attempt])

    scripts = list(tool_resp.get("scripts") or [])
    if not scripts:
        return {
            "status": "failed",
            "reason": str(tool_resp.get("error") or "analysis codegen failed"),
            "output_files": [],
            "code_paths": [],
        }

    last_stdout = ""
    last_stderr = ""
    produced_files_all: List[str] = []
    code_paths: List[str] = []
    all_ok = bool(scripts)
    parsed_summary: Optional[Dict[str, Any]] = None
    declared_any: List[str] = []

    for i, s in enumerate(scripts):
        py_code = str(s.get("python_code") or "")
        script_id = str(s.get("script_id") or f"{analysis_id}_{i + 1:02d}")
        code_path = os.path.join(code_dir, f"{script_id}.py")
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(py_code)
        code_paths.append(code_path)

        exec_out = _exec_python_code(code_path=code_path)
        last_stdout = exec_out["stdout"]
        last_stderr = exec_out["stderr"]
        all_ok = all_ok and bool(exec_out["ok"])
        parsed = _extract_stdout_json(last_stdout) if exec_out["ok"] else None
        if parsed and parsed_summary is None:
            parsed_summary = parsed

        declared = s.get("output_files") or []
        if isinstance(declared, str):
            declared = [declared]
        declared = [str(x) for x in declared if str(x).strip()]
        declared_any.extend(declared)

        if parsed and isinstance(parsed.get("output_files"), list):
            for of in parsed["output_files"]:
                if isinstance(of, str) and of.strip():
                    declared.append(of.strip())

        for of in declared:
            full = of if os.path.isabs(of) else os.path.join(output_dir, of)
            if not os.path.isfile(full) or not os.path.getsize(full):
                all_ok = False
                last_stderr = f'Missing or empty declared analysis output: {full}'
            elif full not in produced_files_all:
                produced_files_all.append(full)

    if not all_ok:
        return {
            "status": "failed",
            "reason": (last_stderr or "analysis script exited non-zero")[:500],
            "code_paths": code_paths,
            "output_files": produced_files_all,
            "stdout": last_stdout,
            "stderr": last_stderr,
        }

    return {
        "status": "success",
        "summary": (parsed_summary.get("summary") if parsed_summary else None)
        or parsed_summary
        or {},
        "notes": (parsed_summary or {}).get("notes") or "",
        "output_files": produced_files_all,
        "code_paths": code_paths,
        "stdout": last_stdout,
        "stderr": last_stderr,
    }


def execute_analysis_plan(
    dataset_bundle: Dict[str, Any],
    data_profile: Dict[str, Any],
    analysis_plan: Dict[str, Any],
    chart_plan: Optional[List[Dict[str, Any]]] = None,
    task_id: str = "task",
    requirement_context_text: str = "",
    artifact_root: str = "artifacts",
) -> Dict[str, Any]:
    """
    Execute an open-vocabulary analysis plan by generating Python code per step
    via the analysis_tools LLM and running it in a subprocess.

    Returns {analysis_results, chart_data_ready_results, issues, fatal,
             analysis_log, analysis_code_paths}.
    """
    issues: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    step_results: List[Dict[str, Any]] = []
    derived_views: Dict[str, Dict[str, Any]] = {}
    findings: List[Dict[str, Any]] = []
    result_registry: List[Dict[str, Any]] = []
    code_paths_all: List[str] = []

    tables = _table_map(dataset_bundle)
    if not tables:
        return {
            "analysis_results": {},
            "chart_data_ready_results": {},
            "issues": [
                {
                    "code": "EMPTY_BUNDLE_TABLES",
                    "severity": "high",
                    "message": "dataset_bundle has no tables for analysis execution.",
                }
            ],
            "fatal": True,
            "analysis_log": logs,
            "analysis_code_paths": [],
        }

    run_ctx = data_profile.get("run_context") or {}
    primary_table_id = str(run_ctx.get("primary_table_id") or "")
    slim_profile = _slim_profile_for_analysis(data_profile)

    work_dir = str(
        task_artifact_dir(artifact_root, task_id, "analysis", _utc_now_tag())
    )
    primary_view_dir = _ensure_dir(os.path.join(work_dir, "primary_view"))
    code_dir = _ensure_dir(os.path.join(work_dir, "code"))
    outputs_dir = _ensure_dir(os.path.join(work_dir, "outputs"))

    primary_view_id: Optional[str] = None
    primary_csv_path: Optional[str] = None
    analysis_outputs: Dict[str, List[str]] = {}
    table_views: Dict[str, str] = {}

    for step in analysis_plan.get("steps") or []:
        method = str(step.get("method") or "").strip()
        step_name = str(step.get("step") or method or "unknown_step")

        if method == "materialize_view":
            primary_view_id, primary_csv_path, table_views = _execute_materialize_step(
                step=step,
                tables=tables,
                primary_table_id=primary_table_id,
                primary_view_dir=primary_view_dir,
                derived_views=derived_views,
                issues=issues,
            )
            step_results.append(
                {
                    "step": step_name,
                    "method": method,
                    "status": "success" if primary_view_id else "failed",
                    "view_id": primary_view_id,
                    "csv_path": primary_csv_path,
                    "table_views": dict(table_views),
                }
            )
            logs.append(
                {
                    "phase": "materialize",
                    "step": step_name,
                    "ok": primary_view_id is not None,
                    "view_id": primary_view_id,
                    "n_table_views": len(table_views),
                }
            )
            continue

        if primary_csv_path is None:
            step_results.append(
                {
                    "step": step_name,
                    "method": method,
                    "status": "skipped",
                    "reason": "No primary view materialized.",
                }
            )
            continue

        parsed_data_path = _resolve_step_input_csv(
            step=step,
            primary_csv_path=primary_csv_path,
            analysis_outputs=analysis_outputs,
            table_views=table_views,
        )

        logs.append(
            {
                "phase": "analysis_codegen_call",
                "step": step_name,
                "parsed_data_path": parsed_data_path,
                "has_upstream": bool((step.get("inputs") or {}).get("from_analysis")),
                "table_id_requested": str(
                    (step.get("inputs") or {}).get("table_id") or ""
                ),
            }
        )

        exec_result = _run_codegen_step(
            step=step,
            step_name=step_name,
            parsed_data_path=parsed_data_path or primary_csv_path,
            output_dir=outputs_dir,
            code_dir=code_dir,
            slim_profile=slim_profile,
            analysis_outputs=analysis_outputs,
            table_views=table_views,
        )

        code_paths_all.extend(exec_result.get("code_paths") or [])
        logs.append(
            {
                "phase": "analysis_exec_end",
                "step": step_name,
                "ok": exec_result.get("status") == "success",
                "n_output_files": len(exec_result.get("output_files") or []),
                "stderr_preview": (exec_result.get("stderr") or "")[:300],
            }
        )

        status = exec_result.get("status", "failed")
        output_files = exec_result.get("output_files") or []

        if status == "success":
            analysis_outputs[step_name] = list(output_files)
            for idx, ofile in enumerate(output_files):
                view_id = f"view_{step_name}__{idx + 1:02d}"
                _register_derived_view(
                    derived_views=derived_views,
                    view_id=view_id,
                    csv_path=ofile,
                    analysis_id=step_name,
                    note=str(step.get("description") or ""),
                )
            scope = _infer_result_scope(
                analysis_id=step_name,
                goal=str(step.get("description") or ""),
                outputs=step.get("outputs") or [],
            )
            finding = {
                "step": step_name,
                "analysis_id": step_name,
                "scope": scope,
                "trust_level": "exploratory",
                "goal": str(step.get("description") or ""),
                "method_hint": str(step.get("method_hint") or ""),
                "summary": exec_result.get("summary") or {},
                "output_files": output_files,
                "notes": exec_result.get("notes") or "",
            }
            findings.append(finding)
            result_registry.append(
                _result_registry_entry(
                    analysis_id=step_name,
                    scope=scope,
                    trust_level="exploratory",
                    summary=exec_result.get("summary") or {},
                    output_files=output_files,
                    notes="LLM/codegen analysis result; use as exploratory when canonical results are available for the same scope.",
                )
            )
            step_results.append(
                {
                    "step": step_name,
                    "method": method or "llm_codegen",
                    "status": "success",
                    "output_files": output_files,
                    "result_preview": _safe_preview_dict(
                        exec_result.get("summary") or {}
                    ),
                }
            )
        else:
            issues.append(
                {
                    "code": "ANALYSIS_STEP_FAILED",
                    "severity": "medium",
                    "message": f"{step_name}: {exec_result.get('reason') or 'failed'}",
                }
            )
            step_results.append(
                {
                    "step": step_name,
                    "method": method or "llm_codegen",
                    "status": "failed",
                    "reason": exec_result.get("reason") or "failed",
                    "stderr_preview": (exec_result.get("stderr") or "")[:300],
                }
            )

    deterministic_outputs = _run_deterministic_chart_derivations(
        chart_plan=chart_plan or [],
        table_views=table_views,
        primary_csv_path=primary_csv_path,
        outputs_dir=outputs_dir,
        derived_views=derived_views,
        findings=findings,
        issues=issues,
    )
    analysis_outputs.update(deterministic_outputs)

    deterministic_artifacts: Dict[str, Any] = {}
    lightcycler_out = run_lightcycler_melt_analysis(
        dataset_bundle=dataset_bundle,
        outputs_dir=outputs_dir,
    )
    if lightcycler_out.get("status") == "success":
        analysis_id = str(
            lightcycler_out.get("analysis_id") or "lightcycler_melt_analysis"
        )
        output_files = list(lightcycler_out.get("output_files") or [])
        analysis_outputs[analysis_id] = output_files
        deterministic_artifacts["lightcycler_melt"] = (
            lightcycler_out.get("artifacts") or {}
        )
        finding = {
            "step": analysis_id,
            "analysis_id": analysis_id,
            "scope": "tm_qc_group_stats",
            "trust_level": "canonical",
            "goal": "Deterministically extract LightCycler melt Tm, QC, group statistics, and normalized curves.",
            "method_hint": "Service-side LightCycler melt analysis.",
            "summary": lightcycler_out.get("summary") or {},
            "output_files": output_files,
            "notes": "Generated only when LightCycler peaks/curves tables are present.",
        }
        findings.append(finding)
        result_registry.append(
            _result_registry_entry(
                analysis_id=analysis_id,
                scope="tm_qc_group_stats",
                trust_level="canonical",
                summary=lightcycler_out.get("summary") or {},
                artifacts=lightcycler_out.get("artifacts") or {},
                output_files=output_files,
                notes="Canonical deterministic melt Tm/QC/group-statistics result.",
            )
        )
        step_results.append(
            {
                "step": analysis_id,
                "method": "deterministic_lightcycler_melt_analysis",
                "status": "success",
                "output_files": output_files,
                "result_preview": _safe_preview_dict(
                    lightcycler_out.get("summary") or {}
                ),
            }
        )
        for key, path in (lightcycler_out.get("artifacts") or {}).items():
            if not path:
                continue
            view_id = f"view_{analysis_id}__{key}"
            _register_derived_view(
                derived_views=derived_views,
                view_id=view_id,
                csv_path=path,
                analysis_id=analysis_id,
                note=f"Deterministic LightCycler melt artifact: {key}.",
            )
    elif lightcycler_out.get("status") != "skipped":
        issues.append(
            {
                "code": "LIGHTCYCLER_MELT_ANALYSIS_FAILED",
                "severity": "medium",
                "message": str(
                    lightcycler_out.get("reason")
                    or "LightCycler deterministic analysis failed."
                ),
            }
        )

    prediction_review_out = run_prediction_review(
        context_text=requirement_context_text,
        analysis_artifacts=deterministic_artifacts,
        outputs_dir=outputs_dir,
    )
    if prediction_review_out.get("status") == "success":
        analysis_id = str(
            prediction_review_out.get("analysis_id") or "r7_prediction_review"
        )
        output_files = list(prediction_review_out.get("output_files") or [])
        analysis_outputs[analysis_id] = output_files
        deterministic_artifacts["prediction_review"] = (
            prediction_review_out.get("artifacts") or {}
        )
        finding = {
            "step": analysis_id,
            "analysis_id": analysis_id,
            "scope": "prediction_vs_observed",
            "trust_level": "canonical",
            "goal": "Compare R7 predicted Tm gain against observed LightCycler group Tm gain and flag residual anomalies.",
            "method_hint": "Service-side prediction-vs-observed residual analysis. Biological explanations are hypotheses, not confirmed causal mechanisms.",
            "summary": prediction_review_out.get("summary") or {},
            "output_files": output_files,
            "notes": "Generated when Top1-Top5 predicted gains are present in the prompt/context and LightCycler group statistics are available.",
        }
        findings.append(finding)
        result_registry.append(
            _result_registry_entry(
                analysis_id=analysis_id,
                scope="prediction_vs_observed",
                trust_level="canonical",
                summary=prediction_review_out.get("summary") or {},
                artifacts=prediction_review_out.get("artifacts") or {},
                output_files=output_files,
                notes="Canonical prediction-vs-observed residual review.",
            )
        )
        step_results.append(
            {
                "step": analysis_id,
                "method": "deterministic_prediction_review",
                "status": "success",
                "output_files": output_files,
                "result_preview": _safe_preview_dict(
                    prediction_review_out.get("summary") or {}
                ),
            }
        )
        for key, path in (prediction_review_out.get("artifacts") or {}).items():
            if not path or not str(path).lower().endswith(".csv"):
                continue
            view_id = f"view_{analysis_id}__{key}"
            _register_derived_view(
                derived_views=derived_views,
                view_id=view_id,
                csv_path=path,
                analysis_id=analysis_id,
                note=f"Deterministic R7 prediction review artifact: {key}.",
            )
    elif prediction_review_out.get("status") != "skipped":
        issues.append(
            {
                "code": "PREDICTION_REVIEW_FAILED",
                "severity": "medium",
                "message": str(
                    prediction_review_out.get("reason")
                    or "R7 prediction review failed."
                ),
            }
        )

    analysis_results = {
        "analysis_version": "3.0",
        "primary_view_id": primary_view_id,
        "derived_views": derived_views,
        "step_results": step_results,
        "findings": findings,
        "qc_flags": [],
        "metrics": {},
        "fits": {},
        "stat_tests": {},
        "analysis_outputs": analysis_outputs,
        "deterministic_artifacts": deterministic_artifacts,
        "result_registry": result_registry,
    }

    chart_data_ready_results = _prepare_chart_ready_results(
        chart_plan=chart_plan or [],
        analysis_results=analysis_results,
    )

    return {
        "analysis_results": analysis_results,
        "chart_data_ready_results": chart_data_ready_results,
        "issues": issues,
        "fatal": not step_results or any(item.get("status") != "success" for item in step_results),
        "analysis_log": logs,
        "analysis_code_paths": code_paths_all,
    }
