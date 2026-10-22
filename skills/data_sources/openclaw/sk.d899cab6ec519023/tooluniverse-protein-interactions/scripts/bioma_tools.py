"""BioAgent wrapper for multi-source protein interaction analysis."""
from pathlib import Path
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.openclaw._bioma_module import json_text, load_source, load_tooluniverse
SCRIPT = Path(__file__).resolve().parents[1] / "python_implementation.py"
class ProteinInteractionInput(ToolInput):
    proteins: list[str] = Field(min_length=1, max_length=100)
    species: int = Field(default=9606, ge=1)
    confidence_score: float = Field(default=0.7, ge=0, le=1)
    include_biogrid: bool = False
    include_structure: bool = False
@bioma_tool(name="openclaw_analyze_protein_interactions", description="Map proteins, retrieve interaction networks, and summarize enrichment with optional BioGRID or structural evidence.", category="protein-interactions", author="OpenClaw", args_schema=ProteinInteractionInput)
def analyze_protein_interactions(proteins: list[str], species: int = 9606, confidence_score: float = 0.7, include_biogrid: bool = False, include_structure: bool = False) -> str:
    module = load_source(SCRIPT, "openclaw_protein_interactions")
    return json_text(module.analyze_protein_network(load_tooluniverse(), proteins, species, confidence_score, include_biogrid, include_structure, True))
