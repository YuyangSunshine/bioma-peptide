from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(tempfile.gettempdir()) / "bioma-matplotlib"),
)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


_REPRESENTATIVE_WELLS = ["A1", "A3", "A5", "A7", "A9", "A11"]


def _read(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=object, encoding="utf-8-sig")


def _style_axes(ax: plt.Axes) -> None:
    ax.grid(True, alpha=0.25, linewidth=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _plot_all_wells_derivative(peaks_path: str, output_dir: Path) -> dict[str, Any]:
    df = _read(peaks_path)
    df["temperature"] = pd.to_numeric(df["temperature"], errors="coerce")
    df["negative_derivative"] = pd.to_numeric(
        df["negative_derivative"], errors="coerce"
    )
    df = df.dropna(subset=["well", "temperature", "negative_derivative"])

    fig, ax = plt.subplots(figsize=(11, 6.5))
    for _, sub in df.sort_values(["well", "temperature"]).groupby("well"):
        role = str(sub.get("role", pd.Series([""])).iloc[0] or "")
        color = "#9aa0a6"
        alpha = 0.28
        if role == "positive_control":
            color = "#2f6fb0"
            alpha = 0.65
        elif role == "blank":
            color = "#c44e52"
            alpha = 0.75
        ax.plot(
            sub["temperature"],
            sub["negative_derivative"],
            color=color,
            alpha=alpha,
            linewidth=0.9,
        )
    ax.set_title("All-Well Melting Peak QC")
    ax.set_xlabel("Temperature (C)")
    ax.set_ylabel("-dF/dT")
    _style_axes(ax)
    path = output_dir / "lightcycler_all_wells_derivative_qc.png"
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return {
        "chart_id": "lightcycler_all_wells_derivative_qc",
        "path": str(path),
        "data_path": peaks_path,
        "rows": int(df.shape[0]),
    }


def _plot_tm_violin_swarm(tm_path: str, output_dir: Path) -> dict[str, Any]:
    df = _read(tm_path)
    df["Tm"] = pd.to_numeric(df["Tm"], errors="coerce")
    if "qc_pass" in df.columns:
        df = df[
            df["qc_pass"].astype(str).str.lower().isin({"true", "1", "yes", "passed"})
        ]
    if "role" in df.columns:
        df = df[df["role"].astype(str) != "blank"]
    df = df.dropna(subset=["group", "Tm"])
    groups = [
        g
        for g in ["Top1", "Top2", "Top3", "Top4", "Top5", "Positive_Control"]
        if g in set(df["group"])
    ]
    if not groups:
        groups = sorted(str(g) for g in df["group"].dropna().unique())
    data = [
        df[df["group"].astype(str) == group]["Tm"].astype(float).tolist()
        for group in groups
    ]

    fig, ax = plt.subplots(figsize=(9.5, 5.8))
    parts = ax.violinplot(
        data,
        positions=list(range(1, len(groups) + 1)),
        showmeans=True,
        showextrema=False,
    )
    for body in parts["bodies"]:
        body.set_facecolor("#9ecae1")
        body.set_edgecolor("#4c78a8")
        body.set_alpha(0.42)
    if "cmeans" in parts:
        parts["cmeans"].set_color("#1f4e79")

    for idx, values in enumerate(data, start=1):
        if not values:
            continue
        offsets = [((i % 7) - 3) * 0.025 for i in range(len(values))]
        ax.scatter(
            [idx + off for off in offsets],
            values,
            s=26,
            alpha=0.8,
            color="#333333",
            linewidth=0,
        )
    ax.axhline(
        90.7, color="#c44e52", linestyle="--", linewidth=1.1
    )
    ax.set_title("Tm Distribution by Group")
    ax.set_xlabel("Group")
    ax.set_ylabel("Tm (C)")
    ax.set_xticks(list(range(1, len(groups) + 1)))
    ax.set_xticklabels(groups, rotation=20, ha="right")
    ax.legend(frameon=False, loc="best")
    _style_axes(ax)
    path = output_dir / "lightcycler_tm_violin_swarm.png"
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return {
        "chart_id": "lightcycler_tm_violin_swarm",
        "path": str(path),
        "data_path": tm_path,
        "groups": groups,
    }


def _plot_representative_normalized_curves(
    curves_path: str, output_dir: Path
) -> dict[str, Any]:
    df = _read(curves_path)
    df["temperature"] = pd.to_numeric(df["temperature"], errors="coerce")
    df["normalized_fluorescence"] = pd.to_numeric(
        df["normalized_fluorescence"], errors="coerce"
    )
    df = df.dropna(subset=["well", "temperature", "normalized_fluorescence"])
    wells = [well for well in _REPRESENTATIVE_WELLS if well in set(df["well"])]
    if not wells:
        wells = sorted(str(w) for w in df["well"].dropna().unique())[:6]

    fig, ax = plt.subplots(figsize=(10, 6))
    for well in wells:
        sub = df[df["well"] == well].sort_values("temperature")
        group = (
            str(sub["group"].iloc[0])
            if "group" in sub.columns and not sub.empty
            else ""
        )
        label = f"{well} ({group})" if group else well
        ax.plot(
            sub["temperature"],
            sub["normalized_fluorescence"],
            linewidth=1.8,
            label=label,
        )
    ax.set_title("Representative Normalized Melting Curves")
    ax.set_xlabel("Temperature (C)")
    ax.set_ylabel("Normalized fluorescence")
    ax.legend(frameon=False, loc="best")
    _style_axes(ax)
    path = output_dir / "lightcycler_representative_normalized_curves.png"
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return {
        "chart_id": "lightcycler_representative_normalized_curves",
        "path": str(path),
        "data_path": curves_path,
        "wells": wells,
    }


def generate_lightcycler_fixed_plots(
    *,
    deterministic_artifacts: dict[str, Any],
    output_dir: str,
) -> dict[str, Any]:
    artifacts = (
        deterministic_artifacts.get("lightcycler_melt") or deterministic_artifacts
    )
    tm_path = str(artifacts.get("qc_summary") or artifacts.get("tm_per_well") or "")
    normalized_path = str(artifacts.get("normalized_curves") or "")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    # all-well derivative needs the enriched peaks table, not just Tm summary.
    source_peaks_path = str(artifacts.get("peaks_source") or "")
    if source_peaks_path and Path(source_peaks_path).exists():
        try:
            results.append(_plot_all_wells_derivative(source_peaks_path, out_dir))
        except Exception as exc:
            issues.append(
                {
                    "code": "LIGHTCYCLER_FIXED_PLOT_FAILED",
                    "severity": "medium",
                    "message": f"all wells derivative: {exc!r}",
                }
            )

    if tm_path and Path(tm_path).exists():
        try:
            results.append(_plot_tm_violin_swarm(tm_path, out_dir))
        except Exception as exc:
            issues.append(
                {
                    "code": "LIGHTCYCLER_FIXED_PLOT_FAILED",
                    "severity": "medium",
                    "message": f"tm violin swarm: {exc!r}",
                }
            )

    if normalized_path and Path(normalized_path).exists():
        try:
            results.append(
                _plot_representative_normalized_curves(normalized_path, out_dir)
            )
        except Exception as exc:
            issues.append(
                {
                    "code": "LIGHTCYCLER_FIXED_PLOT_FAILED",
                    "severity": "medium",
                    "message": f"normalized curves: {exc!r}",
                }
            )

    return {
        "status": "success" if results else "skipped",
        "plots": results,
        "issues": issues,
    }


__all__ = ["generate_lightcycler_fixed_plots"]
