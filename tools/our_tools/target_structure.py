"""Experimental and predicted target-structure retrieval."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError, requests_tool_error


ALPHAFOLD_API = "https://alphafold.ebi.ac.uk/api/prediction"


class AlphaFoldStructureInput(ToolInput):
    uniprot_id: str = Field(description="UniProt accession, for example P04637.")
    structure_format: Literal["mmcif", "pdb"] = Field(
        default="mmcif",
        description="Coordinate format to download when output_dir is provided.",
    )
    output_dir: str = Field(
        default="",
        description="Optional directory for downloading every returned AFDB fragment.",
    )


@bioma_tool(
    name="fetch_alphafold_structures",
    description=(
        "Retrieve AlphaFold DB prediction metadata for a UniProt accession and optionally "
        "download all returned PDB or mmCIF fragments."
    ),
    category="database",
    version="1.0.0",
    author="BioMA",
    args_schema=AlphaFoldStructureInput,
    tags=["protein", "structure", "alphafold", "afdb"],
    requires_network=True,
)
def fetch_alphafold_structures(
    uniprot_id: str,
    structure_format: Literal["mmcif", "pdb"] = "mmcif",
    output_dir: str = "",
) -> str:
    accession = uniprot_id.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,10}", accession):
        raise ToolError(
            "UniProt accession must contain 6-10 letters or digits.",
            error_type="invalid_input",
            retriable=False,
            backend="AlphaFold DB",
        )

    try:
        response = requests.get(f"{ALPHAFOLD_API}/{accession}", timeout=60)
        if response.status_code == 404:
            raise ToolError(
                f"AlphaFold DB has no prediction for {accession}.",
                error_type="not_found",
                retriable=False,
                backend="AlphaFold DB",
            )
        response.raise_for_status()
        records = response.json()
        if not isinstance(records, list) or not records:
            raise ToolError(
                "AlphaFold DB returned no prediction records.",
                error_type="invalid_response",
                retriable=False,
                backend="AlphaFold DB",
            )

        destination = Path(output_dir).expanduser() if output_dir else None
        if destination is not None:
            destination.mkdir(parents=True, exist_ok=True)

        results = [
            _alphafold_record(record, accession, structure_format, destination, index)
            for index, record in enumerate(records, start=1)
        ]
        return json.dumps(
            {"status": "ok", "uniprot_id": accession, "structures": results},
            ensure_ascii=False,
            indent=2,
        )
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="AlphaFold DB") from exc


def _alphafold_record(
    record: object,
    accession: str,
    structure_format: str,
    destination: Path | None,
    index: int,
) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ToolError(
            "AlphaFold DB returned a malformed prediction record.",
            error_type="invalid_response",
            retriable=False,
            backend="AlphaFold DB",
        )
    url_key = "cifUrl" if structure_format == "mmcif" else "pdbUrl"
    structure_url = record.get(url_key)
    if not isinstance(structure_url, str) or not structure_url:
        raise ToolError(
            f"AlphaFold DB record has no {structure_format} URL.",
            error_type="invalid_response",
            retriable=False,
            backend="AlphaFold DB",
        )

    result: dict[str, object] = {
        "entry_id": record.get("entryId"),
        "fragment": record.get("modelEntityId", index),
        "structure_url": structure_url,
        "pae_url": record.get("paeDocUrl"),
        "confidence_url": record.get("bcifUrl"),
    }
    if destination is not None:
        coordinate_response = requests.get(structure_url, timeout=120)
        coordinate_response.raise_for_status()
        suffix = "cif" if structure_format == "mmcif" else "pdb"
        path = destination / f"{accession}-F{index}.{suffix}"
        path.write_bytes(coordinate_response.content)
        result["path"] = str(path.resolve())
    return result
