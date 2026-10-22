"""RNA secondary structure and duplex prediction through ViennaRNA CLI tools."""
from __future__ import annotations
import json, re, shutil, subprocess
from pathlib import Path
from typing import Literal
from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool

class ViennaRNAInput(ToolInput):
    operation: Literal["fold", "duplex"]
    sequence: str = ""
    second_sequence: str = ""
    fasta_path: str = ""
    output_path: str = ""
    temperature_c: float = Field(default=37.0, ge=0.0, le=100.0)

@bioma_tool(name="sciagent_predict_rna_structure", description="Predict RNA minimum-free-energy structures or RNA-RNA duplexes with ViennaRNA.", category="rna-structure", author="SciAgent", args_schema=ViennaRNAInput)
def predict_rna_structure(operation: str, sequence: str = "", second_sequence: str = "", fasta_path: str = "",
                          output_path: str = "", temperature_c: float = 37.0) -> str:
    if fasta_path and sequence: raise ValueError("Provide fasta_path or sequence, not both")
    if fasta_path:
        source = Path(fasta_path).expanduser().resolve()
        if not source.is_file(): raise FileNotFoundError(source)
        input_text = source.read_text(encoding="utf-8")
    else:
        clean = "".join(sequence.split()).upper().replace("T", "U")
        if not clean or set(clean) - set("ACGUN"): raise ValueError("sequence must contain RNA/DNA bases A/C/G/U/T/N")
        input_text = clean + "\n"
    executable = shutil.which("RNAfold" if operation == "fold" else "RNAduplex")
    if not executable: raise RuntimeError("Missing ViennaRNA executable")
    if operation == "duplex":
        second = "".join(second_sequence.split()).upper().replace("T", "U")
        if fasta_path or not second or set(second)-set("ACGUN"): raise ValueError("duplex requires two raw RNA sequences")
        input_text = input_text.strip() + "\n" + second + "\n"
    process = subprocess.run([executable, "--noPS", "--temp", str(temperature_c)] if operation == "fold" else [executable, "--temp", str(temperature_c)], input=input_text, text=True, capture_output=True, timeout=3600)
    if process.returncode: raise RuntimeError(f"ViennaRNA failed: {(process.stderr or process.stdout)[-8000:]}")
    if output_path:
        destination = Path(output_path).expanduser().resolve(); destination.parent.mkdir(parents=True, exist_ok=True); destination.write_text(process.stdout, encoding="utf-8")
    energies = [float(value) for value in re.findall(r"\(\s*(-?\d+(?:\.\d+)?)\s*\)", process.stdout)]
    return json.dumps({"operation": operation, "output_path": str(Path(output_path).expanduser().resolve()) if output_path else None,
                       "mfe_values_kcal_mol": energies, "output": process.stdout[-16000:]})
