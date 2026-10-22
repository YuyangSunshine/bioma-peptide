"""Cyclic-peptide membrane-permeability prediction adapters."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError


class CPMPInput(ToolInput):
    smiles: list[str] = Field(description="Cyclic-peptide SMILES strings.")
    output_dir: str = Field(description="Directory for CPMP input and result CSV files.")
    cpmp_root: str = Field(
        default="",
        description="CPMP checkout path; defaults to the BIOMA_CPMP_ROOT environment variable.",
    )


@bioma_tool(
    name="predict_cyclic_peptide_permeability",
    description=(
        "Run a configured local CPMP checkout to predict PAMPA, Caco-2, RRCK, and "
        "MDCK membrane-permeability values for cyclic-peptide SMILES."
    ),
    category="analysis",
    version="1.0.0",
    author="BioMA",
    args_schema=CPMPInput,
    tags=["peptide", "cyclic-peptide", "permeability", "pampa", "caco2", "cpmp"],
)
def predict_cyclic_peptide_permeability(
    smiles: list[str],
    output_dir: str,
    cpmp_root: str = "",
) -> str:
    if not smiles or any(not item.strip() for item in smiles):
        raise ToolError(
            "smiles must contain at least one non-empty cyclic-peptide SMILES.",
            error_type="invalid_input",
            retriable=False,
            backend="CPMP",
        )
    root_value = cpmp_root or os.getenv("BIOMA_CPMP_ROOT", "")
    root = Path(root_value).expanduser().resolve() if root_value else None
    if root is None or not (root / "predict.py").is_file():
        raise ToolError(
            "CPMP is not configured. Set BIOMA_CPMP_ROOT to its local checkout.",
            error_type="unavailable",
            retriable=False,
            backend="CPMP",
        )

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    input_path = destination / "cpmp_input.csv"
    result_path = destination / "cpmp_predictions.csv"
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["smiles"])
        writer.writerows([item.strip()] for item in smiles)

    command = [
        sys.executable,
        str(root / "predict.py"),
        "--input_file",
        str(input_path),
        "--result_file",
        str(result_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ToolError(
            exc.stderr.strip() or "CPMP prediction failed.",
            error_type="execution_error",
            retriable=False,
            backend="CPMP",
        ) from exc
    if not result_path.is_file():
        raise ToolError(
            "CPMP completed without producing its result CSV.",
            error_type="invalid_response",
            retriable=False,
            backend="CPMP",
        )

    with result_path.open(encoding="utf-8", newline="") as handle:
        predictions = list(csv.DictReader(handle))
    return json.dumps(
        {
            "status": "ok",
            "model": "CPMP",
            "input_csv": str(input_path),
            "result_csv": str(result_path),
            "predictions": predictions,
            "log": completed.stdout[-2000:],
        },
        ensure_ascii=False,
        indent=2,
    )
