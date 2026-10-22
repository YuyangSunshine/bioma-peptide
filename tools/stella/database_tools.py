"""
Database query tools: UniProt, KEGG, STRING, DepMap, DGIdb.
"""

from __future__ import annotations

import json
from typing import Optional

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import requests_tool_error


# ──────────────────────────────────────────────────────────────────────
# UniProt
# ──────────────────────────────────────────────────────────────────────

class UniProtQueryInput(ToolInput):
    query: str = Field(description="UniProt accession ID (e.g. P04637) or gene name (e.g. TP53_HUMAN)")


@bioma_tool(
    name="query_uniprot",
    description="Query UniProt for protein function, domains, PTMs, and disease associations.",
    category="database",
    version="1.0.0",
    author="STELLA Team",
    args_schema=UniProtQueryInput,
    requires_network=True,
)
def query_uniprot(query: str) -> str:
    """Fetch protein annotation from UniProt REST API."""
    # Try accession first, then gene name search
    url = f"https://rest.uniprot.org/uniprotkb/{query}.json"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
        else:
            # Search by gene name
            search_url = (
                f"https://rest.uniprot.org/uniprotkb/search?query={query}+AND+reviewed:true"
                f"&fields=accession,protein_name,organism_name,function,subcellular_location"
                f"&format=json&size=1"
            )
            resp = requests.get(search_url, timeout=15)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return f"No UniProt entry found for: {query}"
            data = results[0]

        # Extract key fields
        protein_name = (
            data.get("proteinDescription", {})
            .get("recommendedName", {})
            .get("fullName", {})
            .get("value", "Unknown")
        )
        organism = data.get("organism", {}).get("scientificName", "Unknown")
        accession = data.get("primaryAccession", query)

        # Function comment
        comments = data.get("comments", [])
        function_text = ""
        for comment in comments:
            if comment.get("commentType") == "FUNCTION":
                texts = comment.get("texts", [])
                if texts:
                    function_text = texts[0].get("value", "")[:500]
                    break

        result = {
            "accession": accession,
            "protein_name": protein_name,
            "organism": organism,
            "function": function_text,
        }
        return json.dumps(result, indent=2, ensure_ascii=False)

    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="UniProt") from exc


# ──────────────────────────────────────────────────────────────────────
# KEGG
# ──────────────────────────────────────────────────────────────────────

class KEGGPathwayInput(ToolInput):
    gene: str = Field(description="Gene symbol, e.g. TP53, EGFR, KRAS")
    organism: Optional[str] = Field(default="hsa", description="KEGG organism code: hsa (human), mmu (mouse)")


@bioma_tool(
    name="query_kegg_pathway",
    description="Query KEGG to find biological pathways containing a specific gene.",
    category="database",
    version="1.0.0",
    author="STELLA Team",
    args_schema=KEGGPathwayInput,
    requires_network=True,
)
def query_kegg_pathway(gene: str, organism: str = "hsa") -> str:
    """Find KEGG pathways for a gene."""
    try:
        # Search for KEGG gene entry
        search_url = f"https://rest.kegg.jp/find/genes/{organism}:{gene}"
        resp = requests.get(search_url, timeout=15)
        resp.raise_for_status()
        if not resp.text.strip():
            return f"Gene '{gene}' not found in KEGG ({organism})"

        # Get pathway list
        gene_id = resp.text.strip().split("\t")[0]
        pathways_url = f"https://rest.kegg.jp/link/pathway/{gene_id}"
        resp2 = requests.get(pathways_url, timeout=15)
        resp2.raise_for_status()

        pathway_ids = [line.split("\t")[1] for line in resp2.text.strip().splitlines() if "\t" in line]

        if not pathway_ids:
            return f"No pathways found for {gene}"

        # Get pathway names
        pathways = []
        for pid in pathway_ids[:10]:
            info_url = f"https://rest.kegg.jp/get/{pid}"
            resp3 = requests.get(info_url, timeout=10)
            resp3.raise_for_status()
            name = ""
            for line in resp3.text.splitlines():
                if line.startswith("NAME"):
                    name = line.split(None, 1)[1] if len(line.split()) > 1 else ""
                    break
            pathways.append({"pathway_id": pid, "name": name})

        return json.dumps({"gene": gene, "pathways": pathways}, indent=2)

    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="KEGG") from exc


# ──────────────────────────────────────────────────────────────────────
# STRING protein-protein interactions
# ──────────────────────────────────────────────────────────────────────

class STRINGInput(ToolInput):
    gene: str = Field(description="Gene symbol or protein name")
    species: Optional[int] = Field(default=9606, description="NCBI taxon ID: 9606 (human), 10090 (mouse)")
    score_threshold: Optional[float] = Field(default=0.7, description="Minimum combined interaction score (0-1)")


@bioma_tool(
    name="query_string_interactions",
    description="Query STRING database for protein-protein interactions of a gene.",
    category="database",
    version="1.0.0",
    author="STELLA Team",
    args_schema=STRINGInput,
    requires_network=True,
)
def query_string_interactions(
    gene: str,
    species: int = 9606,
    score_threshold: float = 0.7,
) -> str:
    """Retrieve PPI data from STRING API."""
    try:
        url = "https://string-db.org/api/json/interaction_partners"
        params = {
            "identifiers": gene,
            "species": species,
            "required_score": int(score_threshold * 1000),
            "limit": 20,
            "caller_identity": "bioagent",
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        interactions = [
            {
                "partner": item.get("preferredName_B", ""),
                "score": round(item.get("score", 0) / 1000, 3),
                "experimental_score": round(item.get("escore", 0) / 1000, 3),
            }
            for item in data
        ]
        return json.dumps(
            {"query_gene": gene, "species": species, "interactions": interactions},
            indent=2,
        )
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="STRING") from exc


# ──────────────────────────────────────────────────────────────────────
# DepMap
# ──────────────────────────────────────────────────────────────────────

class DepMapInput(ToolInput):
    gene: str = Field(description="Gene symbol, e.g. EGFR, KRAS, TP53")
    lineage: Optional[str] = Field(default=None, description="Cancer lineage filter, e.g. 'lung', 'breast'")


@bioma_tool(
    name="query_depmap_data",
    description="Query DepMap for gene dependency scores (CERES) across cancer cell lines.",
    category="database",
    version="1.0.0",
    author="STELLA Team",
    args_schema=DepMapInput,
    requires_network=True,
)
def query_depmap_data(gene: str, lineage: str | None = None) -> str:
    """Query DepMap gene essentiality data."""
    if lineage is not None:
        raise ValueError("lineage filtering is unsupported by this summary-retrieval endpoint")
    try:
        # DepMap public API
        url = f"https://depmap.org/portal/api/data_page/gene_summary?gene={gene}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return json.dumps(
            {
                "gene": gene,
                "summary": str(data)[:800],
                "operation": "retrieval",
                "analysis_performed": False,
                "lineage_supported": False,
                "summary_truncated": len(str(data)) > 800,
                "scientific_validation": "UNKNOWN",
                "note": "For detailed CERES scores, visit https://depmap.org",
            },
            indent=2,
        )
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="DepMap") from exc


# ──────────────────────────────────────────────────────────────────────
# DGIdb
# ──────────────────────────────────────────────────────────────────────

class DGIdbInput(ToolInput):
    gene: str = Field(description="Gene symbol")


@bioma_tool(
    name="query_dgidb_interactions",
    description="Query DGIdb for known drug-gene interactions for a target gene.",
    category="database",
    version="1.0.0",
    author="STELLA Team",
    args_schema=DGIdbInput,
    requires_network=True,
)
def query_dgidb_interactions(gene: str) -> str:
    """Get drug-gene interactions from DGIdb."""
    try:
        url = "https://dgidb.org/api/v2/interactions.json"
        params = {"genes": gene}
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        matched = data.get("matchedTerms", [])
        if not matched:
            return f"No DGIdb interactions found for gene: {gene}"

        interactions = []
        for term in matched[:1]:
            for interaction in term.get("interactions", [])[:15]:
                interactions.append({
                    "drug": interaction.get("drugName", ""),
                    "interaction_type": interaction.get("interactionTypes", []),
                    "sources": interaction.get("sources", [])[:3],
                })

        return json.dumps({"gene": gene, "interactions": interactions, "operation": "retrieval",
                           "analysis_performed": False, "coverage": "first matched term, at most 15 interactions and 3 sources each",
                           "scientific_validation": "UNKNOWN"}, indent=2)
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="DGIdb") from exc


# ──────────────────────────────────────────────────────────────────────
# Protein domain analysis
# ──────────────────────────────────────────────────────────────────────

class DomainInput(ToolInput):
    sequence: str = Field(description="Protein sequence")


@bioma_tool(
    name="analyze_protein_domains",
    description="Identify protein domains and families by querying Pfam (via InterProScan-style lookup).",
    category="database",
    version="1.0.0",
    author="STELLA Team",
    args_schema=DomainInput,
    requires_network=True,
)
def analyze_protein_domains(sequence: str) -> str:
    """Query EBI InterPro for domain annotations."""
    try:
        # Use EBI's InterPro lookup via sequence search
        url = "https://www.ebi.ac.uk/Tools/hmmer/search/hmmscan"
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        data = {"seqdb": "pfam", "seq": sequence[:500]}  # Trim for API limits

        resp = requests.post(url, data=data, headers=headers, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        hits = result.get("results", {}).get("hits", [])[:5]
        domains = [
            {"name": h.get("name", ""), "description": h.get("desc", ""),
             "e_value": h.get("evalue", None)}
            for h in hits
        ]
        return json.dumps({"domains": domains}, indent=2)
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="InterPro") from exc
