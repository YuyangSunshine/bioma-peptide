"""BioAgent wrapper for ESMFold structure prediction."""
from pathlib import Path
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import output_directory, run_cli

SCRIPT = Path(__file__).resolve().parents[1] / "esmfold_client.py"

class EsmfoldInput(ToolInput):
    amino_acid_sequence: str
    output_dir: str

@bioma_tool(name="openclaw_predict_protein_structure", description="Run the bundled ESMFold demonstration mock and save its synthetic PDB output; do not use it as a scientific structure prediction.", category="structural-biology", author="OpenClaw", args_schema=EsmfoldInput)
def predict_protein_structure(amino_acid_sequence: str, output_dir: str) -> str:
    sequence = "".join(amino_acid_sequence.split()).upper()
    allowed = set("ACDEFGHIKLMNPQRSTVWYBXZJUO")
    if not sequence or set(sequence) - allowed:
        raise ValueError("amino_acid_sequence contains invalid residue symbols")
    output = output_directory(output_dir)
    return run_cli(SCRIPT, ["--sequence", sequence, "--output", str(output / "predicted_structure.pdb")], output, timeout=86400)
