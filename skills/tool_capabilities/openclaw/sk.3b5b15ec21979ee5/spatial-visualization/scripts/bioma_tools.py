"""BioAgent tool for coordinate-based spatial feature plots."""
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory
from skills._meta.support.openclaw._bioma_module import json_text
class SpatialPlotInput(ToolInput):
    input_path: str
    output_dir: str
    features: list[str] = Field(min_length=1, max_length=12)
    point_size: float = Field(default=8, gt=0, le=500)
@bioma_tool(name="openclaw_plot_spatial_features", description="Plot bounded gene-expression or observation features over explicit spatial coordinates without modifying the dataset.", category="spatial-omics", author="OpenClaw", args_schema=SpatialPlotInput)
def plot_spatial_features(input_path: str, output_dir: str, features: list[str], point_size: float = 8) -> str:
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scanpy as sc
    from scipy.sparse import issparse
    adata=sc.read_h5ad(existing_file(input_path,"input_path")); coords=adata.obsm.get("spatial")
    if coords is None or coords.shape[1]<2: raise ValueError("input has no two-dimensional spatial coordinates")
    output=output_directory(output_dir); files=[]
    for feature in features:
        category_labels = None
        if feature in adata.obs:
            values=adata.obs[feature]
            if not pd.api.types.is_numeric_dtype(values):
                categorical=pd.Categorical(values); values=categorical.codes; category_labels=list(categorical.categories)
        elif feature in adata.var_names:
            column=adata[:,feature].X; values=np.asarray(column.toarray() if issparse(column) else column).ravel()
        else: raise ValueError(f"feature not found: {feature}")
        fig,ax=plt.subplots(figsize=(7,6)); scatter=ax.scatter(coords[:,0],coords[:,1],c=values,s=point_size,cmap="viridis"); ax.invert_yaxis(); ax.set_title(feature); ax.set_aspect("equal"); colorbar=fig.colorbar(scatter,ax=ax)
        if category_labels:
            colorbar.set_ticks(range(len(category_labels))); colorbar.set_ticklabels([str(item) for item in category_labels])
        fig.tight_layout(); target=output/f"{feature.replace('/','_')}.png"; fig.savefig(target,dpi=300); plt.close(fig); files.append(str(target))
    return json_text({"spots":adata.n_obs,"files":files})
