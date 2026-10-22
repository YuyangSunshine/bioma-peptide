"""Callable AlphaFold Database operation extracted from the Skill examples."""

from __future__ import annotations

import json

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class AlphaFoldQueryInput(ToolInput):
    uniprot_id: str = Field(description="Canonical UniProt accession")


@bioma_tool(
    name="bioclaw_query_alphafold",
    description="Retrieve AlphaFold Database prediction metadata and official structure URLs.",
    category="database",
    author="BioClaw",
    args_schema=AlphaFoldQueryInput,
)
def query_alphafold(uniprot_id: str) -> str:
    accession = uniprot_id.strip().upper()
    response = requests.get(
        f"https://alphafold.ebi.ac.uk/api/prediction/{accession}", timeout=30
    )
    response.raise_for_status()
    return json.dumps(response.json(), ensure_ascii=False)
