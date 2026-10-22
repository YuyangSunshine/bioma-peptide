"""BioAgent tool for plotting real differential-expression results."""
from pathlib import Path
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory
from skills._meta.support.openclaw._bioma_module import json_text
class VolcanoInput(ToolInput):
    table_path: str
    output_dir: str
    gene_column: str = "gene"
    fold_change_column: str = "log2FoldChange"
    p_value_column: str = "padj"
    fold_change_threshold: float = Field(default=1, ge=0)
    p_value_threshold: float = Field(default=0.05, gt=0, le=1)
    maximum_labels: int = Field(default=15, ge=0, le=100)
@bioma_tool(name="openclaw_plot_volcano", description="Create a publication-ready volcano plot from an explicit differential-expression table.", category="omics-visualization", author="OpenClaw", args_schema=VolcanoInput)
def plot_volcano(table_path: str, output_dir: str, gene_column: str = "gene", fold_change_column: str = "log2FoldChange", p_value_column: str = "padj", fold_change_threshold: float = 1, p_value_threshold: float = 0.05, maximum_labels: int = 15) -> str:
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    source = existing_file(table_path, "table_path"); output = output_directory(output_dir)
    df = pd.read_csv(source, sep="\t" if source.suffix.lower() in {".tsv", ".txt"} else ",")
    required = {gene_column, fold_change_column, p_value_column}
    if missing := required - set(df.columns): raise ValueError(f"missing columns: {sorted(missing)}")
    data = df.dropna(subset=list(required)).copy(); data[p_value_column] = data[p_value_column].clip(lower=np.finfo(float).tiny)
    up = (data[p_value_column] < p_value_threshold) & (data[fold_change_column] > fold_change_threshold)
    down = (data[p_value_column] < p_value_threshold) & (data[fold_change_column] < -fold_change_threshold)
    colors = np.where(up, "#D1495B", np.where(down, "#2878A5", "#A6A6A6"))
    fig, ax = plt.subplots(figsize=(8, 6)); ax.scatter(data[fold_change_column], -np.log10(data[p_value_column]), c=colors, s=14, alpha=.7, linewidths=0)
    ax.axvline(fold_change_threshold, color="black", ls="--", lw=.8); ax.axvline(-fold_change_threshold, color="black", ls="--", lw=.8); ax.axhline(-np.log10(p_value_threshold), color="black", ls="--", lw=.8)
    for _, row in data[up | down].nsmallest(maximum_labels, p_value_column).iterrows(): ax.annotate(str(row[gene_column]), (row[fold_change_column], -np.log10(row[p_value_column])), fontsize=7)
    ax.set_xlabel("log2 fold change"); ax.set_ylabel("-log10 adjusted p-value"); fig.tight_layout()
    target = output / "volcano.png"; fig.savefig(target, dpi=300); plt.close(fig)
    return json_text({"rows": len(data), "upregulated": int(up.sum()), "downregulated": int(down.sum()), "output_file": str(target)})
