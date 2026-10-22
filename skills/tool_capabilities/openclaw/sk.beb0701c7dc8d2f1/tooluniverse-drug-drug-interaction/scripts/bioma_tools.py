"""BioAgent wrapper for the bundled bidirectional drug-interaction analysis."""
from pathlib import Path
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.openclaw._bioma_module import json_text, load_source, load_tooluniverse
SCRIPT = Path(__file__).resolve().parents[1] / "ddi_working_example.py"
class DdiInput(ToolInput):
    drug_a: str
    drug_b: str
@bioma_tool(name="openclaw_assess_tooluniverse_ddi", description="Assess a drug pair bidirectionally using RxNorm, DrugBank, DailyMed, literature, and adverse-event sources.", category="drug-safety", author="OpenClaw", args_schema=DdiInput)
def assess_tooluniverse_ddi(drug_a: str, drug_b: str) -> str:
    if not drug_a.strip() or not drug_b.strip() or drug_a.casefold() == drug_b.casefold(): raise ValueError("provide two distinct drug names")
    module = load_source(SCRIPT, "openclaw_tooluniverse_ddi")
    return json_text(module.analyze_ddi(load_tooluniverse(), drug_a, drug_b))
