"""
Web search tools: Google/SerpAPI search and web page fetching.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import requests_tool_error


class WebSearchInput(ToolInput):
    query: str = Field(description="Search query")
    max_results: Optional[int] = Field(default=5, description="Number of results to return")


@bioma_tool(
    name="web_search",
    description="Search the web using SerpAPI (Google) or DuckDuckGo for scientific information.",
    category="web",
    version="1.0.0",
    author="STELLA Team",
    args_schema=WebSearchInput,
    requires_network=True,
)
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for information."""
    serpapi_key = os.getenv("SERPAPI_KEY")

    if serpapi_key:
        try:
            resp = requests.get(
                "https://serpapi.com/search",
                params={"q": query, "api_key": serpapi_key, "num": max_results, "engine": "google"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
                for r in data.get("organic_results", [])[:max_results]
            ]
            return json.dumps(results, indent=2, ensure_ascii=False)
        except requests.RequestException as exc:
            raise requests_tool_error(exc, backend="SerpAPI") from exc

    # Fallback: DuckDuckGo Instant Answer API
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_redirect": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        abstract = data.get("AbstractText", "")
        related = [{"topic": t.get("Text", ""), "url": t.get("FirstURL", "")}
                   for t in data.get("RelatedTopics", [])[:max_results] if isinstance(t, dict)]
        return json.dumps({"abstract": abstract, "related": related}, indent=2, ensure_ascii=False)
    except requests.RequestException as exc:
        raise requests_tool_error(exc, backend="DuckDuckGo") from exc


class WebFetchInput(ToolInput):
    url: str = Field(description="URL to fetch content from")
    max_chars: Optional[int] = Field(default=3000, description="Maximum characters of content to return")


@bioma_tool(
    name="fetch_web_page",
    description="Fetch and return the text content of a web page.",
    category="web",
    version="1.0.0",
    author="STELLA Team",
    args_schema=WebFetchInput,
    requires_network=True,
)
def fetch_web_page(url: str, max_chars: int = 3000) -> str:
    """Fetch web page content."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "BioAgent/1.0 (+https://github.com/bioagent)"},
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.text

        # Strip HTML tags
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text[:max_chars] + (f"\n[... truncated, full page {len(text)} chars]"
                                   if len(text) > max_chars else "")
    except requests.RequestException as exc:
        from urllib.parse import urlsplit

        backend = urlsplit(url).hostname or "web page"
        raise requests_tool_error(exc, backend=backend) from exc
