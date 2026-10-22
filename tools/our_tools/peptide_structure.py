"""Cyclic-peptide and protein-peptide structure prediction."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError


class BoltzPeptideInput(ToolInput):
    peptide_sequence: str = Field(description="Canonical amino-acid sequence of the peptide.")
    target_sequence: str = Field(
        default="",
        description="Optional target-protein sequence for protein-peptide complex prediction.",
    )
    cyclic: bool = Field(default=True, description="Model the peptide as head-to-tail cyclic.")
    output_dir: str = Field(description="Directory for the reproducible Boltz input and outputs.")
    diffusion_samples: int = Field(default=5, ge=1, le=25)
    accelerator: Literal["cpu", "gpu"] = Field(default="cpu")
    use_msa_server: bool = Field(default=True)


@bioma_tool(
    name="predict_cyclic_peptide_structure",
    description=(
        "Run an installed Boltz model for a cyclic-peptide conformer set or a "
        "protein-peptide complex, returning mmCIF structures and the exact input YAML."
    ),
    category="analysis",
    version="1.0.0",
    author="BioMA",
    args_schema=BoltzPeptideInput,
    tags=["peptide", "cyclic-peptide", "protein-complex", "boltz", "structure"],
    requires_network=True,
)
def predict_cyclic_peptide_structure(
    peptide_sequence: str,
    target_sequence: str = "",
    cyclic: bool = True,
    output_dir: str = "",
    diffusion_samples: int = 5,
    accelerator: Literal["cpu", "gpu"] = "cpu",
    use_msa_server: bool = True,
) -> str:
    boltz = shutil.which("boltz")
    if boltz is None:
        raise ToolError(
            "Boltz is not installed or not on PATH. Install the open-source boltz package.",
            error_type="unavailable",
            retriable=False,
            backend="Boltz",
        )
    peptide = _protein_sequence(peptide_sequence, "peptide_sequence")
    target = _protein_sequence(target_sequence, "target_sequence") if target_sequence else ""
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    sequences: list[dict[str, object]] = []
    if target:
        sequences.append({"protein": {"id": "A", "sequence": target}})
    peptide_id = "B" if target else "A"
    sequences.append(
        {"protein": {"id": peptide_id, "sequence": peptide, "cyclic": cyclic}}
    )
    input_path = destination / "boltz_input.yaml"
    input_path.write_text(
        yaml.safe_dump({"version": 1, "sequences": sequences}, sort_keys=False),
        encoding="utf-8",
    )

    command = [
        boltz,
        "predict",
        str(input_path),
        "--out_dir",
        str(destination),
        "--diffusion_samples",
        str(diffusion_samples),
        "--output_format",
        "mmcif",
        "--accelerator",
        accelerator,
    ]
    if use_msa_server:
        command.append("--use_msa_server")
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ToolError(
            exc.stderr.strip() or "Boltz prediction failed.",
            error_type="execution_error",
            retriable=False,
            backend="Boltz",
        ) from exc

    structures = sorted(str(path.resolve()) for path in destination.rglob("*.cif"))
    if not structures:
        raise ToolError(
            "Boltz completed without producing an mmCIF structure.",
            error_type="invalid_response",
            retriable=False,
            backend="Boltz",
        )
    return json.dumps(
        {
            "status": "ok",
            "mode": "protein_peptide_complex" if target else "peptide",
            "cyclic": cyclic,
            "input_yaml": str(input_path),
            "structures": structures,
            "log": completed.stdout[-2000:],
        },
        ensure_ascii=False,
        indent=2,
    )


def _protein_sequence(value: str, field: str) -> str:
    sequence = re.sub(r"\s+", "", value).upper()
    if not sequence or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWYX]+", sequence):
        raise ToolError(
            f"{field} must contain only canonical amino-acid letters or X.",
            error_type="invalid_input",
            retriable=False,
            backend="Boltz",
        )
    return sequence
