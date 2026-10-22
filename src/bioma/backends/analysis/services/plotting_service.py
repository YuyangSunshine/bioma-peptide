from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from bioma.backends.analysis.processes import run_python
from bioma.backends.analysis.artifacts import task_artifact_dir

from bioma.backends.analysis.tools.plotting_tools import (
    generate_viz_analysis_python_code_tool,
    generate_viz_python_code_tool,
    generate_visual_observation_tool,
)

from bioma.backends.analysis.services.lightcycler_fixed_plots import generate_lightcycler_fixed_plots


# =========================================================
# Basic helpers
# =========================================================


def _utc_now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_dir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def _norm(s: Any) -> str:
    s = str(s or "").strip().lower()
    out = []
    prev_us = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        else:
            if not prev_us:
                out.append("_")
                prev_us = True
    s2 = "".join(out).strip("_")
    while "__" in s2:
        s2 = s2.replace("__", "_")
    return s2


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _table_map(bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in bundle.get("tables") or []:
        tid = str(t.get("table_id") or "")
        if tid:
            out[tid] = t
    return out


def _top_binding_column(
    bindings: Dict[str, Any], field: str, threshold: float = 0.55
) -> Optional[str]:
    cands = (bindings or {}).get(field) or []
    if not cands:
        return None
    top = cands[0]
    conf = float(top.get("confidence", 0.0))
    if conf < threshold:
        return None
    col = top.get("column")
    return str(col) if col else None


def _load_table_df(table: Dict[str, Any]) -> pd.DataFrame:
    path = str(table.get("path") or "")
    if not path:
        raise ValueError("Table path missing.")
    return pd.read_csv(path, dtype=object, encoding="utf-8-sig")


def _matrix_to_long(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or df.shape[1] < 2:
        return df.copy()
    row_col = str(df.columns[0])
    long_df = df.melt(
        id_vars=[row_col], var_name="matrix_col_label", value_name="value"
    )
    return long_df.rename(columns={row_col: "matrix_row_label"})


def _materialize_table_view(table: Dict[str, Any]) -> pd.DataFrame:
    df = _load_table_df(table).copy()
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")

    layout_type = str(table.get("layout_type") or "unknown")
    bindings = table.get("canonical_bindings") or {}

    if layout_type == "matrix":
        df = _matrix_to_long(df)

    for field in [
        "sample",
        "group",
        "target",
        "well",
        "replicate",
        "cycle",
        "temperature",
        "time",
        "concentration",
        "ct",
        "fluorescence",
        "intensity",
        "value",
        "mean_value",
        "std_value",
        "cv",
        "p_value",
        "s_value",
        "ratio_value",
        "size_bp",
        "peak_area",
        "peak_height",
    ]:
        col = _top_binding_column(bindings, field)
        if col and col in df.columns and field not in df.columns:
            df[field] = df[col]

    df["__source_table_id"] = str(table.get("table_id") or "")
    return df


_QPCR_RELATIVE_EXPRESSION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "gene": (
        "gene",
        "genes",
        "target",
        "targets",
        "target_gene",
        "target_genes",
        "marker",
        "markers",
        "assay",
        "assays",
        "基因",
        "目标基因",
    ),
    "group": (
        "group",
        "groups",
        "condition",
        "conditions",
        "treatment",
        "treatments",
        "sample_group",
        "genotype",
        "variant",
        "mutation",
        "class",
        "组别",
        "分组",
        "处理",
        "条件",
    ),
    "value": (
        "relative_expression",
        "relative_expr",
        "rel_expression",
        "expression",
        "fold_change",
        "foldchange",
        "fold",
        "2_ddct",
        "2_delta_delta_ct",
        "ddct_value",
        "mean_relative_expression",
        "mean_rel_expression",
        "mean_fold_change",
        "mean_foldchange",
        "avg_relative_expression",
        "average_relative_expression",
        "value",
        "mean",
        "mean_value",
        "相对表达量",
        "表达量",
        "差异倍数",
    ),
    "error": (
        "error",
        "err",
        "sd",
        "std",
        "stddev",
        "standard_deviation",
        "sem",
        "stderr",
        "standard_error",
        "ci",
        "ci95",
        "置信区间",
        "误差",
    ),
    "p_value": (
        "p_value",
        "pvalue",
        "p",
        "padj",
        "adjusted_p_value",
        "adj_p_value",
        "q_value",
        "qvalue",
        "p值",
    ),
    "significance": (
        "significance",
        "significant",
        "sig",
        "stars",
        "asterisk",
        "显著性",
        "显著性标记",
    ),
    "label": (
        "label",
        "value_label",
        "bar_label",
        "annotation",
        "display_value",
        "标签",
        "柱顶数值",
    ),
}


def _find_column_by_name(df: pd.DataFrame, candidates: List[Any]) -> Optional[str]:
    by_norm = {_norm(col): str(col) for col in df.columns}
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        if text in df.columns:
            return text
        found = by_norm.get(_norm(text))
        if found:
            return found
    return None


def _explicit_qpcr_target_gene_label(chart: Dict[str, Any]) -> Optional[str]:
    spec = chart.get("chart_spec") or {}
    for key in ("target_gene", "target", "gene"):
        value = str(spec.get(key) or "").strip()
        if value:
            return value
    return None


def _candidate_columns_for_role(chart: Dict[str, Any], role: str) -> List[Any]:
    spec = chart.get("chart_spec") or {}
    role_keys = {
        "gene": ("x", "gene", "target"),
        "group": ("series_by", "group", "condition"),
        "value": ("y", "value", "relative_expression"),
        "error": ("error", "y_error", "err"),
        "p_value": ("p_value",),
        "significance": ("significance",),
        "label": ("label",),
    }
    candidates: List[Any] = []
    for key in role_keys.get(role, ()):
        candidates.append(spec.get(key))
    candidates.extend(_QPCR_RELATIVE_EXPRESSION_ALIASES.get(role, ()))
    return candidates


def _prepare_qpcr_relative_expression_bar_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    out = df.copy()
    role_to_column: Dict[str, str] = {}

    for role in ("gene", "group", "value", "error", "p_value", "significance", "label"):
        col = _find_column_by_name(out, _candidate_columns_for_role(chart, role))
        if col:
            role_to_column[role] = col

    if (
        role_to_column.get("gene")
        and role_to_column.get("group")
        and role_to_column["gene"] == role_to_column["group"]
    ):
        role_to_column.pop("gene", None)

    if (
        "gene" not in role_to_column
        and "group" in role_to_column
        and "value" in role_to_column
    ):
        target_gene = _explicit_qpcr_target_gene_label(chart)
        if target_gene:
            out["gene"] = target_gene
            role_to_column["gene"] = "gene"

    for role in ("gene", "group", "value"):
        if role not in role_to_column:
            issues.append(
                {
                    "code": "QPCR_RELATIVE_EXPRESSION_FIELD_MISSING",
                    "severity": "high",
                    "message": (
                        f"{chart.get('chart_id')}: missing required role '{role}' "
                        f"for qpcr_relative_expression_bar. Available columns: "
                        f"{', '.join(str(col) for col in out.columns)}"
                    ),
                }
            )

    for role, col in role_to_column.items():
        if role in {"value", "error", "p_value"}:
            out[role] = _to_numeric(out[col])
        elif role not in out.columns:
            out[role] = out[col]

    return _add_plot_page_by_series(out), issues


_QPCR_MELT_CURVE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "temperature": (
        "temperature",
        "temp",
        "tm_temperature",
        "melt_temperature",
        "temperature_c",
        "temperature_celsius",
        "t",
        "x",
    ),
    "value": (
        "negative_derivative",
        "neg_derivative",
        "derivative",
        "-df_dt",
        "minus_df_dt",
        "df_dt",
        "fluorescence",
        "fluor",
        "rfu",
        "signal",
        "intensity",
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
        "gene",
        "genotype",
        "mutation",
        "group",
        "dye",
    ),
    "fluorescence": ("fluorescence", "fluor", "rfu", "raw_fluorescence", "signal"),
    "derivative": (
        "negative_derivative",
        "neg_derivative",
        "derivative",
        "-df_dt",
        "minus_df_dt",
        "df_dt",
    ),
    "tm": ("tm", "melting_temperature", "peak_temperature", "peak_temp"),
    "peak_label": ("peak_label", "peak_annotation", "label", "annotation"),
    "peak_count": ("peak_count", "n_peaks", "peak_number"),
    "peak_type": ("peak_type", "peak_class", "peak_pattern"),
    "genotype": ("genotype", "mutation", "variant", "call"),
    "sample": ("sample", "sample_id", "well", "well_id"),
}

_MAX_SERIES_PER_PLOT_PAGE = 6


def _add_plot_page_by_series(df: pd.DataFrame) -> pd.DataFrame:
    if "series" not in df.columns:
        return df
    series_order = [series for series in pd.unique(df["series"].dropna())]
    page_by_series = {
        series: (idx // _MAX_SERIES_PER_PLOT_PAGE) + 1
        for idx, series in enumerate(series_order)
    }
    out = df.copy()
    out["plot_page"] = out["series"].map(page_by_series).fillna(1).astype(int)
    return out


_PLATE_FRET_TIME_CURVE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "time": (
        "time",
        "time_s",
        "time_sec",
        "time_min",
        "minute",
        "minutes",
        "cycle",
        "x",
    ),
    "value": (
        "fret_ratio",
        "ratio",
        "ratio_value",
        "fret",
        "normalized_fret",
        "mp",
        "mP",
        "fluorescence",
        "intensity",
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
        "condition",
        "treatment",
        "group",
        "channel",
    ),
    "donor_signal": ("donor", "donor_signal", "donor_intensity", "donor_fluorescence"),
    "acceptor_signal": (
        "acceptor",
        "acceptor_signal",
        "acceptor_intensity",
        "acceptor_fluorescence",
    ),
    "ratio": ("fret_ratio", "ratio", "ratio_value", "normalized_fret"),
    "mp": ("mp", "mP", "polarization", "polarization_mp", "anisotropy"),
    "parallel_signal": (
        "p",
        "parallel",
        "parallel_signal",
        "p_signal",
        "parallel_intensity",
    ),
    "perpendicular_signal": (
        "s",
        "perpendicular",
        "perpendicular_signal",
        "s_signal",
        "vertical",
        "vertical_signal",
    ),
    "annotation": ("annotation", "label", "event", "note"),
    "vline": ("vline", "event_time", "marker_time", "vertical_line"),
    "label": ("label", "trace_label", "annotation"),
}


_QPCR_AMPLIFICATION_CURVE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "cycle": ("cycle", "cycles", "cycle_number", "ct_cycle", "pcr_cycle", "x"),
    "value": (
        "delta_rn",
        "drn",
        "rn",
        "fluorescence",
        "fluor",
        "rfu",
        "intensity",
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
        "gene",
        "replicate",
        "dye",
        "group",
    ),
    "ct": ("ct", "cq", "threshold_cycle", "crossing_point"),
    "cp": ("cp", "crossing_point", "ct", "cq"),
    "threshold": ("threshold", "threshold_value", "threshold_y", "ct_threshold"),
    "baseline": ("baseline", "baseline_value"),
    "annotation": ("annotation", "ct_annotation", "label", "note"),
    "label": ("label", "trace_label", "annotation"),
}


_PLATE_STANDARD_CURVE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "concentration": (
        "concentration",
        "conc",
        "standard_concentration",
        "dose",
        "amount",
        "ng_ml",
        "ug_ml",
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
        "mean_value",
        "y",
    ),
    "series": ("series", "assay", "plate", "batch", "group"),
    "sample_type": ("sample_type", "type", "kind", "standard_or_sample"),
    "error": ("error", "err", "sd", "std", "stddev", "sem", "cv", "ci", "ci95"),
    "fit_value": ("fit_value", "fitted", "predicted", "predicted_response", "y_fit"),
    "lloq": ("lloq", "lower_limit", "lower_limit_of_quantification"),
    "uloq": ("uloq", "upper_limit", "upper_limit_of_quantification"),
    "equation": ("equation", "fit_equation", "formula"),
    "r_squared": ("r_squared", "r2", "r_sq", "rsquared"),
}


_PLATE_MICHAELIS_MENTEN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "substrate_concentration": (
        "substrate_concentration",
        "substrate",
        "s",
        "s_concentration",
        "concentration",
        "conc",
        "substrate_um",
        "substrate_uM",
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
    "series": ("series", "enzyme", "sample", "condition", "treatment", "group"),
    "error": ("error", "err", "sd", "std", "stddev", "sem", "ci", "ci95"),
    "fit_value": ("fit_value", "fitted", "predicted", "predicted_velocity", "y_fit"),
    "confidence_band": (
        "confidence_band",
        "ci",
        "ci95",
        "fit_ci",
        "confidence_interval",
    ),
    "km": ("km", "k_m", "michaelis_constant"),
    "vmax": ("vmax", "v_max", "maximum_velocity"),
    "label": ("label", "annotation", "point_label"),
}


_PLATE_SAMPLE_CONCENTRATION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "sample": ("sample", "sample_id", "well", "well_id", "name", "label", "x"),
    "group": ("group", "condition", "treatment", "sample_group", "series", "class"),
    "value": (
        "value",
        "concentration",
        "sample_concentration",
        "calculated_concentration",
        "back_calculated_concentration",
        "amount",
        "response",
        "signal",
        "mean_value",
        "ng_ml",
        "ug_ml",
        "y",
    ),
    "error": ("error", "err", "sd", "std", "stddev", "sem", "ci", "ci95"),
    "n": ("n", "count", "replicate_count"),
    "cv": ("cv", "coefficient_of_variation"),
    "p_value": ("p_value", "pvalue", "p", "padj", "adjusted_p_value"),
    "significance": ("significance", "significant", "sig", "stars"),
    "label": ("label", "value_label", "annotation"),
}


_CE_DNA_ELECTROPHEROGRAM_ALIASES: Dict[str, Tuple[str, ...]] = {
    "size_bp": ("size_bp", "fragment_size", "fragment_size_bp", "bp", "size", "x"),
    "intensity": ("intensity", "fluorescence", "rfu", "signal", "value", "height", "y"),
    "series": ("series", "sample", "sample_id", "lane", "well", "trace", "library"),
    "peak_size_bp": ("peak_size_bp", "main_peak_bp", "peak_bp", "peak_size"),
    "peak_height": ("peak_height", "height", "max_intensity"),
    "peak_area": ("peak_area", "area"),
    "peak_count": ("peak_count", "n_peaks"),
    "peak_label": ("peak_label", "label", "annotation"),
    "concentration": ("concentration", "ng_ul", "ng_per_ul", "ngul", "amount"),
    "gqs": ("gqs", "quality_score", "quality"),
}


_CE_RNA_ELECTROPHEROGRAM_ALIASES: Dict[str, Tuple[str, ...]] = {
    "size_nt": ("size_nt", "fragment_size_nt", "nt", "size", "size_bp", "x"),
    "intensity": ("intensity", "fluorescence", "rfu", "signal", "value", "height", "y"),
    "series": ("series", "sample", "sample_id", "lane", "well", "trace"),
    "peak_size_nt": ("peak_size_nt", "main_peak_nt", "peak_nt", "peak_size"),
    "peak_height": ("peak_height", "height", "max_intensity"),
    "peak_area": ("peak_area", "area"),
    "peak_count": ("peak_count", "n_peaks"),
    "peak_label": ("peak_label", "label", "annotation"),
    "ratio_28s_18s": ("ratio_28s_18s", "28s_18s_ratio", "ratio"),
    "rqs": ("rqs", "rin", "rna_integrity", "quality_score", "quality"),
}


_CE_VIRTUAL_GEL_ALIASES: Dict[str, Tuple[str, ...]] = {
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
    "main_band_size": ("main_band_size", "band_size", "main_peak_size"),
    "main_band_intensity": (
        "main_band_intensity",
        "band_intensity",
        "main_peak_height",
    ),
    "smear_index": ("smear_index", "smear"),
    "quality_label": ("quality_label", "quality", "label"),
}


def _candidate_columns_for_chart_role(
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


def _prepare_standardized_chart_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
    *,
    chart_key: str,
    required_roles: Tuple[str, ...],
    optional_roles: Tuple[str, ...],
    role_keys: Dict[str, Tuple[str, ...]],
    aliases: Dict[str, Tuple[str, ...]],
    numeric_roles: set[str],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    out = df.copy()
    role_to_column: Dict[str, str] = {}

    for role in (*required_roles, *optional_roles):
        col = _find_column_by_name(
            out,
            _candidate_columns_for_chart_role(chart, role, role_keys, aliases),
        )
        if col:
            role_to_column[role] = col

    missing = [role for role in required_roles if role not in role_to_column]
    if missing:
        issues.append(
            {
                "code": "CHART_READY_FIELD_MISSING",
                "severity": "high",
                "message": (
                    f"{chart.get('chart_id')}: missing required role(s) "
                    f"{', '.join(missing)} for {chart_key}. Available columns: "
                    f"{', '.join(str(col) for col in out.columns)}"
                ),
            }
        )

    for role, col in role_to_column.items():
        if role in numeric_roles:
            out[role] = _to_numeric(out[col])
        elif role not in out.columns:
            out[role] = out[col]

    return _add_plot_page_by_series(out), issues


def _prepare_qpcr_melt_curve_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    out, issues = _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="qpcr_melt_curve_neg_derivative",
        required_roles=("temperature", "value", "series"),
        optional_roles=(
            "fluorescence",
            "derivative",
            "tm",
            "peak_label",
            "peak_count",
            "peak_type",
            "genotype",
            "sample",
        ),
        role_keys={
            "temperature": ("x", "temperature"),
            "value": ("y", "value", "derivative", "fluorescence"),
            "series": ("series_by", "series", "group", "sample", "well"),
            "fluorescence": ("fluorescence",),
            "derivative": ("derivative", "y"),
            "tm": ("tm", "peak_temperature"),
            "peak_label": ("peak_label", "label", "annotation"),
            "peak_count": ("peak_count",),
            "peak_type": ("peak_type",),
            "genotype": ("genotype", "group"),
            "sample": ("sample", "well"),
        },
        aliases=_QPCR_MELT_CURVE_ALIASES,
        numeric_roles={
            "temperature",
            "value",
            "fluorescence",
            "derivative",
            "tm",
            "peak_count",
        },
    )
    if "plot_page" in out.columns:
        out = out.drop(columns=["plot_page"])
    return out, issues


def _prepare_plate_fret_time_curve_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    out, issues = _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="plate_fret_time_curve",
        required_roles=("time", "series"),
        optional_roles=(
            "value",
            "donor_signal",
            "acceptor_signal",
            "ratio",
            "mp",
            "parallel_signal",
            "perpendicular_signal",
            "annotation",
            "vline",
            "label",
        ),
        role_keys={
            "time": ("x", "time", "cycle"),
            "value": ("value",),
            "series": ("series_by", "series", "group", "sample", "well"),
            "donor_signal": ("donor_signal", "donor"),
            "acceptor_signal": ("acceptor_signal", "acceptor"),
            "ratio": ("ratio", "y"),
            "mp": ("mp", "y"),
            "parallel_signal": ("parallel_signal", "parallel", "p_signal", "p"),
            "perpendicular_signal": (
                "perpendicular_signal",
                "perpendicular",
                "s_signal",
                "s",
            ),
            "annotation": ("annotation", "label"),
            "vline": ("vline", "event_time"),
            "label": ("label",),
        },
        aliases=_PLATE_FRET_TIME_CURVE_ALIASES,
        numeric_roles={
            "time",
            "value",
            "donor_signal",
            "acceptor_signal",
            "ratio",
            "mp",
            "parallel_signal",
            "perpendicular_signal",
            "vline",
        },
    )
    if "ratio" in out.columns:
        out["ratio"] = _to_numeric(out["ratio"])
        out["value"] = out["ratio"]
    elif "mp" in out.columns:
        out["mp"] = _to_numeric(out["mp"])
        out["value"] = out["mp"]
    elif "parallel_signal" in out.columns and "perpendicular_signal" in out.columns:
        parallel = _to_numeric(out["parallel_signal"])
        perpendicular = _to_numeric(out["perpendicular_signal"])
        out["mp"] = (
            (parallel - perpendicular)
            / (parallel + perpendicular).replace(0, pd.NA)
            * 1000
        )
        out["value"] = out["mp"]
    elif "value" not in out.columns:
        issues.append(
            {
                "code": "CHART_READY_FIELD_MISSING",
                "severity": "high",
                "message": (
                    f"{chart.get('chart_id')}: missing FRET value for plate_fret_time_curve. "
                    "Provide ratio/mP or parallel(P) and perpendicular(S) signals."
                ),
            }
        )
    return out, issues


def _prepare_qpcr_amplification_curve_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    out, issues = _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="qpcr_amplification_curve",
        required_roles=("cycle", "value", "series"),
        optional_roles=("ct", "cp", "threshold", "baseline", "annotation", "label"),
        role_keys={
            "cycle": ("x", "cycle"),
            "value": ("y", "value", "fluorescence"),
            "series": ("series_by", "series", "group", "sample", "well"),
            "ct": ("ct",),
            "cp": ("cp",),
            "threshold": ("threshold",),
            "baseline": ("baseline",),
            "annotation": ("annotation", "label"),
            "label": ("label",),
        },
        aliases=_QPCR_AMPLIFICATION_CURVE_ALIASES,
        numeric_roles={"cycle", "value", "ct", "cp", "threshold", "baseline"},
    )
    return _add_plot_page_by_series(out), issues


def _prepare_plate_standard_curve_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    return _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="plate_standard_curve",
        required_roles=("concentration", "response"),
        optional_roles=(
            "series",
            "sample_type",
            "error",
            "fit_value",
            "lloq",
            "uloq",
            "equation",
            "r_squared",
            "adjusted_r_squared",
            "rmse",
            "fit_model",
            "predicted_molecular_weight",
        ),
        role_keys={
            "concentration": ("x", "concentration"),
            "response": ("y", "response", "value"),
            "series": ("series_by", "series", "group"),
            "sample_type": ("sample_type", "type"),
            "error": ("error", "y_error"),
            "fit_value": ("fit_value", "fitted"),
            "lloq": ("lloq",),
            "uloq": ("uloq",),
            "equation": ("equation",),
            "r_squared": ("r_squared", "r2"),
            "adjusted_r_squared": ("adjusted_r_squared", "adjusted_r2"),
            "rmse": ("rmse",),
            "fit_model": ("fit_model",),
            "predicted_molecular_weight": ("predicted_molecular_weight",),
        },
        aliases=_PLATE_STANDARD_CURVE_ALIASES,
        numeric_roles={
            "concentration",
            "response",
            "error",
            "fit_value",
            "lloq",
            "uloq",
            "r_squared",
            "adjusted_r_squared",
            "rmse",
            "predicted_molecular_weight",
        },
    )


def _prepare_plate_michaelis_menten_curve_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    return _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="plate_michaelis_menten_curve",
        required_roles=("substrate_concentration", "velocity"),
        optional_roles=(
            "series",
            "error",
            "fit_value",
            "confidence_band",
            "km",
            "vmax",
            "label",
        ),
        role_keys={
            "substrate_concentration": (
                "x",
                "substrate_concentration",
                "concentration",
            ),
            "velocity": ("y", "velocity", "rate", "value"),
            "series": ("series_by", "series", "group"),
            "error": ("error", "y_error"),
            "fit_value": ("fit_value", "fitted"),
            "confidence_band": ("confidence_band", "ci"),
            "km": ("km",),
            "vmax": ("vmax",),
            "label": ("label", "annotation"),
        },
        aliases=_PLATE_MICHAELIS_MENTEN_ALIASES,
        numeric_roles={
            "substrate_concentration",
            "velocity",
            "error",
            "fit_value",
            "confidence_band",
            "km",
            "vmax",
        },
    )


def _prepare_plate_sample_concentration_bar_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    return _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="plate_sample_concentration_bar",
        required_roles=("sample", "value"),
        optional_roles=(
            "group",
            "error",
            "n",
            "cv",
            "p_value",
            "significance",
            "label",
        ),
        role_keys={
            "sample": ("x", "sample"),
            "value": ("y", "value", "concentration"),
            "group": ("series_by", "group"),
            "error": ("error", "y_error"),
            "n": ("n",),
            "cv": ("cv",),
            "p_value": ("p_value",),
            "significance": ("significance",),
            "label": ("label", "annotation"),
        },
        aliases=_PLATE_SAMPLE_CONCENTRATION_ALIASES,
        numeric_roles={"value", "error", "n", "cv", "p_value"},
    )


def _prepare_ce_dna_electropherogram_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    return _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="ce_dna_electropherogram",
        required_roles=("size_bp", "intensity", "series"),
        optional_roles=(
            "peak_size_bp",
            "peak_height",
            "peak_area",
            "peak_count",
            "peak_label",
            "concentration",
            "gqs",
        ),
        role_keys={
            "size_bp": ("x", "size", "size_bp"),
            "intensity": ("y", "intensity", "value"),
            "series": ("series_by", "series", "sample", "lane"),
            "peak_size_bp": ("peak_size_bp", "peak_size"),
            "peak_height": ("peak_height",),
            "peak_area": ("peak_area",),
            "peak_count": ("peak_count",),
            "peak_label": ("peak_label", "label"),
            "concentration": ("concentration",),
            "gqs": ("gqs",),
        },
        aliases=_CE_DNA_ELECTROPHEROGRAM_ALIASES,
        numeric_roles={
            "size_bp",
            "intensity",
            "peak_size_bp",
            "peak_height",
            "peak_area",
            "peak_count",
            "concentration",
            "gqs",
        },
    )


def _prepare_ce_rna_electropherogram_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    return _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="ce_rna_electropherogram",
        required_roles=("size_nt", "intensity", "series"),
        optional_roles=(
            "peak_size_nt",
            "peak_height",
            "peak_area",
            "peak_count",
            "peak_label",
            "ratio_28s_18s",
            "rqs",
        ),
        role_keys={
            "size_nt": ("x", "size", "size_nt"),
            "intensity": ("y", "intensity", "value"),
            "series": ("series_by", "series", "sample", "lane"),
            "peak_size_nt": ("peak_size_nt", "peak_size"),
            "peak_height": ("peak_height",),
            "peak_area": ("peak_area",),
            "peak_count": ("peak_count",),
            "peak_label": ("peak_label", "label"),
            "ratio_28s_18s": ("ratio_28s_18s",),
            "rqs": ("rqs", "rin"),
        },
        aliases=_CE_RNA_ELECTROPHEROGRAM_ALIASES,
        numeric_roles={
            "size_nt",
            "intensity",
            "peak_size_nt",
            "peak_height",
            "peak_area",
            "peak_count",
            "ratio_28s_18s",
            "rqs",
        },
    )


def _prepare_ce_virtual_gel_view(
    df: pd.DataFrame,
    chart: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    return _prepare_standardized_chart_view(
        df,
        chart,
        chart_key="ce_virtual_gel",
        required_roles=("lane", "size", "intensity"),
        optional_roles=(
            "sample",
            "marker",
            "main_band_size",
            "main_band_intensity",
            "smear_index",
            "quality_label",
        ),
        role_keys={
            "lane": ("x", "lane", "sample", "series_by"),
            "sample": ("sample",),
            "size": ("y", "size", "size_bp", "size_nt"),
            "intensity": ("value", "intensity", "band_intensity"),
            "marker": ("marker", "sample_type"),
            "main_band_size": ("main_band_size",),
            "main_band_intensity": ("main_band_intensity",),
            "smear_index": ("smear_index",),
            "quality_label": ("quality_label", "label"),
        },
        aliases=_CE_VIRTUAL_GEL_ALIASES,
        numeric_roles={
            "size",
            "intensity",
            "main_band_size",
            "main_band_intensity",
            "smear_index",
        },
    )


_CHART_VIEW_PREPARERS = {
    "qpcr_relative_expression_bar": _prepare_qpcr_relative_expression_bar_view,
    "qpcr_melt_curve_neg_derivative": _prepare_qpcr_melt_curve_view,
    "plate_fret_time_curve": _prepare_plate_fret_time_curve_view,
    "qpcr_amplification_curve": _prepare_qpcr_amplification_curve_view,
    "plate_standard_curve": _prepare_plate_standard_curve_view,
    "plate_michaelis_menten_curve": _prepare_plate_michaelis_menten_curve_view,
    "plate_sample_concentration_bar": _prepare_plate_sample_concentration_bar_view,
    "ce_dna_electropherogram": _prepare_ce_dna_electropherogram_view,
    "ce_rna_electropherogram": _prepare_ce_rna_electropherogram_view,
    "ce_virtual_gel": _prepare_ce_virtual_gel_view,
}


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
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
    code_path: str,
    timeout: int = 120,
) -> Dict[str, Any]:
    try:
        proc = run_python(code_path, timeout=timeout)
        ok = proc.returncode == 0
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
    except Exception as e:
        ok = False
        stdout = ""
        stderr = f"Exception when running code: {repr(e)}"

    return {
        "ok": ok,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
    }


def _resolve_chart_source_table_id(
    chart_item: Dict[str, Any],
    chart_ready_results: Dict[str, Any],
    data_profile: Dict[str, Any],
) -> Optional[str]:
    table_id = chart_item.get("source_table_id")
    if table_id:
        return str(table_id)

    chart_id = chart_item.get("chart_id")
    chart_inputs = chart_ready_results.get("chart_inputs") or []
    for item in chart_inputs:
        if str(item.get("chart_id") or "") == str(chart_id or ""):
            tid = item.get("source_table_id")
            if tid:
                return str(tid)

    primary = (data_profile.get("run_context") or {}).get("primary_table_id")
    return str(primary) if primary else None


def _resolve_chart_source_view_csv_path(
    chart_item: Dict[str, Any],
    chart_ready_results: Dict[str, Any],
) -> Optional[str]:
    chart_id = chart_item.get("chart_id")
    for item in chart_ready_results.get("chart_inputs") or []:
        if str(item.get("chart_id") or "") != str(chart_id or ""):
            continue
        path = str(item.get("source_view_csv_path") or "")
        if path and os.path.exists(path):
            return path
    direct_path = str(chart_item.get("deterministic_derived_path") or "")
    if direct_path and os.path.exists(direct_path):
        return direct_path
    return None


def _materialize_chart_csvs(
    dataset_bundle: Dict[str, Any],
    data_profile: Dict[str, Any],
    chart_plan: List[Dict[str, Any]],
    chart_data_ready_results: Dict[str, Any],
    work_dir: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []
    table_map = _table_map(dataset_bundle)
    out: List[Dict[str, Any]] = []

    csv_dir = _ensure_dir(os.path.join(work_dir, "chart_views"))

    for idx, chart in enumerate(chart_plan or []):
        chart_id = str(chart.get("chart_id") or f"chart_{idx + 1:02d}")
        chart_key = str(chart.get("chart_key") or "unknown")
        table_id = _resolve_chart_source_table_id(
            chart, chart_data_ready_results, data_profile
        )

        if not table_id or table_id not in table_map:
            issues.append(
                {
                    "code": "CHART_SOURCE_TABLE_NOT_FOUND",
                    "severity": "high",
                    "message": f"{chart_id}: cannot resolve source table.",
                }
            )
            continue

        table = table_map[table_id]
        try:
            source_view_csv = _resolve_chart_source_view_csv_path(
                chart, chart_data_ready_results
            )
            if source_view_csv:
                df = pd.read_csv(source_view_csv, dtype=object, encoding="utf-8-sig")
            else:
                df = _materialize_table_view(table)
            prepare_view = _CHART_VIEW_PREPARERS.get(chart_key)
            if prepare_view is not None:
                df, prepare_issues = prepare_view(df, chart)
                issues.extend(prepare_issues)
            csv_path = os.path.join(csv_dir, f"{chart_id}__view.csv")
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        except Exception as e:
            issues.append(
                {
                    "code": "CHART_VIEW_MATERIALIZE_FAILED",
                    "severity": "high",
                    "message": f"{chart_id}: {repr(e)}",
                }
            )
            continue

        chart_note = str(
            chart.get("chart_note")
            or chart.get("notes")
            or chart.get("rationale")
            or ""
        )
        chart_ready_note = (
            f"Chart-ready CSV columns: {', '.join(str(col) for col in df.columns)}.\n"
            "Use parsed_data_path exactly as the plotting source. Prefer canonical columns "
            "when present: gene/group/value/error for relative-expression bars; "
            "temperature/value/series for melt curves; time/value/series for FRET; "
            "cycle/value/series for amplification; concentration/response for standard curves; "
            "substrate_concentration/velocity for Michaelis-Menten; sample/value/error for sample "
            "concentration bars; size_bp/intensity/series for DNA electropherograms; "
            "size_nt/intensity/series for RNA electropherograms; lane/size/intensity for virtual gels. "
            "Do not require raw input "
            "columns that are absent from this chart-ready CSV."
        )
        if chart_note:
            chart_note = chart_note + "\n" + chart_ready_note
        else:
            chart_note = chart_ready_note

        out.append(
            {
                "chart_id": chart_id,
                "chart_key": chart_key,
                "chart_note": chart_note,
                "parsed_data_path": csv_path,
                "source_table_id": table_id,
                "preferred_fields": list(chart.get("preferred_fields") or []),
                "title": str(chart.get("title") or chart_key),
            }
        )

    return out, issues


def _find_existing_output_files(
    output_image_dir: str, output_files: List[str], chart_id: str
) -> List[str]:
    produced = []
    if output_files:
        for of in output_files:
            p = of if os.path.isabs(of) else os.path.join(output_image_dir, of)
            if os.path.exists(p):
                produced.append(p)
    else:
        guess = os.path.join(output_image_dir, f"{chart_id}.png")
        if os.path.exists(guess):
            produced.append(guess)
    return produced


# =========================================================
# Plot generation
# =========================================================


def generate_plots(
    task_id: str,
    stable_requirement: Dict[str, Any],
    data_profile: Dict[str, Any],
    dataset_bundle: Dict[str, Any],
    chart_plan: List[Dict[str, Any]],
    chart_data_ready_results: Optional[Dict[str, Any]] = None,
    artifact_root: str = "artifacts",
) -> Dict[str, Any]:
    chart_data_ready_results = chart_data_ready_results or {}
    issues: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    visualizations: List[Dict[str, Any]] = []
    code_paths: List[str] = []

    work_dir = str(
        task_artifact_dir(artifact_root, task_id, "plotting", _utc_now_tag())
    )
    code_dir = _ensure_dir(os.path.join(work_dir, "code"))
    img_dir = _ensure_dir(os.path.join(work_dir, "images"))

    fixed_out = generate_lightcycler_fixed_plots(
        deterministic_artifacts=chart_data_ready_results.get("deterministic_artifacts")
        or {},
        output_dir=img_dir,
    )
    issues.extend(fixed_out.get("issues") or [])
    for fixed_plot in fixed_out.get("plots") or []:
        chart_id = str(fixed_plot.get("chart_id") or "")
        image_path = str(fixed_plot.get("path") or "")
        data_path = str(fixed_plot.get("data_path") or "")
        if not chart_id or not image_path:
            continue
        visualizations.append(
            {
                "chart_id": chart_id,
                "chart_key": chart_id,
                "scope": "canonical_visualization",
                "trust_level": "canonical",
                "title": chart_id.replace("_", " ").title(),
                "image_paths": [image_path],
                "code_paths": [],
                "parsed_data_path": data_path,
                "source_table_id": "",
                "status": "success",
                "notes": "Deterministic LightCycler fixed plot.",
                "stdout": "",
                "stderr": "",
            }
        )

    if any(viz.get("trust_level") == "canonical" for viz in visualizations):
        logs.append(
            {
                "phase": "skip_llm_chart_generation",
                "reason": "Canonical deterministic visualizations are available.",
                "n_canonical_visualizations": sum(
                    1 for viz in visualizations if viz.get("trust_level") == "canonical"
                ),
            }
        )
        return {
            "visualizations": visualizations,
            "visualizations_code": [],
            "visualization_log": logs,
            "issues": issues,
            "fatal": False,
        }

    chart_items, prep_issues = _materialize_chart_csvs(
        dataset_bundle=dataset_bundle,
        data_profile=data_profile,
        chart_plan=chart_plan,
        chart_data_ready_results=chart_data_ready_results,
        work_dir=work_dir,
    )
    issues.extend(prep_issues)

    if not chart_items:
        return {
            "visualizations": visualizations,
            "visualizations_code": [],
            "visualization_log": logs,
            "issues": issues,
            "fatal": False,
        }

    max_retry = 3
    backoff = [0.5, 1.0, 2.0]

    for item in chart_items:
        chart_id = item["chart_id"]
        chart_key = item["chart_key"]
        parsed_data_path = item["parsed_data_path"]
        chart_note = item["chart_note"]

        tool_resp = None
        ok_codegen = False

        for attempt in range(max_retry):
            logs.append(
                {
                    "phase": "plot_codegen_call",
                    "chart_id": chart_id,
                    "attempt": attempt + 1,
                    "max_retry": max_retry,
                }
            )

            tool_resp = generate_viz_python_code_tool.invoke(
                {
                    "chart_id": chart_id,
                    "chart_key": chart_key,
                    "requirement": chart_note,
                    "data_profile": data_profile,
                    "parsed_data_path": parsed_data_path,
                    "output_image_dir": img_dir,
                }
            )

            ok_codegen = (
                isinstance(tool_resp, dict)
                and isinstance(tool_resp.get("charts"), list)
                and len(tool_resp["charts"]) > 0
            )
            logs.append(
                {
                    "phase": "plot_codegen_result",
                    "chart_id": chart_id,
                    "ok": ok_codegen,
                    "charts_count": len(tool_resp["charts"]) if ok_codegen else 0,
                }
            )

            if ok_codegen:
                break

            if attempt < max_retry - 1:
                time.sleep(backoff[attempt])

        if not ok_codegen:
            issues.append(
                {
                    "code": "PLOT_CODEGEN_FAILED",
                    "severity": "high",
                    "message": f"{chart_id}: plot code generation failed.",
                }
            )
            visualizations.append(
                {
                    "chart_id": chart_id,
                    "chart_key": chart_key,
                    "title": item["title"],
                    "image_paths": [],
                    "parsed_data_path": parsed_data_path,
                    "source_table_id": item["source_table_id"],
                    "status": "failed",
                    "notes": chart_note,
                    "stdout": "",
                    "stderr": "plot codegen failed",
                }
            )
            continue

        # execute returned chart code(s)
        one_success = False
        produced_imgs_all: List[str] = []
        last_stdout = ""
        last_stderr = ""
        local_code_paths: List[str] = []

        for k, c in enumerate(tool_resp.get("charts") or []):
            if not isinstance(c, dict):
                continue

            py_code = str(c.get("python_code") or "")
            output_files = c.get("output_files") or []
            script_id = f"{chart_id}__plot_{k + 1:02d}"
            code_path = os.path.join(code_dir, f"{script_id}.py")

            with open(code_path, "w", encoding="utf-8") as f:
                f.write(py_code)

            code_paths.append(code_path)
            local_code_paths.append(code_path)

            logs.append(
                {
                    "phase": "plot_exec_start",
                    "chart_id": chart_id,
                    "script_id": script_id,
                    "code_path": code_path,
                }
            )

            exec_out = _exec_python_code(code_path=code_path, timeout=120)

            produced = _find_existing_output_files(
                output_image_dir=img_dir,
                output_files=output_files,
                chart_id=chart_id,
            )
            produced_imgs_all.extend(produced)
            last_stdout = exec_out["stdout"]
            last_stderr = exec_out["stderr"]

            logs.append(
                {
                    "phase": "plot_exec_end",
                    "chart_id": chart_id,
                    "script_id": script_id,
                    "ok": exec_out["ok"],
                    "images": len(produced),
                    "stderr_preview": last_stderr[:300],
                }
            )

            if exec_out["ok"]:
                one_success = True

        produced_imgs_all = list(dict.fromkeys(produced_imgs_all))

        visualizations.append(
            {
                "chart_id": chart_id,
                "chart_key": chart_key,
                "title": item["title"],
                "image_paths": produced_imgs_all,
                "code_paths": local_code_paths,
                "parsed_data_path": parsed_data_path,
                "source_table_id": item["source_table_id"],
                "status": "success" if one_success else "failed",
                "notes": chart_note,
                "stdout": last_stdout,
                "stderr": last_stderr,
            }
        )

    return {
        "visualizations": visualizations,
        "visualizations_code": code_paths,
        "visualization_log": logs,
        "issues": issues,
        "fatal": False,
    }


# =========================================================
# Plot-level analysis
# =========================================================


def _json_safe_scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return str(value)
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _row_excerpt(row: pd.Series, preferred_cols: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for col in preferred_cols:
        if col in row.index:
            out[str(col)] = _json_safe_scalar(row[col])
    return out


def _result_payloads(results: List[Dict[str, Any]]) -> List[Any]:
    payloads: List[Any] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        payload = item.get("analysis_result")
        if payload is not None:
            payloads.append(payload)
    return payloads[:5]


def _build_visual_observation(
    *,
    chart_id: str,
    chart_key: str,
    image_paths: List[str],
    chart_note: str,
) -> Dict[str, Any]:
    paths = [str(p) for p in (image_paths or []) if str(p).strip()]
    if not paths:
        return {
            "status": "missing_image",
            "chart_id": chart_id,
            "chart_key": chart_key,
            "image_paths": [],
            "chart_type_matches_expected": "unknown",
            "axes_visible": "unknown",
            "legend_visible": "unknown",
            "visual_trends": [],
            "quality_issues": [
                "No rendered image path is available for visual inspection."
            ],
            "notes": chart_note,
        }

    try:
        invoke = getattr(generate_visual_observation_tool, "invoke", None)
        if callable(invoke):
            observed = invoke(
                {
                    "chart_id": chart_id,
                    "chart_key": chart_key,
                    "requirement": chart_note,
                    "image_paths": paths,
                }
            )
            if isinstance(observed, dict):
                observed.setdefault("chart_id", chart_id)
                observed.setdefault("chart_key", chart_key)
                observed.setdefault("image_paths", paths)
                observed.setdefault("chart_type_matches_expected", "unknown")
                observed.setdefault("axes_visible", "unknown")
                observed.setdefault("legend_visible", "unknown")
                observed.setdefault("visual_trends", [])
                observed.setdefault("quality_issues", [])
                observed.setdefault("notes", chart_note)
                return observed
    except Exception as exc:
        return {
            "status": "vision_unavailable",
            "reason": repr(exc),
            "chart_id": chart_id,
            "chart_key": chart_key,
            "image_paths": paths,
            "chart_type_matches_expected": "unknown",
            "axes_visible": "unknown",
            "legend_visible": "unknown",
            "visual_trends": [],
            "quality_issues": ["Vision model call failed."],
            "notes": chart_note,
        }

    return {
        "status": "vision_unavailable",
        "reason": "generate_visual_observation_tool is not callable in this runtime.",
        "chart_id": chart_id,
        "chart_key": chart_key,
        "image_paths": paths,
        "chart_type_matches_expected": "unknown",
        "axes_visible": "unknown",
        "legend_visible": "unknown",
        "visual_trends": [],
        "quality_issues": ["Vision tool is unavailable."],
        "notes": chart_note,
    }


def _numeric_column_stats(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for col in df.columns:
        values = _to_numeric(df[col]).dropna()
        if values.empty:
            continue
        out[str(col)] = {
            "count": int(values.shape[0]),
            "min": float(values.min()),
            "max": float(values.max()),
            "mean": float(values.mean()),
        }
        if len(out) >= 8:
            break
    return out


def _categorical_preview(df: pd.DataFrame) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for col in df.columns:
        if col.startswith("__"):
            continue
        if _to_numeric(df[col]).notna().sum() > 0:
            continue
        counts = df[col].dropna().astype(str).value_counts().head(6)
        if not counts.empty:
            out[str(col)] = {str(k): int(v) for k, v in counts.items()}
        if len(out) >= 5:
            break
    return out


def _chart_specific_data_evidence(df: pd.DataFrame, chart_key: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    value_col = _find_column_by_name(
        df, ["value", "response", "velocity", "fluorescence", "derivative"]
    )
    series_col = _find_column_by_name(df, ["series", "sample", "group", "well"])
    time_col = _find_column_by_name(
        df,
        [
            "time",
            "cycle",
            "temperature",
            "substrate",
            "substrate_concentration",
            "concentration",
        ],
    )

    if chart_key == "qpcr_relative_expression_bar":
        value_col = _find_column_by_name(
            df, ["value", "relative_expression", "fold_change"]
        )
        gene_col = _find_column_by_name(df, ["gene", "target"])
        group_col = _find_column_by_name(
            df, ["group", "condition", "treatment", "series"]
        )
        if value_col:
            values = _to_numeric(df[value_col])
            valid = df.loc[values.notna()].copy()
            if not valid.empty:
                valid["__value"] = values.loc[values.notna()]
                preferred = [
                    c for c in [gene_col, group_col, value_col, "error", "n"] if c
                ]
                summary["highest_bar"] = _row_excerpt(
                    valid.loc[valid["__value"].idxmax()], preferred
                )
                summary["lowest_bar"] = _row_excerpt(
                    valid.loc[valid["__value"].idxmin()], preferred
                )

    elif chart_key == "qpcr_melt_curve_neg_derivative":
        tm_col = _find_column_by_name(df, ["tm", "Tm", "temperature"])
        peak_count_col = _find_column_by_name(df, ["peak_count"])
        peak_type_col = _find_column_by_name(df, ["peak_type"])
        if series_col and tm_col:
            tm_values: Dict[str, Any] = {}
            for name, group in df.groupby(series_col, dropna=True):
                nums = _to_numeric(group[tm_col]).dropna()
                if not nums.empty:
                    tm_values[str(name)] = float(nums.iloc[0])
            summary["tm_by_series"] = tm_values
        if peak_count_col:
            summary["peak_counts"] = sorted(
                set(int(x) for x in _to_numeric(df[peak_count_col]).dropna())
            )
        if peak_type_col:
            summary["peak_types"] = sorted(
                set(str(x) for x in df[peak_type_col].dropna())
            )

    elif chart_key == "plate_fret_time_curve":
        value_col = _find_column_by_name(df, ["value", "mp", "mP", "ratio"])
        time_col = _find_column_by_name(df, ["time", "time_s", "minute", "min"])
        if series_col and value_col and time_col:
            trends: Dict[str, Any] = {}
            for name, group in df.copy().groupby(series_col, dropna=True):
                group = (
                    group.assign(
                        __time=_to_numeric(group[time_col]),
                        __value=_to_numeric(group[value_col]),
                    )
                    .dropna(subset=["__time", "__value"])
                    .sort_values("__time")
                )
                if not group.empty:
                    start = float(group["__value"].iloc[0])
                    end = float(group["__value"].iloc[-1])
                    trends[str(name)] = {
                        "start": start,
                        "end": end,
                        "delta": end - start,
                    }
            summary["series_trends"] = trends

    elif chart_key == "qpcr_amplification_curve":
        ct_col = _find_column_by_name(df, ["ct", "cp", "cq"])
        threshold_col = _find_column_by_name(df, ["threshold"])
        if series_col and ct_col:
            ct_values: Dict[str, Any] = {}
            for name, group in df.groupby(series_col, dropna=True):
                nums = _to_numeric(group[ct_col]).dropna()
                if not nums.empty:
                    ct_values[str(name)] = float(nums.iloc[0])
            summary["ct_by_series"] = ct_values
        if threshold_col:
            vals = sorted(
                set(float(x) for x in _to_numeric(df[threshold_col]).dropna())
            )
            summary["threshold_values"] = vals[:10]

    elif chart_key == "plate_standard_curve":
        for col_name in ("slope", "intercept", "r_squared", "fit_value"):
            col = _find_column_by_name(df, [col_name])
            if col:
                vals = _to_numeric(df[col]).dropna()
                if not vals.empty:
                    summary[col_name] = {
                        "min": float(vals.min()),
                        "max": float(vals.max()),
                    }
        sample_type_col = _find_column_by_name(df, ["sample_type", "type"])
        if sample_type_col:
            summary["sample_type_counts"] = {
                str(k): int(v)
                for k, v in df[sample_type_col].astype(str).value_counts().items()
            }

    elif chart_key == "plate_michaelis_menten_curve":
        for col_name in ("km", "vmax", "fit_value"):
            col = _find_column_by_name(df, [col_name])
            if col:
                vals = _to_numeric(df[col]).dropna()
                if not vals.empty:
                    summary[col_name] = {
                        "min": float(vals.min()),
                        "max": float(vals.max()),
                    }

    elif chart_key == "plate_sample_concentration_bar":
        sample_col = _find_column_by_name(df, ["sample"])
        group_col = _find_column_by_name(df, ["group", "series"])
        value_col = _find_column_by_name(df, ["value", "concentration"])
        if value_col:
            values = _to_numeric(df[value_col])
            valid = df.loc[values.notna()].copy()
            if not valid.empty:
                valid["__value"] = values.loc[values.notna()]
                preferred = [
                    c
                    for c in [sample_col, group_col, value_col, "error", "n", "cv"]
                    if c
                ]
                summary["highest_bar"] = _row_excerpt(
                    valid.loc[valid["__value"].idxmax()], preferred
                )
                summary["lowest_bar"] = _row_excerpt(
                    valid.loc[valid["__value"].idxmin()], preferred
                )
                summary["n_bars"] = int(valid.shape[0])

    elif chart_key == "ce_dna_electropherogram":
        size_col = _find_column_by_name(df, ["peak_size_bp", "size_bp"])
        height_col = _find_column_by_name(df, ["peak_height", "intensity"])
        area_col = _find_column_by_name(df, ["peak_area"])
        if series_col and size_col:
            summary["peak_size_bp_by_series"] = {
                str(name): float(_to_numeric(group[size_col]).dropna().iloc[0])
                for name, group in df.groupby(series_col, dropna=True)
                if not _to_numeric(group[size_col]).dropna().empty
            }
        if series_col and height_col:
            summary["peak_height_by_series"] = {
                str(name): float(_to_numeric(group[height_col]).dropna().iloc[0])
                for name, group in df.groupby(series_col, dropna=True)
                if not _to_numeric(group[height_col]).dropna().empty
            }
        if area_col:
            vals = _to_numeric(df[area_col]).dropna()
            if not vals.empty:
                summary["peak_area_range"] = {
                    "min": float(vals.min()),
                    "max": float(vals.max()),
                }

    elif chart_key == "ce_rna_electropherogram":
        size_col = _find_column_by_name(df, ["peak_size_nt", "size_nt"])
        ratio_col = _find_column_by_name(df, ["ratio_28s_18s"])
        if series_col and size_col:
            summary["peak_size_nt_by_series"] = {
                str(name): float(_to_numeric(group[size_col]).dropna().iloc[0])
                for name, group in df.groupby(series_col, dropna=True)
                if not _to_numeric(group[size_col]).dropna().empty
            }
        if series_col and ratio_col:
            summary["ratio_28s_18s_by_series"] = {
                str(name): float(_to_numeric(group[ratio_col]).dropna().iloc[0])
                for name, group in df.groupby(series_col, dropna=True)
                if not _to_numeric(group[ratio_col]).dropna().empty
            }

    elif chart_key == "ce_virtual_gel":
        lane_col = _find_column_by_name(df, ["lane"])
        band_col = _find_column_by_name(df, ["main_band_size"])
        smear_col = _find_column_by_name(df, ["smear_index"])
        if lane_col and band_col:
            summary["main_band_size_by_lane"] = {
                str(name): float(_to_numeric(group[band_col]).dropna().iloc[0])
                for name, group in df.groupby(lane_col, dropna=True)
                if not _to_numeric(group[band_col]).dropna().empty
            }
        if lane_col and smear_col:
            summary["smear_index_by_lane"] = {
                str(name): float(_to_numeric(group[smear_col]).dropna().iloc[0])
                for name, group in df.groupby(lane_col, dropna=True)
                if not _to_numeric(group[smear_col]).dropna().empty
            }

    elif value_col:
        values = _to_numeric(df[value_col]).dropna()
        if not values.empty:
            summary["value_range"] = {
                "column": value_col,
                "min": float(values.min()),
                "max": float(values.max()),
            }
        if series_col and time_col:
            summary["series_column"] = series_col
            summary["x_column"] = time_col

    return summary


def _build_data_evidence(
    *,
    parsed_data_path: str,
    chart_key: str,
    results_for_chart: List[Dict[str, Any]],
    analysis_results: Dict[str, Any],
) -> Dict[str, Any]:
    if not parsed_data_path:
        return {
            "status": "failed",
            "reason": "parsed_data_path missing",
            "key_metrics": {},
            "script_result_summaries": _result_payloads(results_for_chart),
        }

    path = Path(parsed_data_path)
    if not path.exists():
        return {
            "status": "failed",
            "reason": f"parsed_data_path does not exist: {parsed_data_path}",
            "key_metrics": {},
            "script_result_summaries": _result_payloads(results_for_chart),
        }

    try:
        df = pd.read_csv(path, dtype=object, encoding="utf-8-sig")
        key_metrics = {
            "row_count": int(df.shape[0]),
            "column_count": int(df.shape[1]),
            "columns": [str(c) for c in df.columns],
            "numeric_column_stats": _numeric_column_stats(df),
            "categorical_preview": _categorical_preview(df),
            "chart_specific": _chart_specific_data_evidence(df, chart_key),
        }
        return {
            "status": "success",
            "parsed_data_path": parsed_data_path,
            "key_metrics": key_metrics,
            "script_result_summaries": _result_payloads(results_for_chart),
            "analysis_findings_count": len(
                (analysis_results or {}).get("findings") or []
            ),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": repr(exc),
            "key_metrics": {},
            "script_result_summaries": _result_payloads(results_for_chart),
            "analysis_findings_count": len(
                (analysis_results or {}).get("findings") or []
            ),
        }


def analyze_plots(
    data_profile: Dict[str, Any],
    visualizations: List[Dict[str, Any]],
    task_id: str,
    stable_requirement: Optional[Dict[str, Any]] = None,
    analysis_results: Optional[Dict[str, Any]] = None,
    chart_plan: Optional[List[Dict[str, Any]]] = None,
    artifact_root: str = "artifacts",
) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    code_paths: List[str] = []

    work_dir = str(
        task_artifact_dir(artifact_root, task_id, "plotting", _utc_now_tag())
    )
    code_dir = _ensure_dir(os.path.join(work_dir, "plot_analysis_code"))

    max_retry = 3
    backoff = [0.5, 1.0, 2.0]

    out_visualizations: List[Dict[str, Any]] = []

    for idx, viz in enumerate(visualizations or []):
        if not isinstance(viz, dict):
            continue

        chart_id = str(viz.get("chart_id") or f"chart_{idx + 1:02d}")
        chart_key = str(viz.get("chart_key") or "unknown")
        chart_note = str(viz.get("notes") or "")
        raw_image_paths = viz.get("image_paths") or []
        if isinstance(raw_image_paths, str):
            image_paths = [raw_image_paths]
        else:
            image_paths = list(raw_image_paths or [])
        parsed_data_path = str(viz.get("parsed_data_path") or "")

        if viz.get("status") != "success":
            viz = dict(viz)
            visual_observation = _build_visual_observation(
                chart_id=chart_id,
                chart_key=chart_key,
                image_paths=image_paths,
                chart_note=chart_note,
            )
            data_evidence = {
                "status": "skipped",
                "reason": "Visualization generation not successful.",
                "key_metrics": {},
                "script_result_summaries": [],
            }
            viz["plot_analysis"] = {
                "status": "skipped",
                "reason": "Visualization generation not successful.",
                "results": [],
                "scripts_meta": [],
                "visual_observation": visual_observation,
                "data_evidence": data_evidence,
            }
            out_visualizations.append(viz)
            continue

        if not parsed_data_path:
            issues.append(
                {
                    "code": "MISSING_VIZ_PARSED_PATH",
                    "severity": "high",
                    "message": f"{chart_id}: parsed_data_path missing for plot analysis.",
                }
            )
            viz = dict(viz)
            visual_observation = _build_visual_observation(
                chart_id=chart_id,
                chart_key=chart_key,
                image_paths=image_paths,
                chart_note=chart_note,
            )
            data_evidence = _build_data_evidence(
                parsed_data_path=parsed_data_path,
                chart_key=chart_key,
                results_for_chart=[],
                analysis_results=analysis_results or {},
            )
            viz["plot_analysis"] = {
                "status": "failed",
                "reason": "parsed_data_path missing",
                "results": [],
                "scripts_meta": [],
                "visual_observation": visual_observation,
                "data_evidence": data_evidence,
            }
            out_visualizations.append(viz)
            continue

        tool_resp = None
        ok_codegen = False
        all_result_items: List[Dict[str, Any]] = []

        for attempt in range(max_retry):
            logs.append(
                {
                    "phase": "plot_analysis_codegen_call",
                    "chart_id": chart_id,
                    "attempt": attempt + 1,
                    "max_retry": max_retry,
                }
            )

            tool_resp = generate_viz_analysis_python_code_tool.invoke(
                {
                    "chart_id": chart_id,
                    "chart_key": chart_key,
                    "requirement": chart_note,
                    "data_profile": data_profile,
                    "parsed_data_path": parsed_data_path,
                    "image_paths": image_paths,
                }
            )

            ok_codegen = (
                isinstance(tool_resp, dict)
                and isinstance(tool_resp.get("results"), list)
                and len(tool_resp["results"]) > 0
            )
            logs.append(
                {
                    "phase": "plot_analysis_codegen_result",
                    "chart_id": chart_id,
                    "ok": ok_codegen,
                    "results_count": len(tool_resp["results"]) if ok_codegen else 0,
                }
            )

            if ok_codegen:
                for r in tool_resp.get("results") or []:
                    if isinstance(r, dict):
                        all_result_items.append(r)
                break

            if attempt < max_retry - 1:
                time.sleep(backoff[attempt])

        if not ok_codegen:
            issues.append(
                {
                    "code": "PLOT_ANALYSIS_CODEGEN_FAILED",
                    "severity": "high",
                    "message": f"{chart_id}: chart-level analysis code generation failed.",
                }
            )
            viz = dict(viz)
            visual_observation = _build_visual_observation(
                chart_id=chart_id,
                chart_key=chart_key,
                image_paths=image_paths,
                chart_note=chart_note,
            )
            data_evidence = _build_data_evidence(
                parsed_data_path=parsed_data_path,
                chart_key=chart_key,
                results_for_chart=[],
                analysis_results=analysis_results or {},
            )
            viz["plot_analysis"] = {
                "status": "failed",
                "reason": "plot-analysis codegen failed",
                "results": [],
                "scripts_meta": [],
                "visual_observation": visual_observation,
                "data_evidence": data_evidence,
            }
            out_visualizations.append(viz)
            continue

        any_success = False
        results_for_chart: List[Dict[str, Any]] = []
        scripts_meta_for_chart: List[Dict[str, Any]] = []

        for k, item in enumerate(all_result_items):
            if not isinstance(item, dict):
                continue

            py_code = str(item.get("python_code") or "")
            purpose = str(
                item.get("analysis_purpose")
                or chart_note
                or f"analysis for {chart_key}"
            )
            script_id = str(
                item.get("result_id") or f"{chart_id}__analysis_{k + 1:02d}"
            )
            code_path = os.path.join(code_dir, f"{script_id}.py")

            with open(code_path, "w", encoding="utf-8") as f:
                f.write(py_code)

            code_paths.append(code_path)

            logs.append(
                {
                    "phase": "plot_analysis_exec_start",
                    "chart_id": chart_id,
                    "script_id": script_id,
                    "code_path": code_path,
                }
            )

            exec_out = _exec_python_code(code_path=code_path, timeout=120)
            parsed_stdout = (
                _extract_json(exec_out["stdout"]) if exec_out["ok"] else None
            )

            results_for_chart.append(
                {
                    "analysis_purpose": purpose,
                    "analysis_result": parsed_stdout
                    if parsed_stdout is not None
                    else (exec_out["stdout"] or None),
                    "status": "success" if exec_out["ok"] else "failed",
                    "script_id": script_id,
                    "stdout": exec_out["stdout"],
                    "stderr": exec_out["stderr"],
                }
            )

            scripts_meta_for_chart.append(
                {
                    "script_id": script_id,
                    "code_path": code_path,
                    "status": "success" if exec_out["ok"] else "failed",
                }
            )

            logs.append(
                {
                    "phase": "plot_analysis_exec_end",
                    "chart_id": chart_id,
                    "script_id": script_id,
                    "ok": exec_out["ok"],
                    "stderr_preview": exec_out["stderr"][:300],
                }
            )

            if exec_out["ok"]:
                any_success = True

        viz = dict(viz)
        visual_observation = _build_visual_observation(
            chart_id=chart_id,
            chart_key=chart_key,
            image_paths=image_paths,
            chart_note=chart_note,
        )
        data_evidence = _build_data_evidence(
            parsed_data_path=parsed_data_path,
            chart_key=chart_key,
            results_for_chart=results_for_chart,
            analysis_results=analysis_results or {},
        )
        viz["plot_analysis"] = {
            "status": "success" if any_success else "failed",
            "results": results_for_chart,
            "scripts_meta": scripts_meta_for_chart,
            "summary": "",
            "visual_observation": visual_observation,
            "data_evidence": data_evidence,
        }
        out_visualizations.append(viz)

    return {
        "visualizations": out_visualizations,
        "visualizations_code": code_paths,
        "visualization_log": logs,
        "issues": issues,
        "fatal": False,
    }
