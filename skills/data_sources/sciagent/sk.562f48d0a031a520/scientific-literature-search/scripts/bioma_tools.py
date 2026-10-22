"""Callable literature searches extracted from the Skill examples."""

from __future__ import annotations

import json
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class ScientificLiteratureInput(ToolInput):
    source: Literal["crossref", "arxiv", "europe_pmc"] = Field(
        description="Literature source"
    )
    query: str = Field(description="Search expression")
    cursor_mark: str = Field(
        default="*",
        description="Europe PMC page cursor; use nextCursorMark from the preceding response.",
    )
    max_results: int = Field(default=10, ge=1, le=50, description="Maximum results")


@bioma_tool(
    name="sciagent_search_scientific_literature",
    description="Search Crossref, arXiv, or Europe PMC literature with a reproducible query.",
    category="literature",
    author="SciAgent",
    args_schema=ScientificLiteratureInput,
)
def search_scientific_literature(
    source: str, query: str, max_results: int = 10, cursor_mark: str = "*"
) -> str:
    if source == "europe_pmc":
        response = requests.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={
                "query": query.strip(),
                "format": "json",
                "resultType": "core",
                "pageSize": max_results,
                "cursorMark": cursor_mark,
            },
            timeout=30,
        )
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
    if source == "crossref":
        response = requests.get(
            "https://api.crossref.org/works",
            params={
                "query": query.strip(),
                "rows": max_results,
                "select": "DOI,title,author,published,container-title,URL,type",
            },
            timeout=30,
        )
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
    response = requests.get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{query.strip()}",
            "start": 0,
            "max_results": max_results,
        },
        timeout=30,
    )
    response.raise_for_status()
    return json.dumps({"source": "arxiv", "atom": response.text}, ensure_ascii=False)
