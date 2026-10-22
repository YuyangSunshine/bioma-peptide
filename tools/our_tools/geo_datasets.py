"""Bounded GEO dataset discovery and official file-manifest inspection."""
from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator

from bioma.tools.base import ToolInput, bioma_tool

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_GEO = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
_ACCESSION = re.compile(r"^GSE[1-9][0-9]*$", re.IGNORECASE)


class GEOSearchInput(ToolInput):
    query: str = Field(min_length=2, max_length=500, description="Scientific GEO search terms, not an accession URL.")
    organism: str | None = Field(default=None, min_length=2, max_length=120)
    start_year: int | None = Field(default=None, ge=2000, le=2100)
    end_year: int | None = Field(default=None, ge=2000, le=2100)
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query", "organism")
    @classmethod
    def clean_text(cls, value):
        if value is None:
            return value
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Search text cannot be blank.")
        return cleaned


class GEOInspectInput(ToolInput):
    accession: str = Field(description="Exact GEO Series accession such as GSE206785.")

    @field_validator("accession")
    @classmethod
    def normalize_accession(cls, value):
        accession = value.strip().upper()
        if not _ACCESSION.fullmatch(accession):
            raise ValueError("accession must be an exact GEO Series identifier such as GSE206785")
        return accession


def _timeout():
    from bioma.runtime.deadline import check, remaining

    check()
    left = remaining()
    return min(45, left) if left is not None else 45


def _get_json(path, params):
    import requests

    response = requests.get(
        f"{_EUTILS}/{path}", params={**params, "tool": "BioMA"}, timeout=_timeout()
    )
    response.raise_for_status()
    return response.json()


def _search_term(query, organism, start_year, end_year):
    terms = [f"({query})"]
    if organism:
        terms.append(f'"{organism}"[Organism]')
    if start_year or end_year:
        lower = start_year or 2000
        upper = end_year or 2100
        if lower > upper:
            raise ValueError("start_year cannot be later than end_year")
        terms.append(f'"{lower}/01/01"[PDAT] : "{upper}/12/31"[PDAT]')
    return " AND ".join(terms)


def _summary_records(ids):
    if not ids:
        return []
    payload = _get_json("esummary.fcgi", {
        "db": "gds", "id": ",".join(ids), "retmode": "json",
    })
    result = payload.get("result") or {}
    records = []
    for uid in result.get("uids") or ids:
        row = result.get(str(uid)) or {}
        accession = str(row.get("accession") or "").upper()
        if not _ACCESSION.fullmatch(accession):
            continue
        sample_count = row.get("n_samples")
        try:
            sample_count = int(sample_count) if sample_count not in {None, ""} else None
        except (TypeError, ValueError):
            sample_count = None
        records.append({
            "accession": accession,
            "title": row.get("title") or "",
            "summary": row.get("summary") or "",
            "organism": row.get("taxon") or "",
            "study_type": row.get("gdsType") or row.get("entryType") or "",
            "sample_count": sample_count,
            "publication_date": row.get("PDAT") or row.get("pdat") or "",
            "repository_url": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",
            "ncbi_uid": str(uid),
        })
    return records


def _search(query, organism=None, start_year=None, end_year=None, limit=5):
    term = _search_term(query, organism, start_year, end_year)
    payload = _get_json("esearch.fcgi", {
        "db": "gds", "term": term, "retmode": "json",
        "retmax": limit, "sort": "pub date",
    })
    ids = list((payload.get("esearchresult") or {}).get("idlist") or [])
    return term, _summary_records(ids[:limit])


def _https_resource(url):
    parts = urlsplit(url.strip())
    if parts.scheme not in {"ftp", "https"} or not (
        (parts.hostname or "").lower() == "ftp.ncbi.nlm.nih.gov"
        or (parts.hostname or "").lower().endswith(".ncbi.nlm.nih.gov")
    ):
        raise ValueError("GEO returned a supplementary URL outside official NCBI hosts")
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def validate_search(result, arguments):
    try:
        return (
            result["status"] == "ok"
            and result["query"] == arguments["query"]
            and len(result["candidates"]) <= arguments.get("limit", 5)
            and all(_ACCESSION.fullmatch(row["accession"]) for row in result["candidates"])
        )
    except (KeyError, TypeError):
        return False


def validate_inspection(result, arguments):
    try:
        accession = arguments["accession"].strip().upper()
        return (
            result["status"] == "ok"
            and result["dataset"]["accession"] == accession
            and all(item["url"].startswith("https://") for item in result["resources"])
        )
    except (KeyError, TypeError):
        return False


@bioma_tool(
    name="search_geo_datasets",
    category="database",
    author="BioMA",
    description="Search the official NCBI GEO DataSets index for GEO Series candidates. Returns bounded comparable metadata and exact accessions; it does not select a dataset or download files.",
    args_schema=GEOSearchInput,
    requires_network=True,
    result_kind="retrieved_fact",
    result_validator=validate_search,
    replay_safe=True,
    tags=["GEO", "dataset-discovery", "transcriptomics", "single-cell"],
)
def search_geo_datasets(query: str, organism: str | None = None,
                        start_year: int | None = None, end_year: int | None = None,
                        limit: int = 5):
    term, candidates = _search(query, organism, start_year, end_year, limit)
    return {
        "status": "ok",
        "query": query,
        "resolved_ncbi_term": term,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "qualification": "Repository metadata only; candidate relevance and scientific applicability remain to be assessed.",
    }


@bioma_tool(
    name="inspect_geo_dataset",
    category="database",
    author="BioMA",
    description="Inspect one exact GEO Series accession through official NCBI metadata and return its supplementary-resource manifest. Use read_public_resource separately for an explicitly selected file URL.",
    args_schema=GEOInspectInput,
    requires_network=True,
    result_kind="retrieved_fact",
    result_validator=validate_inspection,
    replay_safe=True,
    tags=["GEO", "dataset-manifest", "transcriptomics", "public-data"],
)
def inspect_geo_dataset(accession: str):
    import requests

    accession = accession.strip().upper()
    _, records = _search(f'{accession}[ACCN]', None, None, None, 5)
    dataset = next((record for record in records if record["accession"] == accession), None)
    if dataset is None:
        raise ValueError(f"NCBI GEO did not return an exact Series record for {accession}")
    response = requests.get(_GEO, params={
        "acc": accession, "targ": "self", "form": "text", "view": "quick",
    }, timeout=_timeout())
    response.raise_for_status()
    resources = []
    seen = set()
    for line in response.text.splitlines():
        if not line.startswith("!Series_supplementary_file") or " = " not in line:
            continue
        url = _https_resource(line.split(" = ", 1)[1])
        if url in seen:
            continue
        seen.add(url)
        resources.append({
            "name": urlsplit(url).path.rsplit("/", 1)[-1] or accession,
            "role": "supplementary_file_unclassified",
            "url": url,
            "size_bytes": None,
        })
    quick_url = _GEO + "?" + urlencode({
        "acc": accession, "targ": "self", "form": "text", "view": "quick",
    })
    return {
        "status": "ok",
        "dataset": dataset,
        "resources": resources,
        "resource_count": len(resources),
        "metadata_url": quick_url,
        "qualification": "Official repository metadata and unclassified file links only; matrix, metadata, sample grouping, and applicability must be verified before analysis.",
        "next_step": "Compare candidate datasets, select an exact resource for a stated role, then call read_public_resource with its URL and an explicit byte bound.",
    }
