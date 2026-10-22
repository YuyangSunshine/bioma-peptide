"""BioAgent wrapper for deterministic RDKit-based compound screening."""
from pathlib import Path
from typing import Literal
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.openclaw._bioma_module import json_text, load_source
SCRIPT = Path(__file__).resolve().parents[1] / "chem_tools.py"
class ChemicalScreenInput(ToolInput):
    smiles: str
    operation: Literal["properties", "lipinski", "synthetic_accessibility", "toxicity", "pains", "screen"] = "screen"
@bioma_tool(name="openclaw_screen_chemical_structure", description="Calculate RDKit molecular properties and screen a SMILES for Lipinski, accessibility, PAINS, and toxicity alerts.", category="cheminformatics", author="OpenClaw", args_schema=ChemicalScreenInput)
def screen_chemical_structure(smiles: str, operation: str = "screen") -> str:
    module = load_source(SCRIPT, "openclaw_chemcrow_tools")
    tools = module.ChemTools()
    if not tools.rdkit_available: raise RuntimeError("RDKit is required; mock fallback results are not exposed")
    if not tools.validate_smiles(smiles): raise ValueError("invalid SMILES")
    method = {"properties": tools.calculate_properties, "lipinski": tools.check_lipinski, "synthetic_accessibility": tools.calculate_sa_score, "toxicity": tools.check_toxicity_alerts, "pains": tools.check_pains, "screen": tools.screen_compound}[operation]
    return json_text(method(smiles))
