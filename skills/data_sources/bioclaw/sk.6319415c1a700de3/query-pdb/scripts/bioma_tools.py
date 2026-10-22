"""Callable RCSB PDB operations extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class PdbQueryInput(ToolInput):
    operation: Literal["search", "entry"] = Field(description="RCSB PDB operation")
    query: str = Field(description="Full-text query or four-character PDB ID")
    max_results: int = Field(default=5, ge=1, le=50, description="Maximum search results")


@bioma_tool(
    name="bioclaw_query_pdb",
    description="Search RCSB PDB or retrieve structured metadata for a PDB entry.",
    category="database",
    author="BioClaw",
    args_schema=PdbQueryInput,
)
def query_pdb(operation: str, query: str, max_results: int = 5) -> str:
    if operation == "entry":
        response = requests.get(
            f"https://data.rcsb.org/rest/v1/core/entry/{query.strip().upper()}", timeout=30
        )
    else:
        payload = {
            "query": {"type": "terminal", "service": "full_text", "parameters": {"value": query}},
            "return_type": "entry",
            "request_options": {"paginate": {"start": 0, "rows": max_results}},
        }
        response = requests.post(
            "https://search.rcsb.org/rcsbsearch/v2/query", json=payload, timeout=30
        )
    response.raise_for_status()
    return json.dumps(response.json(), ensure_ascii=False)
