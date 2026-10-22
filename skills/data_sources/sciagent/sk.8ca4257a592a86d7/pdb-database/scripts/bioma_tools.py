"""Callable RCSB PDB operation extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class PdbDatabaseInput(ToolInput):
    operation: Literal["search", "entry", "file"] = Field(description="RCSB PDB operation")
    query: str = Field(description="Full-text query or PDB ID")
    file_format: Literal["pdb", "cif"] = Field(default="cif", description="Coordinate file format")
    max_results: int = Field(default=10, ge=1, le=100, description="Maximum search results")


@bioma_tool(
    name="sciagent_query_pdb",
    description="Search RCSB PDB, retrieve entry metadata, or return coordinate file content.",
    category="database",
    author="SciAgent",
    args_schema=PdbDatabaseInput,
)
def query_pdb_database(
    operation: str,
    query: str,
    file_format: str = "cif",
    max_results: int = 10,
) -> str:
    pdb_id = query.strip().upper()
    if operation == "entry":
        response = requests.get(f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id}", timeout=30)
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
    if operation == "file":
        response = requests.get(f"https://files.rcsb.org/download/{pdb_id}.{file_format}", timeout=30)
        response.raise_for_status()
        return json.dumps({"pdb_id": pdb_id, "format": file_format, "content": response.text})
    payload = {
        "query": {"type": "terminal", "service": "full_text", "parameters": {"value": query}},
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": max_results}},
    }
    response = requests.post("https://search.rcsb.org/rcsbsearch/v2/query", json=payload, timeout=30)
    response.raise_for_status()
    return json.dumps(response.json(), ensure_ascii=False)
