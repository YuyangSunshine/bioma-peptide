"""Callable AlphaFold DB lookup extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class AlphaFoldDatabaseInput(ToolInput):
    uniprot_id: str = Field(description="UniProt accession, for example P00520")
    output: Literal["metadata", "file_urls"] = Field(default="metadata", description="Result detail")


@bioma_tool(
    name="sciagent_query_alphafold_database",
    description="Retrieve AlphaFold DB prediction metadata and current structure/confidence file URLs by UniProt accession.",
    category="database",
    author="SciAgent",
    args_schema=AlphaFoldDatabaseInput,
)
def query_alphafold_database(uniprot_id: str, output: str = "metadata") -> str:
    accession = uniprot_id.strip().upper()
    response = requests.get(
        f"https://alphafold.ebi.ac.uk/api/prediction/{accession}", timeout=30
    )
    response.raise_for_status()
    predictions = response.json()
    if output == "metadata":
        return json.dumps(predictions, ensure_ascii=False)
    url_fields = ("cifUrl", "bcifUrl", "pdbUrl", "paeDocUrl", "paeImageUrl")
    result = [
        {
            "entryId": prediction.get("entryId"),
            **{key: prediction.get(key) for key in url_fields if prediction.get(key)},
        }
        for prediction in predictions
    ]
    return json.dumps(result, ensure_ascii=False)
