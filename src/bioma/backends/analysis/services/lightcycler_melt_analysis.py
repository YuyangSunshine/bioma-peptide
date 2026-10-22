from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

import pandas as pd


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _table_map(dataset_bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for table in dataset_bundle.get("tables") or []:
        tid = str(table.get("table_id") or "")
        if tid:
            out[tid] = table
    return out


def _read_table(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=object, encoding="utf-8-sig")


def _find_table_with_columns(
    dataset_bundle: dict[str, Any], required: set[str]
) -> Optional[dict[str, Any]]:
    for table in dataset_bundle.get("tables") or []:
        cols = {_norm(col.get("name")) for col in table.get("columns") or []}
        if required <= cols:
            return table
    return None


def _table_path(table: dict[str, Any]) -> str:
    return str(table.get("path") or "")


def _positive_control_baseline(tm_df: pd.DataFrame) -> Optional[float]:
    role = tm_df.get("role")
    if role is not None:
        control = tm_df[tm_df["role"].astype(str).str.lower().str.contains("positive")]
        if not control.empty:
            return float(pd.to_numeric(control["Tm"], errors="coerce").dropna().mean())
    group = tm_df.get("group")
    if group is not None:
        control = tm_df[
            tm_df["group"]
            .astype(str)
            .str.lower()
            .isin({"positive_control", "positive control", "r6", "baseline"})
        ]
        if not control.empty:
            return float(pd.to_numeric(control["Tm"], errors="coerce").dropna().mean())
    return None


def _extract_tm_per_well(peaks: pd.DataFrame) -> pd.DataFrame:
    required = {"well", "temperature", "negative_derivative"}
    missing = required - set(peaks.columns)
    if missing:
        raise ValueError(f"peaks table missing columns: {sorted(missing)}")

    work = peaks.copy()
    work["temperature"] = pd.to_numeric(work["temperature"], errors="coerce")
    work["negative_derivative"] = pd.to_numeric(
        work["negative_derivative"], errors="coerce"
    )
    work = work.dropna(subset=["well", "temperature", "negative_derivative"])
    idx = work.groupby("well")["negative_derivative"].idxmax()
    tm = work.loc[idx].copy().sort_values("well")
    tm = tm.rename(columns={"temperature": "Tm", "negative_derivative": "peak_height"})
    keep = ["well", "sample", "group", "role", "replicate_id", "Tm", "peak_height"]
    for col in keep:
        if col not in tm.columns:
            tm[col] = ""
    return tm[keep].reset_index(drop=True)


def _qc_tm(tm: pd.DataFrame) -> pd.DataFrame:
    out = tm.copy()
    out["Tm"] = pd.to_numeric(out["Tm"], errors="coerce")
    out["peak_height"] = pd.to_numeric(out["peak_height"], errors="coerce")
    max_peak = (
        float(out["peak_height"].max())
        if out["peak_height"].notna().any()
        else math.nan
    )
    low_peak_threshold = max_peak * 0.05 if math.isfinite(max_peak) else math.nan

    reasons: list[str] = []
    for _, row in out.iterrows():
        row_reasons: list[str] = []
        tm = row.get("Tm")
        peak = row.get("peak_height")
        role = str(row.get("role") or "").lower()
        if pd.isna(tm) or float(tm) < 50 or float(tm) > 105:
            row_reasons.append("tm_out_of_expected_range")
        if math.isfinite(low_peak_threshold) and (
            pd.isna(peak) or float(peak) < low_peak_threshold
        ):
            row_reasons.append("low_peak_height")
        if (
            role == "blank"
            and math.isfinite(low_peak_threshold)
            and not pd.isna(peak)
            and float(peak) >= low_peak_threshold
        ):
            row_reasons.append("blank_signal_leakage")
        reasons.append(";".join(row_reasons) if row_reasons else "passed")

    out["qc_fail_reason"] = reasons
    out["qc_pass"] = out["qc_fail_reason"].eq("passed")

    # Flag, but do not automatically fail, within-group IQR outliers. This keeps
    # the fixed analysis conservative while surfacing wells like H3 in Top2.
    out["within_group_outlier"] = False
    for group, sub in out[out["qc_pass"]].groupby("group", dropna=False):
        values = pd.to_numeric(sub["Tm"], errors="coerce").dropna()
        if len(values) < 4:
            continue
        q1 = float(values.quantile(0.25))
        q3 = float(values.quantile(0.75))
        iqr = q3 - q1
        if iqr <= 0:
            median = float(values.median())
            mask = (out["group"].eq(group)) & (out["Tm"].sub(median).abs() > 0.5)
        else:
            mask = (out["group"].eq(group)) & (
                (out["Tm"] < q1 - 1.5 * iqr) | (out["Tm"] > q3 + 1.5 * iqr)
            )
        out.loc[mask, "within_group_outlier"] = True

    return out


def _group_stats(qc: pd.DataFrame) -> pd.DataFrame:
    passed = qc[qc["qc_pass"] & (qc["role"].astype(str) != "blank")].copy()
    passed["Tm"] = pd.to_numeric(passed["Tm"], errors="coerce")
    rows: list[dict[str, Any]] = []
    baseline = _positive_control_baseline(passed)
    for group, sub in passed.groupby("group", dropna=False):
        values = pd.to_numeric(sub["Tm"], errors="coerce").dropna()
        if values.empty:
            continue
        mean = float(values.mean())
        sd = float(values.std(ddof=1)) if len(values) > 1 else 0.0
        rows.append(
            {
                "group": group,
                "role": str(
                    sub["role"].iloc[0] if "role" in sub.columns and len(sub) else ""
                ),
                "n": int(len(values)),
                "mean_Tm": mean,
                "sd_Tm": sd,
                "sem_Tm": sd / math.sqrt(len(values)) if len(values) else 0.0,
                "delta_vs_positive_control": None
                if baseline is None
                else mean - baseline,
            }
        )
    return pd.DataFrame(rows, columns=["group", "role", "n", "mean_Tm", "sd_Tm",
        "sem_Tm", "delta_vs_positive_control"]).sort_values(["role", "group"]).reset_index(drop=True)


def _normalized_curves(curves: pd.DataFrame) -> pd.DataFrame:
    required = {"well", "temperature", "fluorescence"}
    missing = required - set(curves.columns)
    if missing:
        raise ValueError(f"curves table missing columns: {sorted(missing)}")
    work = curves.copy()
    work["temperature"] = pd.to_numeric(work["temperature"], errors="coerce")
    work["fluorescence"] = pd.to_numeric(work["fluorescence"], errors="coerce")
    work = work.dropna(subset=["well", "temperature", "fluorescence"])
    mins = work.groupby("well")["fluorescence"].transform("min")
    maxs = work.groupby("well")["fluorescence"].transform("max")
    denom = (maxs - mins).replace(0, pd.NA)
    work["normalized_fluorescence"] = (work["fluorescence"] - mins) / denom
    cols = [
        "well",
        "sample",
        "group",
        "role",
        "replicate_id",
        "temperature",
        "fluorescence",
        "normalized_fluorescence",
    ]
    for col in cols:
        if col not in work.columns:
            work[col] = ""
    return work[cols].reset_index(drop=True)


def run_lightcycler_melt_analysis(
    *,
    dataset_bundle: dict[str, Any],
    outputs_dir: str,
) -> dict[str, Any]:
    peaks_table = _find_table_with_columns(
        dataset_bundle, {"well", "temperature", "negative_derivative"}
    )
    curves_table = _find_table_with_columns(
        dataset_bundle, {"well", "temperature", "fluorescence"}
    )
    if not peaks_table:
        return {"status": "skipped", "reason": "No LightCycler peaks table found."}

    out_dir = Path(outputs_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    peaks = _read_table(_table_path(peaks_table))
    tm = _extract_tm_per_well(peaks)
    qc = _qc_tm(tm)
    stats = _group_stats(qc)

    tm_path = out_dir / "lightcycler_tm_per_well.csv"
    qc_path = out_dir / "lightcycler_qc_summary.csv"
    failed_path = out_dir / "lightcycler_qc_failed.csv"
    stats_path = out_dir / "lightcycler_group_tm_statistics.csv"

    tm.to_csv(tm_path, index=False, encoding="utf-8-sig")
    qc.to_csv(qc_path, index=False, encoding="utf-8-sig")
    qc[~qc["qc_pass"]].to_csv(failed_path, index=False, encoding="utf-8-sig")
    stats.to_csv(stats_path, index=False, encoding="utf-8-sig")

    output_files = [str(tm_path), str(qc_path), str(failed_path), str(stats_path)]
    normalized_path = None
    if curves_table:
        curves = _read_table(_table_path(curves_table))
        normalized = _normalized_curves(curves)
        normalized_path = out_dir / "lightcycler_normalized_curves.csv"
        normalized.to_csv(normalized_path, index=False, encoding="utf-8-sig")
        output_files.append(str(normalized_path))

    return {
        "status": "success" if not stats.empty else "failed",
        "reason": "" if not stats.empty else "No non-blank group has valid QC-passing Tm observations.",
        "analysis_id": "lightcycler_melt_analysis",
        "output_files": output_files,
        "artifacts": {
            "peaks_source": _table_path(peaks_table),
            "curves_source": _table_path(curves_table) if curves_table else "",
            "tm_per_well": str(tm_path),
            "qc_summary": str(qc_path),
            "qc_failed": str(failed_path),
            "group_tm_statistics": str(stats_path),
            "normalized_curves": str(normalized_path) if normalized_path else "",
        },
        "summary": {
            "n_wells": int(qc["well"].nunique()),
            "n_qc_pass": int(qc["qc_pass"].sum()),
            "n_qc_failed": int((~qc["qc_pass"]).sum()),
            "n_within_group_outliers": int(qc["within_group_outlier"].sum()),
            "groups": stats.to_dict(orient="records"),
        },
    }


__all__ = ["run_lightcycler_melt_analysis"]
