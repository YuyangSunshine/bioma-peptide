"""AnnData inspection helpers extracted from the Skill examples."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class InspectAnnDataInput(ToolInput):
    input_path: str = Field(description="Input .h5ad file")
    backed: bool = Field(default=True, description="Read lazily without loading X")
    category_limit: int = Field(default=20, ge=1, le=100, description="Maximum values per categorical obs column")


@bioma_tool(
    name="sciagent_inspect_anndata",
    description="Inspect an H5AD file's shape, metadata, layers, embeddings, graphs, and categorical observation counts.",
    category="single-cell",
    author="SciAgent",
    args_schema=InspectAnnDataInput,
)
def inspect_anndata(input_path: str, backed: bool = True, category_limit: int = 20) -> str:
    try:
        import anndata as ad
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install anndata") from exc
    path = Path(input_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    adata = ad.read_h5ad(path, backed="r" if backed else None)
    try:
        category_counts: dict[str, dict[str, int]] = {}
        for column in adata.obs.columns:
            series = adata.obs[column]
            if str(series.dtype) == "category" or series.nunique(dropna=False) <= category_limit:
                counts = series.astype(str).value_counts(dropna=False).head(category_limit)
                category_counts[str(column)] = {str(key): int(value) for key, value in counts.items()}
        result = {
            "path": str(path),
            "shape": [int(adata.n_obs), int(adata.n_vars)],
            "is_backed": bool(adata.isbacked),
            "obs_columns": list(map(str, adata.obs.columns)),
            "var_columns": list(map(str, adata.var.columns)),
            "layers": list(map(str, adata.layers.keys())),
            "obsm": {str(k): list(map(int, v.shape)) for k, v in adata.obsm.items()},
            "varm": {str(k): list(map(int, v.shape)) for k, v in adata.varm.items()},
            "obsp": {str(k): list(map(int, v.shape)) for k, v in adata.obsp.items()},
            "uns_keys": list(map(str, adata.uns.keys())),
            "category_counts": category_counts,
        }
        return json.dumps(result, ensure_ascii=False)
    finally:
        if getattr(adata, "file", None) is not None:
            adata.file.close()
