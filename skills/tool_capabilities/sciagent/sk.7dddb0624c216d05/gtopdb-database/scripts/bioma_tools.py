"""Callable Guide to Pharmacology query extracted from the Skill examples."""

from __future__ import annotations

import json
import re
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class GtoPdbInput(ToolInput):
    operation: Literal["target", "target_interactions", "target_xrefs", "ligand", "ligand_structure"] = Field(description="GtoPdb lookup type")
    query: str = Field(description="Gene symbol, UniProt accession, target ID, or ligand ID")


@bioma_tool(
    name="sciagent_query_gtopdb",
    description="Resolve GtoPdb targets or retrieve target interactions/xrefs and ligand metadata/structures.",
    category="database",
    author="SciAgent",
    args_schema=GtoPdbInput,
)
def query_gtopdb(operation: str, query: str) -> str:
    base = "https://www.guidetopharmacology.org/services"
    value = query.strip()
    if operation == "target":
        is_uniprot = bool(
            re.fullmatch(r"(?:[A-NR-Z][0-9][A-Z0-9]{3}[0-9]|[A-Z0-9]{10})", value.upper())
        )
        field = "accession" if is_uniprot else "geneSymbol"
        url, params = f"{base}/targets", {field: value}
    elif operation.startswith("target_"):
        suffix = "interactions" if operation.endswith("interactions") else "databaseLinks"
        url, params = f"{base}/targets/{value}/{suffix}", None
    else:
        suffix = "/structure" if operation == "ligand_structure" else ""
        url, params = f"{base}/ligands/{value}{suffix}", None
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    return json.dumps(response.json(), ensure_ascii=False)
