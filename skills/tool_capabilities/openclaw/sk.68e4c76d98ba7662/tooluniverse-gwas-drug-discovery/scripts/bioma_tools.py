"""BioAgent wrapper for GWAS-to-drug target discovery."""
from pathlib import Path
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.openclaw._bioma_module import json_text, load_source
SCRIPT = Path(__file__).resolve().parents[1] / "python_implementation.py"
class GwasDrugTargetInput(ToolInput):
    disease: str
    maximum_p_value: float = Field(default=5e-8, gt=0, le=1)
    minimum_associations: int = Field(default=1, ge=1)
    include_druggability: bool = True
    maximum_targets: int = Field(default=10, ge=1, le=50)
@bioma_tool(name="openclaw_discover_gwas_drug_targets", description="Rank candidate drug targets for a disease from GWAS evidence and optional druggability lookups.", category="gwas", author="OpenClaw", args_schema=GwasDrugTargetInput)
def discover_gwas_drug_targets(disease: str, maximum_p_value: float = 5e-8, minimum_associations: int = 1, include_druggability: bool = True, maximum_targets: int = 10) -> str:
    if not disease.strip(): raise ValueError("disease is required")
    module = load_source(SCRIPT, "openclaw_gwas_drug_discovery")
    return json_text(module.discover_drug_targets(disease, maximum_p_value, minimum_associations, include_druggability, maximum_targets))
