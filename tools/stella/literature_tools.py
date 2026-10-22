"""
Literature search tools: PubMed, Semantic Scholar, paper summarization.
"""

from __future__ import annotations

import json
from typing import Optional

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import requests_tool_error


class PubMedInput(ToolInput):
    query: str = Field(description="PubMed search query with optional Boolean operators")
    max_results: Optional[int] = Field(default=10, description="Maximum number of results")
    years: Optional[int] = Field(default=5, description="Filter to papers from last N years")


@bioma_tool(
    name="search_pubmed",
    description="Search PubMed for scientific literature. Returns titles, abstracts, and PMIDs.",
    category="literature",
    version="1.0.0",
    author="STELLA Team",
    args_schema=PubMedInput,
    requires_network=True,
)
def search_pubmed(query: str, max_results: int = 10, years: int = 5) -> str:
    """Search NCBI PubMed using E-utilities."""
    import datetime

    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    current_year = datetime.datetime.now().year
    date_filter = f"{current_year - years}/01/01:{current_year}/12/31[dp]"
    full_query = f"({query}) AND {date_filter}"

    try:
        # Search
        search_resp = requests.get(
            f"{base_url}/esearch.fcgi",
            params={"db": "pubmed", "term": full_query, "retmax": max_results,
                    "retmode": "json", "tool": "bioagent", "email": "bioagent@lab.edu"},
            timeout=15,
        )
        search_resp.raise_for_status()
        ids = search_resp.json()["esearchresult"]["idlist"]

        if not ids:
            return f"No PubMed results found for: {query}"

        # Fetch summaries
        fetch_resp = requests.get(
            f"{base_url}/efetch.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "rettype": "abstract",
                    "retmode": "text", "tool": "bioagent", "email": "bioagent@lab.edu"},
            timeout=20,
        )
        fetch_resp.raise_for_status()
        return (
            f"Found {len(ids)} papers (showing abstracts):\n\n"
            + fetch_resp.text[:3000]
            + (f"\n[... {len(ids)} total results]" if len(ids) > 3 else "")
        )
    except requests.RequestException as exc:
        raise requests_tool_error(exc, backend="PubMed") from exc


class SemScholarInput(ToolInput):
    query: str = Field(description="Semantic Scholar search query")
    max_results: Optional[int] = Field(default=10, description="Maximum number of results")


@bioma_tool(
    name="search_semantic_scholar",
    description="Search Semantic Scholar for papers. Returns titles, authors, year, and citation counts.",
    category="literature",
    version="1.0.0",
    author="STELLA Team",
    args_schema=SemScholarInput,
    requires_network=True,
)
def search_semantic_scholar(query: str, max_results: int = 10) -> str:
    """Search Semantic Scholar API."""
    try:
        resp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": query,
                "limit": max_results,
                "fields": "title,authors,year,citationCount,abstract,externalIds",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        papers = []
        for paper in data.get("data", []):
            papers.append({
                "title": paper.get("title", ""),
                "authors": [a["name"] for a in paper.get("authors", [])[:3]],
                "year": paper.get("year"),
                "citations": paper.get("citationCount", 0),
                "doi": paper.get("externalIds", {}).get("DOI", ""),
                "abstract_preview": (paper.get("abstract") or "")[:200],
            })

        return json.dumps({"query": query, "total": data.get("total", 0), "papers": papers},
                         indent=2, ensure_ascii=False)
    except Exception as exc:
        return f"Semantic Scholar error: {exc}"


class AbstractInput(ToolInput):
    pmid: str = Field(description="PubMed ID (PMID)")


@bioma_tool(
    name="fetch_paper_abstract",
    description="Fetch the abstract of a PubMed paper by its PMID.",
    category="literature",
    version="1.0.0",
    author="STELLA Team",
    args_schema=AbstractInput,
    requires_network=True,
)
def fetch_paper_abstract(pmid: str) -> str:
    """Fetch abstract text for a specific PubMed paper."""
    try:
        resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pubmed", "id": pmid, "rettype": "abstract",
                    "retmode": "text", "tool": "bioagent"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.text.strip()[:2000]
    except requests.RequestException as exc:
        raise requests_tool_error(exc, backend="PubMed") from exc


class SummarizePapersInput(ToolInput):
    papers_json: str = Field(description="JSON string of a paper list returned by an available literature search tool")
    focus: Optional[str] = Field(default=None, description="Specific aspect to focus the summary on")


@bioma_tool(
    name="summarize_papers",
    description="Synthesize a list of papers into a structured research summary.",
    category="literature",
    version="1.0.0",
    author="STELLA Team",
    args_schema=SummarizePapersInput,
)
def summarize_papers(papers_json: str, focus: str | None = None) -> str:
    """Create a narrative summary from a list of papers."""
    try:
        data = json.loads(papers_json) if isinstance(papers_json, str) else papers_json
        papers = data.get("papers", data) if isinstance(data, dict) else data
    except (json.JSONDecodeError, TypeError):
        papers = [{"title": papers_json}]

    if not papers:
        return "No papers provided for summarization."

    focus_str = f" (focusing on {focus})" if focus else ""
    lines = [f"Research Summary{focus_str} — {len(papers)} papers\n"]
    lines.append("=" * 50)

    for i, p in enumerate(papers[:10], 1):
        title = p.get("title", "Unknown title")
        year = p.get("year", "")
        authors = p.get("authors", [])
        author_str = ", ".join(authors[:2]) + (" et al." if len(authors) > 2 else "")
        abstract = p.get("abstract_preview", "")
        lines.append(f"\n{i}. {title}")
        if author_str or year:
            lines.append(f"   {author_str} ({year})")
        if abstract:
            lines.append(f"   {abstract[:200]}...")

    return "\n".join(lines)


class KeyFindingsInput(ToolInput):
    text: str = Field(description="Abstract or paper text to extract findings from")


@bioma_tool(
    name="extract_key_findings",
    description="Extract key findings, methods, and conclusions from a paper abstract or text.",
    category="literature",
    version="1.0.0",
    author="STELLA Team",
    args_schema=KeyFindingsInput,
)
def extract_key_findings(text: str) -> str:
    """Parse abstract sections into structured findings."""
    sections = {"Background": "", "Methods": "", "Results": "", "Conclusions": ""}

    import re
    for section in sections:
        pattern = re.compile(
            rf"(?:{section}|{section.upper()})[:\.]\s*(.*?)(?=Background|BACKGROUND|Methods|METHODS|Results|RESULTS|Conclusions|CONCLUSIONS|$)",
            re.DOTALL | re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            sections[section] = m.group(1).strip()[:300]

    # If no structured abstract, return full text
    if all(v == "" for v in sections.values()):
        return f"Unstructured abstract:\n{text[:500]}"

    result = "\n".join(f"**{k}**: {v}" for k, v in sections.items() if v)
    return result
