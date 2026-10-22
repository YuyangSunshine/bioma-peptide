"""Local DrugBank XML queries extracted from the Skill examples."""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class DrugbankXmlInput(ToolInput):
    xml_path: str = Field(description="Uncompressed DrugBank full-database XML path")
    query: str = Field(description="Exact DrugBank ID, drug name, or CAS number")
    operation: Literal["info", "interactions", "targets", "external_ids"] = Field(
        default="info", description="Information to extract"
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum nested records returned")


_NS = "{http://www.drugbank.ca}"


def _text(element: ET.Element, path: str) -> str | None:
    child = element.find(path)
    return child.text.strip() if child is not None and child.text else None


def _find_drug(xml_path: Path, query: str) -> ET.Element:
    normalized = query.strip().lower()
    for _, element in ET.iterparse(xml_path, events=("end",)):
        if element.tag != f"{_NS}drug":
            continue
        primary_id = _text(element, f'{_NS}drugbank-id[@primary="true"]')
        name = _text(element, f"{_NS}name")
        cas = _text(element, f"{_NS}cas-number")
        if normalized in {(primary_id or "").lower(), (name or "").lower(), (cas or "").lower()}:
            return element
        element.clear()
    raise LookupError(f"No exact DrugBank match for: {query}")


@bioma_tool(
    name="sciagent_query_drugbank_xml",
    description="Look up drug metadata, interactions, targets, or external IDs in a licensed local DrugBank XML export.",
    category="pharmacology",
    author="SciAgent",
    args_schema=DrugbankXmlInput,
)
def query_drugbank_xml(xml_path: str, query: str, operation: str = "info", limit: int = 100) -> str:
    path = Path(xml_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    drug = _find_drug(path, query)
    drug_id = _text(drug, f'{_NS}drugbank-id[@primary="true"]')
    name = _text(drug, f"{_NS}name")
    result: dict[str, object] = {"drugbank_id": drug_id, "name": name, "operation": operation}
    if operation == "info":
        result.update(
            type=drug.get("type"),
            description=_text(drug, f"{_NS}description"),
            indication=_text(drug, f"{_NS}indication"),
            mechanism_of_action=_text(drug, f"{_NS}mechanism-of-action"),
            cas_number=_text(drug, f"{_NS}cas-number"),
            groups=[item.text for item in drug.findall(f"{_NS}groups/{_NS}group") if item.text],
        )
    elif operation == "interactions":
        rows = []
        for item in drug.findall(f"{_NS}drug-interactions/{_NS}drug-interaction")[:limit]:
            rows.append({
                "drugbank_id": _text(item, f"{_NS}drugbank-id"),
                "name": _text(item, f"{_NS}name"),
                "description": _text(item, f"{_NS}description"),
            })
        result["interactions"] = rows
    elif operation == "targets":
        rows = []
        for item in drug.findall(f"{_NS}targets/{_NS}target")[:limit]:
            polypeptide = item.find(f"{_NS}polypeptide")
            rows.append({
                "id": item.get("id"),
                "name": _text(item, f"{_NS}name"),
                "organism": _text(item, f"{_NS}organism"),
                "actions": [action.text for action in item.findall(f"{_NS}actions/{_NS}action") if action.text],
                "polypeptide_id": polypeptide.get("id") if polypeptide is not None else None,
                "polypeptide_source": polypeptide.get("source") if polypeptide is not None else None,
            })
        result["targets"] = rows
    else:
        rows = []
        for item in drug.findall(f"{_NS}external-identifiers/{_NS}external-identifier")[:limit]:
            rows.append({
                "resource": _text(item, f"{_NS}resource"),
                "identifier": _text(item, f"{_NS}identifier"),
            })
        result["external_ids"] = rows
    return json.dumps(result, ensure_ascii=False, default=str)
