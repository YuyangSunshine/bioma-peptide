"""CNVkit operations extracted from the Skill workflow."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class CnvkitInput(ToolInput):
    operation: Literal["batch", "segment", "call", "export"] = Field(description="CNVkit operation")
    input_path: str = Field(description="Tumor BAM for batch, CNR for segment, or CNS for call/export")
    output_path: str = Field(description="Output directory for batch or output file otherwise")
    normal_bam: str = Field(default="", description="Optional matched normal BAM for batch")
    targets_bed: str = Field(default="", description="Capture targets BED for batch")
    reference_fasta: str = Field(default="", description="Reference FASTA for batch")
    method: Literal["hybrid", "amplicon", "wgs", "cbs", "hmm"] = Field(default="hybrid", description="Batch or segmentation method")
    ploidy: int = Field(default=2, ge=1, le=8, description="Tumor ploidy for call")
    purity: float = Field(default=0.0, ge=0, le=1, description="Tumor purity; 0 leaves unspecified")
    export_format: Literal["vcf", "seg", "bed"] = Field(default="seg", description="Export format")
    threads: int = Field(default=4, ge=1, le=64, description="Worker processes for batch")


@bioma_tool(
    name="sciagent_run_cnvkit",
    description="Run constrained CNVkit batch, segment, call, or export operations for somatic copy-number analysis.",
    category="genomics",
    author="SciAgent",
    args_schema=CnvkitInput,
)
def run_cnvkit(operation: str, input_path: str, output_path: str,
               normal_bam: str = "", targets_bed: str = "", reference_fasta: str = "",
               method: str = "hybrid", ploidy: int = 2, purity: float = 0.0,
               export_format: str = "seg", threads: int = 4) -> str:
    executable = shutil.which("cnvkit.py")
    if not executable:
        raise RuntimeError("Missing executable: install cnvkit")
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    output = Path(output_path).expanduser().resolve()
    if operation == "batch":
        targets = Path(targets_bed).expanduser().resolve()
        fasta = Path(reference_fasta).expanduser().resolve()
        if not targets.is_file() or not fasta.is_file():
            raise ValueError("targets_bed and reference_fasta are required for batch")
        output.mkdir(parents=True, exist_ok=True)
        command = [executable, "batch", str(source), "--targets", str(targets),
                   "--fasta", str(fasta), "--output-dir", str(output),
                   "--method", method, "--processes", str(threads), "--diagram", "--scatter"]
        if normal_bam:
            normal = Path(normal_bam).expanduser().resolve()
            if not normal.is_file():
                raise FileNotFoundError(normal)
            command.extend(["--normal", str(normal)])
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        if operation == "segment":
            if method not in {"cbs", "hmm"}:
                raise ValueError("segment method must be cbs or hmm")
            command = [executable, "segment", str(source), "-o", str(output), "--method", method]
        elif operation == "call":
            command = [executable, "call", str(source), "-o", str(output), "--ploidy", str(ploidy)]
            if purity:
                command.extend(["--purity", str(purity)])
        else:
            command = [executable, "export", export_format, str(source), "-o", str(output)]
    process = subprocess.run(command, check=True, text=True, capture_output=True)
    return json.dumps({"operation": operation, "output": str(output),
                       "stdout_tail": process.stdout[-4000:], "stderr_tail": process.stderr[-4000:]},
                      ensure_ascii=False)
