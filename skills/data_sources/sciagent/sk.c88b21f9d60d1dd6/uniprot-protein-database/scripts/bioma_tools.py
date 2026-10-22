"""Callable UniProt query extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class UniProtDatabaseInput(ToolInput):
    operation: Literal["search", "entry", "fasta"] = Field(description="UniProt lookup type")
    query: str = Field(description="UniProt query expression or accession")
    max_results: int = Field(default=20, ge=1, le=100, description="Maximum search results")


@bioma_tool(
    name="sciagent_query_uniprot_database",
    description="Search UniProtKB or retrieve a protein entry/FASTA sequence by accession.",
    category="database",
    author="SciAgent",
    args_schema=UniProtDatabaseInput,
)
def query_uniprot_database(operation: str, query: str, max_results: int = 20) -> str:
    base = "https://rest.uniprot.org/uniprotkb"
    value = query.strip()
    if operation == "search":
        response = requests.get(
            f"{base}/search",
            params={"query": value, "format": "json", "size": max_results},
            timeout=30,
        )
    else:
        suffix = "json" if operation == "entry" else "fasta"
        response = requests.get(f"{base}/{value.upper()}.{suffix}", timeout=30)
    response.raise_for_status()
    if operation == "fasta":
        sequence = "".join(line for line in response.text.splitlines() if not line.startswith(">"))
        return json.dumps({"accession": value.upper(), "sequence": sequence, "length": len(sequence)})
    return json.dumps(response.json(), ensure_ascii=False)
