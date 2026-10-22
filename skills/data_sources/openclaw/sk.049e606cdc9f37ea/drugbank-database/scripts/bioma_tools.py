"""BioAgent wrapper for local DrugBank XML queries."""
from pathlib import Path
from typing import Literal
import xml.etree.ElementTree as ET
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file
from skills._meta.support.openclaw._bioma_module import json_text, load_source

SCRIPT = Path(__file__).resolve().parent / "drugbank_helper.py"

class DrugbankInput(ToolInput):
    xml_path: str
    mode: Literal["info", "interactions", "targets", "properties", "search"]
    value: str
    exact_name: bool = False

@bioma_tool(name="openclaw_query_drugbank", description="Query a licensed local DrugBank XML export for drug details, interactions, targets, properties, or names.", category="drug-database", author="OpenClaw", args_schema=DrugbankInput)
def query_drugbank(xml_path: str, mode: str, value: str, exact_name: bool = False) -> str:
    module = load_source(SCRIPT, "openclaw_drugbank_helper")
    root = ET.parse(existing_file(xml_path, "xml_path")).getroot()
    helper = module.DrugBankHelper(root)
    result = {"info": helper.get_drug_info, "interactions": helper.get_interactions, "targets": helper.get_targets, "properties": helper.get_properties}.get(mode)
    return json_text(helper.search_by_name(value, exact=exact_name) if mode == "search" else result(value))
