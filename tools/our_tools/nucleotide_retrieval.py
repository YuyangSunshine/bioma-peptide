"""Nucleotide retrieval required by the imported PCR primer skill."""

from __future__ import annotations

import json as _json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool

_NCBI_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_NCBI_RATE_LIMIT = 0.35  # seconds between requests (max 3/sec without API key)

# Common name → scientific name mapping for NCBI queries
_SPECIES_MAP: dict[str, str] = {
    "human": "Homo sapiens",
    "mouse": "Mus musculus",
    "rat": "Rattus norvegicus",
    "zebrafish": "Danio rerio",
    "fruit fly": "Drosophila melanogaster",
    "fly": "Drosophila melanogaster",
    "drosophila": "Drosophila melanogaster",
    "c elegans": "Caenorhabditis elegans",
    "celegans": "Caenorhabditis elegans",
    "worm": "Caenorhabditis elegans",
    "yeast": "Saccharomyces cerevisiae",
    "s cerevisiae": "Saccharomyces cerevisiae",
    "ecoli": "Escherichia coli",
    "e coli": "Escherichia coli",
    "arabidopsis": "Arabidopsis thaliana",
    "rice": "Oryza sativa",
    "maize": "Zea mays",
    "corn": "Zea mays",
    "xenopus": "Xenopus laevis",
    "chicken": "Gallus gallus",
    "dog": "Canis lupus familiaris",
    "pig": "Sus scrofa",
    "cow": "Bos taurus",
    "monkey": "Macaca mulatta",
    "rhesus": "Macaca mulatta",
}


def _resolve_species(name: str) -> str:
    """Convert common name to scientific name for NCBI queries."""
    key = name.strip().lower()
    if key in _SPECIES_MAP:
        return _SPECIES_MAP[key]
    # Already looks scientific (two words, capitalized) — use as-is
    if re.match(r"^[A-Z][a-z]+ [a-z]+$", name.strip()):
        return name.strip()
    # Unknown — use as-is with quotes
    return name.strip()


# Known accession prefixes and what they mean
_REFSEQ_RNA_PREFIXES = ("NM_", "NR_", "XM_", "XR_")
_REFSEQ_DNA_PREFIXES = ("NC_", "NG_", "NT_", "NW_", "AC_", "AP_")
_GENBANK_PREFIXES = (
    "AY",
    "AF",
    "DQ",
    "EU",
    "FJ",
    "GU",
    "HE",
    "HG",
    "HQ",
    "JF",
    "JN",
    "JQ",
    "JX",
    "KC",
    "KF",
    "KJ",
    "KP",
    "KR",
    "KT",
    "KU",
    "KV",
    "KX",
    "KY",
    "KZ",
    "MF",
    "MG",
    "MH",
    "MK",
    "MN",
    "MT",
    "MW",
    "MZ",
    "OK",
    "OL",
    "OM",
    "ON",
    "OP",
    "OQ",
    "OR",
    "PP",
    "PQ",
    "U",
    "V",
    "X",
    "Y",
    "Z",
)


def _looks_like_accession(s: str) -> bool:
    s_upper = s.upper().strip()
    base = s_upper.split(".", 1)[0]
    if re.fullmatch(r"[A-Z]{2}_[0-9]+", base):
        return True
    return any(
        base.startswith(prefix) and base[len(prefix) :].isdigit()
        for prefix in _GENBANK_PREFIXES
    )


def _looks_like_refseq_mrna(s: str) -> bool:
    return bool(re.match(r"^(NM_|XM_)", s.strip().upper()))


def _looks_like_refseq_genomic(s: str) -> bool:
    return bool(re.match(r"^(NC_|NG_|NT_|NW_)", s.strip().upper()))


def _looks_like_ensembl_transcript(s: str) -> bool:
    """ENST00000229239 or ENSMUST..."""
    return bool(re.match(r"^ENST", s.strip(), re.IGNORECASE))


def _looks_like_chromosome_coords(s: str) -> bool:
    """chr12:6534516-6538370 or 12:6534516-6538370"""
    return bool(re.match(r"^(?:chr)?\d{1,2}:\d+-\d+$", s.strip()))


def _guess_input_type(s: str) -> str:
    """Return a human-readable label for the input type."""
    s = s.strip()
    if _looks_like_refseq_mrna(s):
        return f"RefSeq mRNA ({s})"
    if _looks_like_refseq_genomic(s):
        return f"RefSeq genomic ({s})"
    if _looks_like_accession(s):
        return f"NCBI accession ({s})"
    if _looks_like_ensembl_transcript(s):
        return f"Ensembl transcript ({s})"
    if _looks_like_chromosome_coords(s):
        return f"chromosomal coordinates ({s})"
    return f"gene symbol '{s}'"


class FetchNucleotideInput(ToolInput):
    query: str = Field(
        description=(
            "What to look up. Accepts any of these formats:\n"
            "- RefSeq mRNA (NM_ prefix)\n"
            "- RefSeq genomic (NC_ prefix)\n"
            "- GenBank accession\n"
            "- Ensembl transcript ID (ENST prefix)\n"
            "- Chromosome coordinates (chr:start-end)\n"
            "- Gene symbol (needs species name)"
        )
    )
    species: str = Field(
        default="human",
        description="Species name for gene symbol lookup (e.g., human, mouse, rat, zebrafish). "
        "Ignored when query is already an accession number or coordinates.",
    )
    molecule: str = Field(
        default="auto",
        description="Preferred molecule type: 'mrna' (CDS/transcript), 'genomic' (chromosomal DNA), "
        "or 'auto' (let the tool decide based on what's available).",
    )
    max_length: int = Field(
        default=8000,
        description="Maximum sequence length to return (characters). Increase for genomic regions.",
    )


@bioma_tool(
    name="fetch_nucleotide_sequence",
    description=(
        "Fetch DNA nucleotide sequences from NCBI. Works with RefSeq accessions (NM_/NC_), "
        "GenBank IDs, Ensembl transcript IDs, chromosomal coordinates, or gene symbol + species. "
        "Returns raw DNA sequence plus metadata (gene name, organism, sequence type, length). "
        "Use for PCR primer design, sgRNA design, restriction analysis, or any task "
        "needing actual DNA sequence. This returns DNA — NOT protein like UniProt."
    ),
    category="analysis",
    version="1.1.0",
    author="PredAgent Team",
    args_schema=FetchNucleotideInput,
    tags=["dna", "ncbi", "sequence", "nucleotide", "fetch"],
)
def fetch_nucleotide_sequence(
    query: str,
    species: str = "human",
    molecule: str = "auto",
    max_length: int = 8000,
) -> str:
    """Fetch DNA sequence from NCBI — generic over input type."""
    started = time.time()
    query = query.strip().rstrip(".")

    # ── Case 1: Chromosome coordinates ──
    if _looks_like_chromosome_coords(query):
        return _fetch_by_coordinates(query, max_length, started)

    # ── Case 2: It's already an accession → fetch directly ──
    if _looks_like_accession(query):
        acc = query.upper()
        seq, title = _efetch_with_title(acc)
        if seq:
            return _format_result(
                acc, title or acc, seq, max_length, started, molecule="DNA"
            )
        # Fall through to gene search if direct fetch fails

    # ── Case 3: Try Ensembl transcript ID → NCBI cross-reference ──
    if _looks_like_ensembl_transcript(query):
        acc = _ensembl_to_ncbi(query, species)
        if acc:
            seq, title = _efetch_with_title(acc)
            if seq:
                return _format_result(
                    acc,
                    title or query,
                    seq,
                    max_length,
                    started,
                    molecule="mRNA",
                    note=f"cross-referenced from {query}",
                )

    # ── Case 4: Gene name or free-text → search NCBI Nucleotide ──
    molecule_filter = molecule if molecule in ("mrna", "genomic") else "auto"
    resolved = _search_nucleotide_generic(query, species, molecule_filter)
    if resolved is None:
        elapsed = time.time() - started
        return _json.dumps(
            {
                "error": f"No nucleotide record found for '{query}' ({species}).",
                "wall_seconds": round(elapsed, 2),
            },
            ensure_ascii=False,
        )

    acc, title, mol_type = resolved
    time.sleep(_NCBI_RATE_LIMIT)
    seq = _efetch_sequence(acc)
    elapsed = time.time() - started
    if seq is None:
        return _json.dumps(
            {
                "error": f"Found {acc} ({title}) but could not fetch sequence.",
                "wall_seconds": round(elapsed, 2),
            },
            ensure_ascii=False,
        )

    return _format_result(acc, title, seq, max_length, started, molecule=mol_type)


def _format_result(
    accession: str,
    title: str,
    seq_raw: str,
    max_len: int,
    started: float,
    molecule: str = "auto",
    note: str = "",
) -> str:
    seq_clean = re.sub(r"\s+", "", seq_raw).upper()
    nt = {"A": 0, "T": 0, "G": 0, "C": 0, "N": 0}
    for b in seq_clean[: min(len(seq_clean), 20000)]:
        if b in nt:
            nt[b] += 1
    truncated = len(seq_clean) > max_len
    if truncated:
        seq_clean = seq_clean[:max_len]
    elapsed = time.time() - started
    result = {
        "accession": accession,
        "title": title,
        "molecule_type": molecule,
        "sequence": seq_clean,
        "length": len(seq_clean),
        "base_composition": {
            "A": nt["A"],
            "T": nt["T"],
            "G": nt["G"],
            "C": nt["C"],
            "N": nt["N"],
        },
        "gc_content_pct": round(
            100.0 * (nt["G"] + nt["C"]) / max(1, nt["A"] + nt["T"] + nt["G"] + nt["C"]),
            1,
        ),
        "truncated": truncated,
        "wall_seconds": round(elapsed, 2),
    }
    if note:
        result["note"] = note
    return _json.dumps(result, ensure_ascii=False)


def _fetch_by_coordinates(coords: str, max_len: int, started: float) -> str:
    """Fetch sequence by chr:start-end coordinates via NCBI E-utilities."""
    m = re.match(r"^(?:chr)?(\d{1,2}):(\d+)-(\d+)$", coords.strip())
    if not m:
        elapsed = time.time() - started
        return _json.dumps(
            {
                "error": f"Invalid coordinates: {coords}",
                "wall_seconds": round(elapsed, 2),
            }
        )
    chrom, start, end = m.group(1), m.group(2), m.group(3)
    # Search for human chromosome reference
    time.sleep(_NCBI_RATE_LIMIT)
    search_term = (
        f"NC_0000{chrom.zfill(2)}"
        if len(chrom) <= 2
        else f"chromosome {chrom} [Organism] AND Homo sapiens [Organism]"
    )
    params = {
        "db": "nucleotide",
        "term": search_term,
        "retmode": "json",
        "retmax": "1",
    }
    try:
        data = _ncbi_request("esearch", params)
        id_list = data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return _json.dumps(
                {"error": f"Could not find reference for chromosome {chrom}."}
            )
        acc_id = id_list[0]
        # Fetch the region
        time.sleep(_NCBI_RATE_LIMIT)
        efetch_params = {
            "db": "nucleotide",
            "id": acc_id,
            "seq_start": start,
            "seq_stop": end,
            "rettype": "fasta",
            "retmode": "text",
        }
        url = f"{_NCBI_EUTILS_BASE}/efetch.fcgi?{urllib.parse.urlencode(efetch_params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "BioAgent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            fasta = resp.read().decode("utf-8")
        lines = fasta.strip().split("\n")
        header = (
            lines[0][1:] if lines[0].startswith(">") else f"chr{chrom}:{start}-{end}"
        )
        seq = "".join(ln for ln in lines if not ln.startswith(">"))
        return _format_result(
            f"chr{chrom}:{start}-{end}",
            header,
            seq,
            max_len,
            started,
            molecule="genomic",
        )
    except Exception as exc:
        elapsed = time.time() - started
        return _json.dumps(
            {
                "error": f"Failed to fetch coordinates: {exc}",
                "wall_seconds": round(elapsed, 2),
            }
        )


def _search_nucleotide_generic(
    gene: str, species: str, molecule: str
) -> tuple[str, str, str] | None:
    """Search NCBI for a nucleotide record. Returns (accession, title, molecule_type)."""
    # Normalize gene name: GAPDH → GAPDH; "GAPDH human" → GAPDH
    gene = gene.strip().split()[0].strip('"') if gene else gene
    # Resolve common species name to scientific name for NCBI
    sci_species = _resolve_species(species)

    # Try mRNA first, then genomic
    queries = []
    if molecule in ("mrna", "auto"):
        queries.append(
            (
                f'"{gene}"[Gene Name] AND "{sci_species}"[Organism] AND biomol_mrna[PROP]',
                "mRNA",
            )
        )
    if molecule in ("genomic", "auto"):
        queries.append(
            (
                f'"{gene}"[Gene Name] AND "{sci_species}"[Organism] AND (biomol_genomic[PROP] OR refseq[Keyword])',
                "genomic",
            )
        )
    # Fallback: if species query returns nothing, try without organism filter
    queries.append((f'"{gene}"[Gene Name] AND biomol_mrna[PROP]', "mRNA"))

    for term, mol_label in queries:
        params = {
            "db": "nucleotide",
            "term": term,
            "retmode": "json",
            "retmax": "5",
            "sort": "relevance",
        }
        try:
            data = _ncbi_request("esearch", params)
            id_list = data.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                continue
            time.sleep(_NCBI_RATE_LIMIT)
            summaries = _ncbi_request(
                "esummary",
                {"db": "nucleotide", "id": ",".join(id_list), "retmode": "json"},
            )
            results = summaries.get("result", {})
            # Prefer RefSeq mRNA (NM_)
            for uid in id_list:
                entry = results.get(uid, {})
                acc = entry.get("accessionversion") or entry.get("caption") or ""
                title_text = entry.get("title", gene)
                if acc and acc.startswith("NM_"):
                    return acc, title_text, "mRNA"
            # Next best: any RefSeq
            for uid in id_list:
                entry = results.get(uid, {})
                acc = entry.get("accessionversion") or entry.get("caption") or ""
                title_text = entry.get("title", gene)
                if acc and re.match(r"^(N[MCRGX]_)", acc):
                    return acc, title_text, mol_label
            # Fallback: first hit
            entry = results.get(id_list[0], {})
            acc = entry.get("accessionversion") or entry.get("caption") or id_list[0]
            return acc, entry.get("title", gene), mol_label
        except Exception:
            continue

    return None


_ensembl_cache: dict[str, str | None] = {}


def _ensembl_to_ncbi(ensembl_id: str, species: str) -> str | None:
    """Cross-reference Ensembl transcript to NCBI RefSeq via E-utilities."""
    if ensembl_id in _ensembl_cache:
        return _ensembl_cache[ensembl_id]
    term = f"{ensembl_id}[Accession] AND {species}[Organism]"
    params = {"db": "nucleotide", "term": term, "retmode": "json", "retmax": "3"}
    try:
        data = _ncbi_request("esearch", params)
        id_list = data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            _ensembl_cache[ensembl_id] = None
            return None
        time.sleep(_NCBI_RATE_LIMIT)
        summaries = _ncbi_request(
            "esummary",
            {"db": "nucleotide", "id": ",".join(id_list), "retmode": "json"},
        )
        results = summaries.get("result", {})
        for uid in id_list:
            entry = results.get(uid, {})
            acc = entry.get("accessionversion") or entry.get("caption") or ""
            if acc and acc.startswith("NM_"):
                _ensembl_cache[ensembl_id] = acc
                return acc
    except Exception:
        pass
    _ensembl_cache[ensembl_id] = None
    return None


def _efetch_with_title(accession: str) -> tuple[str | None, str | None]:
    """Fetch sequence AND title for an accession. Returns (sequence, title)."""
    title = accession
    try:
        time.sleep(_NCBI_RATE_LIMIT)
        summaries = _ncbi_request(
            "esummary",
            {"db": "nucleotide", "id": accession, "retmode": "json"},
        )
        results = summaries.get("result", {})
        uid = list(results.get("uids", []))
        if uid and uid[0] in results:
            title = results[uid[0]].get("title", accession)
    except Exception:
        pass
    seq = _efetch_sequence(accession)
    return seq, title


def _efetch_sequence(accession: str) -> str | None:
    """Fetch FASTA sequence from NCBI by accession."""
    params = {
        "db": "nucleotide",
        "id": accession,
        "rettype": "fasta",
        "retmode": "text",
    }
    try:
        url = f"{_NCBI_EUTILS_BASE}/efetch.fcgi?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "BioAgent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            fasta = resp.read().decode("utf-8")
        # Strip FASTA header lines
        lines = fasta.strip().split("\n")
        seq_lines = [ln for ln in lines if not ln.startswith(">")]
        return "".join(seq_lines)
    except Exception:
        return None


def _ncbi_request(endpoint: str, params: dict) -> dict:
    """Make a JSON request to NCBI E-utilities."""
    url = f"{_NCBI_EUTILS_BASE}/{endpoint}.fcgi?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "BioAgent/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _json.loads(resp.read().decode("utf-8"))
