"""Callable UniProt operations extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class UniProtQueryInput(ToolInput):
    operation: Literal["search", "entry", "fasta"] = Field(description="UniProt operation")
    query: str = Field(description="Gene/protein search term or UniProt accession")
    organism_id: int = Field(default=9606, description="NCBI taxonomy ID used for search")
    max_results: int = Field(default=5, ge=1, le=25, description="Maximum search results")


@bioma_tool(
    name="bioclaw_query_uniprot",
    description="Search UniProt or retrieve an entry/FASTA sequence through the UniProt REST API.",
    category="database",
    author="BioClaw",
    args_schema=UniProtQueryInput,
)
def query_uniprot(operation: str, query: str, organism_id: int = 9606, max_results: int = 5) -> str:
    base = "https://rest.uniprot.org/uniprotkb"
    if operation == "search":
        response = requests.get(
            f"{base}/search",
            params={
                "query": f"gene_exact:{query} AND organism_id:{organism_id} AND reviewed:true",
                "format": "json",
                "size": max_results,
                "fields": "accession,id,gene_names,protein_name,organism_name,length,cc_function,ft_domain,sequence",
            },
            timeout=30,
        )
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
    accession = query.strip().upper()
    suffix = "json" if operation == "entry" else "fasta"
    response = requests.get(f"{base}/{accession}.{suffix}", timeout=30)
    response.raise_for_status()
    if operation == "entry":
        return json.dumps(response.json(), ensure_ascii=False)
    sequence = "".join(
        line.strip() for line in response.text.splitlines() if not line.startswith(">")
    )
    return json.dumps({"accession": accession, "sequence": sequence, "length": len(sequence)})
