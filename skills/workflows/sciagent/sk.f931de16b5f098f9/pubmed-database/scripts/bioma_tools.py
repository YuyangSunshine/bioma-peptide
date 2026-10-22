"""Callable PubMed E-utilities query extracted from the Skill examples."""

from __future__ import annotations

import json
import os
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class PubMedInput(ToolInput):
    operation: Literal["search", "summary", "abstract"] = Field(description="PubMed lookup type")
    query: str = Field(description="PubMed search expression or comma-separated PMIDs")
    max_results: int = Field(default=20, ge=1, le=100, description="Maximum search results")


@bioma_tool(
    name="sciagent_query_pubmed",
    description="Search PubMed or retrieve article summaries and abstract text through NCBI E-utilities.",
    category="literature",
    author="SciAgent",
    args_schema=PubMedInput,
)
def query_pubmed(operation: str, query: str, max_results: int = 20) -> str:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    params = {"db": "pubmed", "email": os.getenv("NCBI_EMAIL", "")}
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    if operation == "search":
        url = f"{base}/esearch.fcgi"
        params.update({"term": query.strip(), "retmax": max_results, "retmode": "json"})
    elif operation == "summary":
        url = f"{base}/esummary.fcgi"
        params.update({"id": query.strip(), "retmode": "json"})
    else:
        url = f"{base}/efetch.fcgi"
        params.update({"id": query.strip(), "rettype": "abstract", "retmode": "text"})
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    if operation == "abstract":
        return json.dumps({"pmids": query, "abstracts": response.text}, ensure_ascii=False)
    return json.dumps(response.json(), ensure_ascii=False)
