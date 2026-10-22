"""BioAgent wrapper for direct UniProt REST access."""
from pathlib import Path
from typing import Literal
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import run_cli

SCRIPT = Path(__file__).resolve().parent / "uniprot_client.py"

class UniprotInput(ToolInput):
    mode: Literal["search", "get", "map"]
    value: str
    output_format: Literal["json", "tsv", "xml", "fasta", "txt", "rdf"] = "json"
    fields: str = ""
    maximum_results: int = Field(default=25, ge=1, le=500)
    from_database: str = ""
    to_database: str = ""

@bioma_tool(name="openclaw_query_uniprot", description="Search UniProt, retrieve an accession, or map identifiers through the UniProt REST API.", category="protein-database", author="OpenClaw", args_schema=UniprotInput)
def query_uniprot(mode: str, value: str, output_format: str = "json", fields: str = "", maximum_results: int = 25, from_database: str = "", to_database: str = "") -> str:
    if not value.strip():
        raise ValueError("value is required")
    args = [{"search": "--search", "get": "--get", "map": "--map"}[mode], value, "--format", output_format, "--size", str(maximum_results)]
    if fields:
        args += ["--fields", fields]
    if mode == "map":
        if not from_database or not to_database:
            raise ValueError("from_database and to_database are required for map")
        args += ["--from", from_database, "--to", to_database]
    return run_cli(SCRIPT, args)
