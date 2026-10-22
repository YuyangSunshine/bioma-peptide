"""BioAgent wrapper for applying VCF variants to a reference FASTA."""
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from bioma.tools.base import ToolInput, bioma_tool
from skills._meta.support.clawbio._bioma_cli import existing_file, output_directory

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "generate_consensus.sh"

class ConsensusSequenceInput(ToolInput):
    reference_fasta_path: str
    vcf_path: str
    output_dir: str
    sample: str = ""

@bioma_tool(name="openclaw_generate_consensus_sequence", description="Apply indexed VCF variants to a reference FASTA with bcftools consensus.", category="genomics", author="OpenClaw", args_schema=ConsensusSequenceInput)
def generate_consensus_sequence(reference_fasta_path: str, vcf_path: str, output_dir: str, sample: str = "") -> str:
    reference = existing_file(reference_fasta_path, "reference_fasta_path")
    variants = existing_file(vcf_path, "vcf_path")
    output = output_directory(output_dir)
    consensus = output / "consensus.fasta"
    command = ["bash", str(SCRIPT), str(reference), str(variants), str(consensus)]
    if sample:
        command.append(sample)
    environment = os.environ.copy()
    process = subprocess.run(command, text=True, capture_output=True, timeout=43200, env=environment)
    if process.returncode:
        detail = "\n".join(part for part in (process.stdout, process.stderr) if part)
        raise RuntimeError(f"consensus generation failed: {detail[-16000:]}")
    return json.dumps({"output_dir": str(output), "files": [consensus.name], "stdout_tail": process.stdout[-4000:], "stderr_tail": process.stderr[-2000:]}, ensure_ascii=False)
