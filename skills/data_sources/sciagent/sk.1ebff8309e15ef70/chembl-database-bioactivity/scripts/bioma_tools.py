"""Callable ChEMBL REST query extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class ChEMBLInput(ToolInput):
    resource: Literal["molecule", "target", "activity", "drug", "mechanism"] = Field(description="ChEMBL resource")
    query: str = Field(description="ChEMBL ID or preferred-name search text")
    max_results: int = Field(default=20, ge=1, le=100, description="Maximum search results")


@bioma_tool(
    name="sciagent_query_chembl",
    description="Retrieve a ChEMBL record by ID or search molecules, targets, activities, drugs, and mechanisms.",
    category="database",
    author="SciAgent",
    args_schema=ChEMBLInput,
)
def query_chembl(resource: str, query: str, max_results: int = 20) -> str:
    base = "https://www.ebi.ac.uk/chembl/api/data"
    value = query.strip()
    if value.upper().startswith("CHEMBL") and resource in {"molecule", "target", "drug"}:
        url, params = f"{base}/{resource}/{value.upper()}.json", None
    else:
        url = f"{base}/{resource}.json"
        field = {
            "molecule": "pref_name__icontains",
            "target": "pref_name__icontains",
            "activity": "target_chembl_id",
            "drug": "pref_name__icontains",
            "mechanism": "molecule_chembl_id",
        }[resource]
        params = {field: value, "limit": max_results}
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return json.dumps(response.json(), ensure_ascii=False)
