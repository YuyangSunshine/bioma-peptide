from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from pydantic import Field

from bioma.tools.base import Tool, ToolInput
from bioma.tools.errors import ToolError, requests_tool_error

PEPTIDE_SEARCH_URL = "https://peptidesearch.uniprot.org/asyncrest/"
UNIPROT_ENTRY_URL = "https://rest.uniprot.org/uniprotkb"
RCSB_DOWNLOAD_URL = "https://files.rcsb.org/download"
ALPHAFOLD_API_URL = "https://alphafold.ebi.ac.uk/api/prediction"
UNIPROT_SPARQL_URL = "https://sparql.uniprot.org/sparql"


def _get(url: str, **kwargs) -> requests.Response:
    return _request_with_retry("get", url, **kwargs)


def _post(url: str, **kwargs) -> requests.Response:
    return _request_with_retry("post", url, **kwargs)


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    last_error: requests.exceptions.RequestException | None = None
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", "BioMA/1.0 (sequence-structure-resolution)")
    kwargs["headers"] = headers
    # Interactive Web runs must fail promptly and show a recovery decision instead
    # of silently waiting through several long external-service timeouts.
    for attempt in range(2):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt == 1:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 6.0) if retry_after else 0.75 * (attempt + 1)
            except (TypeError, ValueError):
                delay = 1.5 * (attempt + 1)
            time.sleep(max(0.5, delay))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                raise
    assert last_error is not None
    raise last_error


class ResolveSequenceStructureInput(ToolInput):
    sequence: str = Field(
        description="Complete protein sequence in canonical one-letter amino-acid code."
    )
    reviewed_only: bool = Field(
        default=True,
        description="Prefer the reviewed UniProtKB/Swiss-Prot exact match.",
    )


def build_structure_tools(base_dir: Path, *, allow_cached: bool = False) -> tuple[Tool]:
    """Build a sequence-to-UniProt-to-structure tool bound to the artifact root."""
    artifact_root = (base_dir / "artifacts" / "structures").resolve()

    def resolve_sequence_structure(
        sequence: str,
        reviewed_only: bool = True,
    ) -> dict[str, object]:
        normalized = "".join(sequence.split()).upper()
        invalid = sorted(set(normalized) - set("ACDEFGHIKLMNPQRSTVWY"))
        if len(normalized) < 7 or invalid:
            raise ToolError(
                "A complete canonical protein sequence of at least 7 residues is required.",
                error_type="invalid_input",
                retriable=False,
                backend="UniProt",
        )
        sequence_sha256 = hashlib.sha256(normalized.encode("ascii")).hexdigest()
        if allow_cached:
            for metadata_path in artifact_root.glob("*_structure_index.json"):
                try:
                    cached = json.loads(metadata_path.read_text(encoding="utf-8"))
                    pdb_path = Path(str(cached.get("pdb_path") or ""))
                    if (
                        cached.get("sequence_sha256") == sequence_sha256
                        and cached.get("sequence_exact_match") is True
                        and pdb_path.is_file()
                    ):
                        return {
                            **cached,
                            "success": True,
                            "lookup_method": f"{cached.get('lookup_method', 'unknown')} (verified local cache)",
                            "metadata_path": str(metadata_path.resolve()),
                            "cache_used": True,
                        }
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
        try:
            sequence_md5 = hashlib.md5(normalized.encode("ascii")).hexdigest()
            accessions = _sparql_sequence_search(
                sequence_md5, reviewed_only=reviewed_only
            )
            lookup_method = "UniProt_SPARQL_MD5"
            if not accessions:
                accessions = _peptide_search(
                    normalized, reviewed_only=reviewed_only
                )
                lookup_method = "UniProt_peptide_search"
            match = _exact_uniprot_match(normalized, accessions)
            if match is None and reviewed_only:
                accessions = _sparql_sequence_search(
                    sequence_md5, reviewed_only=False
                ) or _peptide_search(normalized, reviewed_only=False)
                match = _exact_uniprot_match(normalized, accessions)
            if match is None:
                raise ToolError(
                    "UniProt peptide search returned no full-sequence exact match.",
                    error_type="not_found",
                    retriable=False,
                    backend="UniProt",
                )
            accession, entry = match
            structure = _download_best_structure(accession, entry, artifact_root)
        except requests.exceptions.RequestException as exc:
            raise requests_tool_error(exc, backend="UniProt/RCSB") from exc

        retrieved_at = datetime.now(timezone.utc).isoformat()
        protein_name = (
            entry.get("proteinDescription", {})
            .get("recommendedName", {})
            .get("fullName", {})
            .get("value")
        )
        organism = entry.get("organism", {}).get("scientificName")
        record: dict[str, object] = {
            "success": True,
            "match_type": "exact_full_sequence",
            "sequence_identity": 1.0,
            "sequence_verification": "entry.sequence.value == normalized_query",
            "sequence_exact_match": True,
            "sequence_length": len(normalized),
            "sequence_sha256": sequence_sha256,
            "sequence_md5": sequence_md5,
            "lookup_method": lookup_method,
            "uniprot_accession": accession,
            "uniprot_entry_id": entry.get("uniProtkbId"),
            "protein_name": protein_name,
            "organism": organism,
            "uniprot_url": f"https://www.uniprot.org/uniprotkb/{accession}/entry",
            "candidate_accessions": accessions[:20],
            "retrieved_at": retrieved_at,
            **structure,
        }
        artifact_root.mkdir(parents=True, exist_ok=True)
        metadata_path = artifact_root / f"{accession}_structure_index.json"
        record["pdb_sha256"] = hashlib.sha256(Path(str(record["pdb_path"])).read_bytes()).hexdigest()
        metadata_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        record["metadata_path"] = str(metadata_path.resolve())
        return record

    def reconcile_structure(contract):
        args = contract["arguments"]
        sequence = "".join(args["sequence"].split()).upper()
        digest = hashlib.sha256(sequence.encode("ascii")).hexdigest()
        started = datetime.fromisoformat(contract["effect_started_at"])
        for path in artifact_root.glob("*_structure_index.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                coordinate = Path(record["pdb_path"]).resolve()
                if not coordinate.is_relative_to(artifact_root):
                    continue
                content = coordinate.read_bytes()
                if (record.get("sequence_sha256") == digest
                    and record.get("sequence_exact_match") is True
                    and datetime.fromisoformat(record["retrieved_at"]) >= started
                    and record.get("pdb_sha256") == hashlib.sha256(content).hexdigest()
                    and any(line.startswith((b"ATOM  ", b"HETATM")) for line in content.splitlines())):
                    return "succeeded", {**record, "metadata_path": str(path.resolve())}
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return "unknown", None

    return (
        Tool(
            name="resolve_sequence_structure",
            description=(
                "Search UniProt by a protein sequence, verify a full-sequence exact match, "
                "then download the best linked RCSB PDB structure or an AlphaFold fallback."
            ),
            category="database",
            result_kind="retrieved_fact",
            version="1.0.0",
            author="BioMA",
            func=resolve_sequence_structure,
            args_schema=ResolveSequenceStructureInput,
            tags=("sequence", "uniprot", "pdb", "structure", "provenance"),
            requires_network=True,
            external_effect=True,
            replay_safe=True,
            reconcile_effect=reconcile_structure,
        ),
    )


def _sparql_sequence_search(sequence_md5: str, *, reviewed_only: bool) -> list[str]:
    reviewed_clause = "?protein up:reviewed true ." if reviewed_only else ""
    query = f"""PREFIX up:<http://purl.uniprot.org/core/>
SELECT ?protein WHERE {{
  ?protein a up:Protein ; up:sequence ?sequence .
  ?sequence up:md5Checksum \"{sequence_md5}\" .
  {reviewed_clause}
}} LIMIT 30"""
    response = _get(
        UNIPROT_SPARQL_URL,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=15,
    )
    response.raise_for_status()
    bindings = response.json().get("results", {}).get("bindings", [])
    return list(
        dict.fromkeys(
            str(item.get("protein", {}).get("value") or "").rstrip("/").rsplit("/", 1)[-1]
            for item in bindings
            if item.get("protein", {}).get("value")
        )
    )


def _peptide_search(sequence: str, *, reviewed_only: bool) -> list[str]:
    data = {"peps": sequence}
    if reviewed_only:
        data["spOnly"] = "on"
    response = _post(PEPTIDE_SEARCH_URL, data=data, timeout=15)
    response.raise_for_status()
    if response.status_code == 200:
        return _parse_accessions(response.text)
    if response.status_code != 202:
        raise ToolError(
            f"UniProt peptide search returned HTTP {response.status_code}.",
            error_type="unavailable",
            retriable=True,
            backend="UniProt",
        )
    status_url = response.headers.get("Location")
    if not status_url:
        raise ToolError(
            "UniProt peptide search did not return a job location.",
            error_type="internal",
            retriable=True,
            backend="UniProt",
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = _get(status_url, timeout=12, allow_redirects=False)
        if status.status_code == 200:
            return _parse_accessions(status.text)
        if status.status_code not in {202, 303}:
            status.raise_for_status()
        wait_seconds = min(float(status.headers.get("Retry-After") or 2), 5.0)
        time.sleep(max(0.5, wait_seconds))
    raise ToolError(
        "UniProt peptide search did not complete within 30 seconds.",
        error_type="timeout",
        retriable=True,
        backend="UniProt",
    )


def _parse_accessions(text: str) -> list[str]:
    values = re.findall(r"\b[A-Z0-9][A-Z0-9-]{4,14}\b", text.upper())
    return list(dict.fromkeys(values))


def _exact_uniprot_match(
    sequence: str, accessions: list[str]
) -> tuple[str, dict] | None:
    for accession in accessions[:30]:
        response = _get(f"{UNIPROT_ENTRY_URL}/{accession}.json", timeout=12)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        entry = response.json()
        entry_sequence = str(entry.get("sequence", {}).get("value") or "").upper()
        if entry_sequence == sequence:
            return accession, entry
    return None


def _download_best_structure(
    accession: str, entry: dict, artifact_root: Path
) -> dict[str, object]:
    pdb_records = [
        item
        for item in entry.get("uniProtKBCrossReferences", [])
        if item.get("database") == "PDB" and item.get("id")
    ]
    if pdb_records:
        selected = min(pdb_records, key=_pdb_rank)
        pdb_id = str(selected["id"]).upper()
        response = _get(f"{RCSB_DOWNLOAD_URL}/{pdb_id}.pdb", timeout=25)
        response.raise_for_status()
        pdb_text = response.text
        artifact_root.mkdir(parents=True, exist_ok=True)
        path = artifact_root / f"{accession}_{pdb_id}.pdb"
        path.write_text(pdb_text, encoding="utf-8")
        properties = {
            item.get("key"): item.get("value") for item in selected.get("properties", [])
        }
        return {
            "structure_source": "RCSB PDB",
            "pdb_id": pdb_id,
            "experimental_method": properties.get("Method"),
            "resolution": properties.get("Resolution"),
            "chain_coverage": properties.get("Chains"),
            "structure_url": f"https://www.rcsb.org/structure/{pdb_id}",
            "pdb_path": str(path.resolve()),
            # Hash the bytes actually persisted on disk. On Windows, text mode
            # newline translation can otherwise make the recorded provenance
            # differ from the downloaded artifact.
            "pdb_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "atom_records": sum(
                1 for line in pdb_text.splitlines() if line.startswith("ATOM")
            ),
        }

    response = _get(f"{ALPHAFOLD_API_URL}/{accession}", timeout=15)
    response.raise_for_status()
    records = response.json()
    if not records or not records[0].get("pdbUrl"):
        raise ToolError(
            f"No RCSB PDB cross-reference or AlphaFold model exists for {accession}.",
            error_type="not_found",
            retriable=False,
            backend="RCSB/AlphaFold DB",
        )
    pdb_url = records[0]["pdbUrl"]
    pdb_response = _get(pdb_url, timeout=25)
    pdb_response.raise_for_status()
    artifact_root.mkdir(parents=True, exist_ok=True)
    path = artifact_root / f"{accession}_AlphaFold.pdb"
    path.write_text(pdb_response.text, encoding="utf-8")
    return {
        "structure_source": "AlphaFold DB",
        "pdb_id": None,
        "experimental_method": "predicted",
        "resolution": None,
        "chain_coverage": f"1-{len(str(entry.get('sequence', {}).get('value') or ''))}",
        "structure_url": pdb_url,
        "pdb_path": str(path.resolve()),
        "pdb_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "atom_records": sum(
            1 for line in pdb_response.text.splitlines() if line.startswith("ATOM")
        ),
    }


def _pdb_rank(record: dict) -> tuple[int, float, str]:
    properties = {
        item.get("key"): item.get("value") for item in record.get("properties", [])
    }
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", str(properties.get("Resolution") or ""))
    resolution = float(match.group()) if match else float("inf")
    chains = str(properties.get("Chains") or "")
    coverage_match = re.search(r"(?:^|=)1-(\d+)(?:$|[, ])", chains)
    full_coverage = 0 if coverage_match and int(coverage_match.group(1)) >= 238 else 1
    return full_coverage, resolution, str(record.get("id") or "")
