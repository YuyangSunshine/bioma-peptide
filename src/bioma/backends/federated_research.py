"""Shared read-only biomedical source connectors and ResearchSourceClient adapter."""
from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

from bioma.core.scientific_capabilities import ResearchBundle, RetrievedClaim, Source


@dataclass(frozen=True)
class ConnectorPolicy:
    timeout_seconds: float = 20.0
    retries: int = 2
    max_content_bytes: int = 2_000_000
    user_agent: str = "BioMA/1.0 (scientific source retrieval)"


class ReadOnlyHttpCache:
    """Content-addressed successful-response cache; never fabricates offline hits."""
    def __init__(self, root: Path | str | None):
        self.root = Path(root).resolve() if root is not None else None

    @staticmethod
    def key(url: str, params: dict[str, Any] | None) -> str:
        return hashlib.sha256(json.dumps([url, params or {}], sort_keys=True,
            separators=(",", ":"), default=str).encode()).hexdigest()

    def read(self, url: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        if self.root is None:
            return None
        path = self.root / (self.key(url, params) + ".json")
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("url") != url or payload.get("params") != (params or {}):
            raise ValueError("Source cache identity mismatch")
        return payload

    def write(self, url: str, params: dict[str, Any] | None, *, content: bytes,
              headers: dict[str, Any], retrieved_at: str) -> None:
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"url": url, "params": params or {}, "retrieved_at": retrieved_at,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "content_hex": content.hex(), "headers": headers}
        path = self.root / (self.key(url, params) + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


class BiomedicalHttpClient:
    def __init__(self, *, cache_dir: Path | str | None = None,
                 policy: ConnectorPolicy = ConnectorPolicy(), session=None):
        self.cache = ReadOnlyHttpCache(cache_dir)
        self.policy = policy
        self.session = session or requests.Session()

    def get(self, url: str, *, params: dict[str, Any] | None = None,
            accept: str = "application/json") -> tuple[bytes, dict[str, Any]]:
        cached = self.cache.read(url, params)
        if cached is not None:
            content = bytes.fromhex(cached["content_hex"])
            if hashlib.sha256(content).hexdigest() != cached["content_sha256"]:
                raise ValueError("Cached source content hash mismatch")
            audit = {key: value for key, value in cached.items() if key != "content_hex"}
            return content, {**audit, "cache_hit": True}
        error = None
        for attempt in range(self.policy.retries + 1):
            try:
                response = self.session.get(url, params=params,
                    headers={"Accept": accept, "User-Agent": self.policy.user_agent},
                    timeout=self.policy.timeout_seconds)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                content = response.content
                if len(content) > self.policy.max_content_bytes:
                    raise ValueError("Source response exceeds configured content limit")
                retrieved_at = datetime.now(timezone.utc).isoformat()
                headers = {key.lower(): value for key, value in response.headers.items()
                           if key.lower() in {"content-type", "etag", "last-modified", "x-api-version"}}
                self.cache.write(url, params, content=content, headers=headers,
                                 retrieved_at=retrieved_at)
                return content, {"url": response.url, "params": params or {},
                    "retrieved_at": retrieved_at,
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "headers": headers, "cache_hit": False}
            except (requests.RequestException, ValueError) as exc:
                error = exc
                if attempt >= self.policy.retries or isinstance(exc, ValueError):
                    raise
                time.sleep(min(0.25 * (2 ** attempt), 1.0))
        raise error  # pragma: no cover

    def json(self, url: str, *, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        content, provenance = self.get(url, params=params)
        return json.loads(content), provenance


class EuropePmcConnector:
    name = "europe_pmc"
    SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def __init__(self, client: BiomedicalHttpClient):
        self.client = client

    def search(self, query: str, *, limit: int = 10, cursor: str = "*") -> list[Source]:
        payload, request = self.client.json(self.SEARCH, params={"query": query,
            "format": "json", "resultType": "core", "pageSize": limit,
            "cursorMark": cursor})
        rows = payload.get("resultList", {}).get("result", [])
        return [self._source(row, request) for row in rows]

    def _source(self, row: dict[str, Any], request: dict[str, Any]) -> Source:
        doi = str(row.get("doi") or "").lower()
        pmcid = str(row.get("pmcid") or "").upper()
        pmid = str(row.get("pmid") or (row.get("id") if row.get("source") == "MED" else ""))
        identifier = "doi:" + doi if doi else ("pmcid:" + pmcid if pmcid else "pmid:" + pmid)
        title = str(row.get("title") or "Untitled record")
        abstract = str(row.get("abstractText") or "")
        citation = "https://doi.org/" + doi if doi else "https://europepmc.org/article/{}/{}".format(
            row.get("source") or "MED", row.get("id") or pmcid or pmid)
        license_value = row.get("license") or ("open_access_unspecified" if row.get("isOpenAccess") == "Y" else "metadata_only")
        source_id = "source:" + hashlib.sha256(identifier.encode()).hexdigest()
        return Source(id=source_id, identifier=identifier, citation=citation,
            spans={key: value for key, value in {"title": title, "abstract": abstract}.items() if value},
            provenance={"connector": self.name, "source_type": "journal_article_or_preprint",
                "retrieval_status": "retrieved", "retrieved_at": request["retrieved_at"],
                "source_version": row.get("firstPublicationDate") or row.get("journalInfo", {}).get("printPublicationDate"),
                "content_sha256": hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest(),
                "access_scope": "public_metadata", "license": license_value, "pmid": pmid or None, "pmcid": pmcid or None,
                "record_source": row.get("source"), "record_id": row.get("id"),
                "doi": doi or None, "is_open_access": row.get("isOpenAccess") == "Y",
                "request": request})

    def fetch(self, source: Source) -> Source:
        pmcid = source.provenance.get("pmcid")
        if not pmcid or not source.provenance.get("is_open_access"):
            return source
        url = f"{self.BASE}/{quote(str(pmcid))}/fullTextXML"
        content, request = self.client.get(url, accept="application/xml")
        root = ET.fromstring(content)
        paragraphs = [" ".join("".join(node.itertext()).split()) for node in root.findall(".//body//p")]
        text = "\n".join(item for item in paragraphs if item)
        if not text:
            return source
        return source.model_copy(update={"spans": {**source.spans, "full_text": text},
            "provenance": {**source.provenance, "full_text": {"retrieval_status": "retrieved",
                "format": "JATS XML", "content_sha256": request["content_sha256"],
                "retrieved_at": request["retrieved_at"], "access_scope": "pmc_open_access",
                "license": source.provenance.get("license"), "request": request}}})

    def expand_citations(self, source: Source, *, limit: int = 10) -> list[Source]:
        record_source = source.provenance.get("record_source")
        record_id = source.provenance.get("record_id")
        if not record_source or not record_id:
            pmid = source.provenance.get("pmid")
            pmcid = source.provenance.get("pmcid")
            record_source, record_id = ("MED", pmid) if pmid else ("PMC", pmcid)
        if not record_id:
            return []
        # Europe PMC references requires BOTH the source namespace and record ID.
        payload, request = self.client.json(f"{self.BASE}/{quote(str(record_source), safe='')}/{quote(str(record_id), safe='')}/references",
            params={"format": "json", "pageSize": limit})
        return [self._source(row, request) for row in payload.get("referenceList", {}).get("reference", [])[:limit]]


class PubMedConnector:
    name = "pubmed"
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, client: BiomedicalHttpClient, *, email: str = ""):
        self.client, self.email = client, email

    def search(self, query: str, *, limit: int = 10) -> list[Source]:
        payload, search_request = self.client.json(self.BASE + "/esearch.fcgi", params={
            "db": "pubmed", "term": query, "retmax": limit, "retmode": "json",
            "tool": "bioma", "email": self.email})
        ids = payload.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        summaries, request = self.client.json(self.BASE + "/esummary.fcgi", params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "json",
            "tool": "bioma", "email": self.email})
        rows = []
        for pmid in ids:
            item = summaries.get("result", {}).get(pmid, {})
            title = str(item.get("title") or "Untitled PubMed record")
            identifier = "pmid:" + pmid
            rows.append(Source(id="source:" + hashlib.sha256(identifier.encode()).hexdigest(),
                identifier=identifier, citation="https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
                spans={"title": title}, provenance={"connector": self.name,
                    "source_type": "pubmed_record", "retrieval_status": "retrieved",
                    "retrieved_at": request["retrieved_at"], "source_version": item.get("sortpubdate"),
                    "content_sha256": hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest(),
                    "access_scope": "public_metadata",
                    "license": "NLM bibliographic metadata; article content license varies", "pmid": pmid,
                    "search_request": search_request, "request": request}))
        return rows

    def fetch(self, source: Source) -> Source:
        return source

    def expand_citations(self, source: Source, *, limit: int = 10) -> list[Source]:
        return []


class CrossrefConnector:
    name = "crossref"
    BASE = "https://api.crossref.org/works"

    def __init__(self, client: BiomedicalHttpClient, *, mailto: str = ""):
        self.client, self.mailto = client, mailto

    def resolve(self, doi: str) -> Source:
        payload, request = self.client.json(self.BASE + "/" + quote(doi, safe=""),
            params={"mailto": self.mailto} if self.mailto else None)
        row = payload["message"]
        normalized = str(row.get("DOI") or doi).lower()
        title = " ".join(row.get("title") or []) or "Untitled Crossref record"
        identifier = "doi:" + normalized
        return Source(id="source:" + hashlib.sha256(identifier.encode()).hexdigest(),
            identifier=identifier, citation="https://doi.org/" + normalized,
            spans={"title": title}, provenance={"connector": self.name,
                "source_type": row.get("type") or "crossref_work", "retrieval_status": "retrieved",
                "retrieved_at": request["retrieved_at"], "source_version": row.get("indexed", {}).get("date-time"),
                "content_sha256": request["content_sha256"], "access_scope": "public_metadata",
                "license": row.get("license") or "metadata_only; content license unspecified",
                "doi": normalized, "request": request})


class BioRxivConnector:
    name = "biorxiv_medrxiv"
    BASE = "https://api.biorxiv.org/details"

    def __init__(self, client: BiomedicalHttpClient):
        self.client = client

    def resolve(self, doi: str, *, server: str = "biorxiv") -> list[Source]:
        if server not in {"biorxiv", "medrxiv"}:
            raise ValueError("server must be biorxiv or medrxiv")
        payload, request = self.client.json(f"{self.BASE}/{server}/{quote(doi, safe='/')}")
        rows = []
        for item in payload.get("collection", []):
            normalized = str(item.get("doi") or doi).lower()
            identity = f"doi:{normalized}:v{item.get('version')}"
            rows.append(Source(id="source:" + hashlib.sha256(identity.encode()).hexdigest(),
                identifier=identity, citation="https://doi.org/" + normalized,
                spans={"title": item.get("title") or "Untitled preprint",
                       "abstract": item.get("abstract") or ""},
                provenance={"connector": self.name, "source_type": server + "_preprint",
                    "retrieval_status": "retrieved", "retrieved_at": request["retrieved_at"],
                    "source_version": item.get("version"),
                    "content_sha256": hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest(),
                    "access_scope": "public_metadata", "license": item.get("license") or "unspecified",
                    "doi": normalized, "server": server, "request": request}))
        return rows


class FederatedBiomedicalSourceClient:
    """ResearchSourceClient over shared connectors; produces sourced, non-authoritative bundles."""
    def __init__(self, *, cache_dir: Path | str | None = None, client=None,
                 include_pubmed: bool = True, email: str = ""):
        self.client = client or BiomedicalHttpClient(cache_dir=cache_dir)
        self.europe_pmc = EuropePmcConnector(self.client)
        self.pubmed = PubMedConnector(self.client, email=email)
        self.include_pubmed = include_pubmed

    def search(self, query: str) -> list[Source]:
        sources = self.europe_pmc.search(query, limit=10)
        if self.include_pubmed:
            sources.extend(self.pubmed.search(query, limit=10))
        return list(_deduplicate_sources(sources).values())

    def fetch(self, source: Source) -> Source:
        return self.europe_pmc.fetch(source) if source.provenance.get("connector") == "europe_pmc" else source

    def expand_citations(self, source: Source) -> list[Source]:
        return self.europe_pmc.expand_citations(source) if source.provenance.get("connector") == "europe_pmc" else []

    def synthesize(self, source_set: Iterable[Source], question: str) -> ResearchBundle:
        sources = list(_deduplicate_sources(source_set).values())
        claims = []
        for source in sources:
            span = "abstract" if source.spans.get("abstract") else "title"
            statement = source.spans.get(span, "").strip()
            if statement:
                claims.append(RetrievedClaim(statement=statement, source_id=source.id,
                                              span_ids=[span]))
        return ResearchBundle(sources=sources, retrieved_claims=claims,
            retrieval_trace=[{"operation": "deterministic_span_projection", "question": question,
                              "source_ids": [source.id for source in sources]}],
            limitations=["Retrieved metadata and open full text remain source material, not admitted scientific truth."],
            provenance={"backend": type(self).__name__, "connectors": sorted({
                source.provenance.get("connector", "unknown") for source in sources})})


def _deduplicate_sources(sources: Iterable[Source]) -> dict[str, Source]:
    """Deduplicate across all known DOI/PMCID/PMID aliases, retaining richer spans."""
    rows: dict[str, Source] = {}
    aliases: dict[str, str] = {}
    for source in sources:
        identifiers = {source.identifier.lower()}
        identifiers.update(f"{name}:{str(value).lower()}" for name in ("doi", "pmcid", "pmid")
                           if (value := source.provenance.get(name)))
        existing_keys = {aliases[value] for value in identifiers if value in aliases}
        key = sorted(existing_keys)[0] if existing_keys else sorted(identifiers)[0]
        current = rows.get(key)
        if current is None or sum(map(len, source.spans.values())) > sum(map(len, current.spans.values())):
            rows[key] = source
        for value in identifiers:
            aliases[value] = key
    return rows
