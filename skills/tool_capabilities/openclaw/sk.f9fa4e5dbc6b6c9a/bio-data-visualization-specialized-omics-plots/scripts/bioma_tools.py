"""BioAgent tool for a deterministic MA plot from real DE results."""
from pathlib import Path
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory
from skills._meta.support.openclaw._bioma_module import json_text
class MaPlotInput(ToolInput):
    table_path: str
    output_dir: str
    base_mean_column: str = "baseMean"
    fold_change_column: str = "log2FoldChange"
    adjusted_p_column: str = "padj"
    fdr_threshold: float = Field(default=0.05, gt=0, le=1)
@bioma_tool(name="openclaw_plot_ma", description="Create an MA plot from explicit base-mean, log2-fold-change, and adjusted-p-value columns.", category="omics-visualization", author="OpenClaw", args_schema=MaPlotInput)
def plot_ma(table_path: str, output_dir: str, base_mean_column: str = "baseMean", fold_change_column: str = "log2FoldChange", adjusted_p_column: str = "padj", fdr_threshold: float = 0.05) -> str:
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    source=existing_file(table_path,"table_path"); output=output_directory(output_dir)
    df=pd.read_csv(source,sep="\t" if source.suffix.lower() in {".tsv",".txt"} else ",")
    required={base_mean_column,fold_change_column,adjusted_p_column}
    if missing:=required-set(df.columns): raise ValueError(f"missing columns: {sorted(missing)}")
    data=df.dropna(subset=list(required)).copy(); significant=data[adjusted_p_column]<fdr_threshold
    fig,ax=plt.subplots(figsize=(8,6)); ax.scatter(np.log10(data[base_mean_column].clip(lower=np.finfo(float).tiny)),data[fold_change_column],c=np.where(significant,"#D1495B","#A6A6A6"),s=12,alpha=.7,linewidths=0); ax.axhline(0,color="black",lw=.8); ax.set_xlabel("log10 mean expression"); ax.set_ylabel("log2 fold change"); fig.tight_layout()
    target=output/"ma_plot.png"; fig.savefig(target,dpi=300); plt.close(fig)
    return json_text({"rows":len(data),"significant":int(significant.sum()),"output_file":str(target)})
