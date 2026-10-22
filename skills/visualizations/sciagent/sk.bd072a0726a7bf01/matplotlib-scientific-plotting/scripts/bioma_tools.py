"""Generate focused publication-ready plots from local tabular data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class MatplotlibInput(ToolInput):
    data_path: str = Field(description="CSV or TSV input table")
    output_path: str = Field(description="PNG, PDF, or SVG output file")
    plot_type: Literal["line", "scatter", "bar", "heatmap"] = Field(description="Plot type")
    x_column: str = Field(default="", description="X-axis column; optional for heatmaps")
    y_columns: list[str] = Field(default_factory=list, description="Y or heatmap numeric columns")
    title: str = Field(default="", description="Plot title")
    x_label: str = Field(default="", description="X-axis label")
    y_label: str = Field(default="", description="Y-axis label")
    width: float = Field(default=7.0, gt=1, le=30, description="Figure width in inches")
    height: float = Field(default=5.0, gt=1, le=30, description="Figure height in inches")
    dpi: int = Field(default=300, ge=72, le=1200, description="Raster output DPI")


@bioma_tool(
    name="sciagent_plot_matplotlib",
    description="Create line, scatter, bar, or heatmap figures from a CSV/TSV table.",
    category="visualization",
    author="SciAgent",
    args_schema=MatplotlibInput,
)
def plot_matplotlib(
    data_path: str,
    output_path: str,
    plot_type: str,
    x_column: str = "",
    y_columns: list[str] | None = None,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    width: float = 7.0,
    height: float = 5.0,
    dpi: int = 300,
) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    source = Path(data_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() not in {".png", ".pdf", ".svg"}:
        raise ValueError("output_path must end in .png, .pdf, or .svg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    separator = "\t" if source.suffix.lower() in {".tsv", ".txt"} else ","
    frame = pd.read_csv(source, sep=separator)
    columns = y_columns or []
    if not columns:
        columns = [column for column in frame.select_dtypes("number").columns if column != x_column]
    missing = [column for column in ([x_column] if x_column else []) + columns if column not in frame]
    if missing:
        raise KeyError(f"Missing columns: {missing}")
    if not columns:
        raise ValueError("No numeric y_columns were selected")
    if plot_type != "heatmap" and not x_column:
        raise ValueError("x_column is required for line, scatter, and bar plots")
    fig, ax = plt.subplots(figsize=(width, height), constrained_layout=True)
    if plot_type == "heatmap":
        values = frame[columns].apply(pd.to_numeric, errors="coerce").to_numpy().T
        image = ax.imshow(values, aspect="auto", interpolation="nearest", cmap="viridis")
        ax.set_yticks(range(len(columns)), labels=columns)
        if x_column:
            labels = frame[x_column].astype(str).tolist()
            step = max(1, len(labels) // 20)
            ticks = list(range(0, len(labels), step))
            ax.set_xticks(ticks, labels=[labels[index] for index in ticks], rotation=45, ha="right")
        fig.colorbar(image, ax=ax, label=y_label or "Value")
    else:
        x_values = frame[x_column]
        for column in columns:
            y_values = pd.to_numeric(frame[column], errors="coerce")
            if plot_type == "line":
                ax.plot(x_values, y_values, marker="o", linewidth=1.5, label=column)
            elif plot_type == "scatter":
                ax.scatter(x_values, y_values, s=24, alpha=0.8, label=column)
            else:
                if len(columns) != 1:
                    raise ValueError("bar plots currently require exactly one y column")
                ax.bar(x_values.astype(str), y_values, label=column)
        if len(columns) > 1:
            ax.legend(frameon=False)
    ax.set_title(title)
    ax.set_xlabel(x_label or x_column)
    if plot_type != "heatmap":
        ax.set_ylabel(y_label or (columns[0] if len(columns) == 1 else "Value"))
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(destination, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return json.dumps(
        {"output": str(destination), "plot_type": plot_type, "rows": len(frame),
         "columns": columns, "bytes": destination.stat().st_size},
        ensure_ascii=False,
    )
