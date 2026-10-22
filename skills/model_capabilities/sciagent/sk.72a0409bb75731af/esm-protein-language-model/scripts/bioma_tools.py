"""Local ESM C and ESM3 operations extracted from the Skill examples."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class EsmInput(ToolInput):
    operation: Literal["embeddings", "generate_sequence", "predict_structure", "inverse_fold"] = Field(
        description="ESM operation"
    )
    sequences: list[str] = Field(default_factory=list, description="Protein sequences or one masked generation prompt")
    pdb_path: str = Field(default="", description="Input PDB for inverse_fold")
    output_path: str = Field(description="NPY output for embeddings or PDB/FASTA output for generation")
    model_name: str = Field(default="", description="Optional ESM model name; operation-specific default is used")
    num_steps: int = Field(default=16, ge=1, le=100, description="ESM3 generation steps")
    temperature: float = Field(default=0.7, ge=0, le=2, description="ESM3 generation temperature")
    num_designs: int = Field(default=1, ge=1, le=20, description="Number of inverse-folding designs")


def _validate_sequences(sequences: list[str], allow_mask: bool = False) -> list[str]:
    if not sequences or len(sequences) > 32:
        raise ValueError("sequences must contain between 1 and 32 entries")
    alphabet = set("ACDEFGHIKLMNPQRSTVWY") | ({"_"} if allow_mask else set())
    cleaned = [sequence.strip().upper() for sequence in sequences]
    for sequence in cleaned:
        invalid = sorted(set(sequence) - alphabet)
        if not sequence or invalid:
            raise ValueError(f"Invalid protein sequence characters: {''.join(invalid)}")
        if len(sequence) > 4096:
            raise ValueError("Sequence length is limited to 4,096 residues")
    return cleaned


@bioma_tool(
    name="sciagent_run_esm",
    description="Run local ESM C embeddings or ESM3 sequence, structure, and inverse-folding generation, saving large outputs to files.",
    category="protein-design",
    author="SciAgent",
    args_schema=EsmInput,
)
def run_esm(
    operation: str,
    output_path: str,
    sequences: list[str] | None = None,
    pdb_path: str = "",
    model_name: str = "",
    num_steps: int = 16,
    temperature: float = 0.7,
    num_designs: int = 1,
) -> str:
    values = sequences or []
    if operation == "generate_sequence":
        _validate_sequences(values, allow_mask=True)
    elif operation != "inverse_fold":
        _validate_sequences(values)
    default_python = Path(sys.prefix).parent / "bioagent_esm" / "bin" / "python"
    python = Path(os.getenv("BIOMA_ESM_PYTHON", str(default_python))).expanduser().absolute()
    if not python.is_file():
        raise RuntimeError(
            "Missing isolated ESM environment; set BIOMA_ESM_PYTHON to its Python executable"
        )
    runner = Path(__file__).with_name("esm_runner.py")
    payload = {
        "operation": operation,
        "output_path": output_path,
        "sequences": values,
        "pdb_path": pdb_path,
        "model_name": model_name,
        "num_steps": num_steps,
        "temperature": temperature,
        "num_designs": num_designs,
    }
    process = subprocess.run(
        [str(python), str(runner)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"ESM runner failed: {detail[-8000:]}")
    return process.stdout.strip()
