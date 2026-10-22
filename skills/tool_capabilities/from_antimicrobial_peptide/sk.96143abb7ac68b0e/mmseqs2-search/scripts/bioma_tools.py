"""MMseqs2 search and clustering workflows exposed as BioAgent tools."""

from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class MMseqs2Input(ToolInput):
    operation: Literal["search", "cluster"] = Field(description="Search a target FASTA or cluster the query FASTA")
    query_fasta: str = Field(description="Query sequences in FASTA format")
    target_fasta: str = Field(default="", description="Target FASTA required for search")
    output_path: str = Field(description="Search TSV path or cluster output prefix")
    temporary_dir: str = Field(default="", description="Optional MMseqs2 temporary directory")
    evalue: float = Field(default=1e-3, gt=0)
    minimum_identity: float = Field(default=0.0, ge=0.0, le=1.0)
    minimum_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    coverage_mode: int = Field(default=0, ge=0, le=5)
    sensitivity: float = Field(default=5.7, ge=1.0, le=7.5)
    threads: int = Field(default=4, ge=1, le=128)


@bioma_tool(
    name="sciagent_run_mmseqs2",
    description="Search FASTA sequences or cluster them by identity and coverage with MMseqs2.",
    category="sequence-similarity",
    author="BioAgent Community",
    args_schema=MMseqs2Input,
)
def run_mmseqs2(
    operation: str,
    query_fasta: str,
    output_path: str,
    target_fasta: str = "",
    temporary_dir: str = "",
    evalue: float = 1e-3,
    minimum_identity: float = 0.0,
    minimum_coverage: float = 0.0,
    coverage_mode: int = 0,
    sensitivity: float = 5.7,
    threads: int = 4,
) -> str:
    executable = shutil.which("mmseqs")
    if not executable:
        raise RuntimeError("Missing executable: install mmseqs2")
    query = Path(query_fasta).expanduser().resolve()
    if not query.is_file():
        raise FileNotFoundError(query)
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = Path(target_fasta).expanduser().resolve() if target_fasta else None
    if operation == "search" and (target is None or not target.is_file()):
        raise ValueError("target_fasta must be an existing FASTA file for search")

    if temporary_dir:
        temp_path = Path(temporary_dir).expanduser().resolve()
        temp_path.mkdir(parents=True, exist_ok=True)
        context = nullcontext(str(temp_path))
    else:
        context = tempfile.TemporaryDirectory(prefix="bioagent-mmseqs-")

    with context as temp_dir:
        common = [
            "--min-seq-id", str(minimum_identity), "-c", str(minimum_coverage),
            "--cov-mode", str(coverage_mode), "--threads", str(threads),
        ]
        if operation == "search":
            fields = "query,target,pident,alnlen,mismatch,gapopen,qstart,qend,tstart,tend,evalue,bits"
            command = [
                executable, "easy-search", str(query), str(target), str(destination), str(temp_dir),
                "-e", str(evalue), "-s", str(sensitivity), "--format-output", fields, *common,
            ]
        else:
            command = [executable, "easy-cluster", str(query), str(destination), str(temp_dir), *common]
        process = subprocess.run(command, text=True, capture_output=True, timeout=14400)
    if process.returncode:
        raise RuntimeError(f"MMseqs2 {operation} failed: {(process.stderr or process.stdout)[-12000:]}")
    if operation == "search":
        hit_count = sum(1 for line in destination.read_text(encoding="utf-8").splitlines() if line.strip())
        outputs = {"result_tsv": str(destination), "hit_count": hit_count}
    else:
        outputs = {
            "cluster_tsv": str(destination) + "_cluster.tsv",
            "representative_fasta": str(destination) + "_rep_seq.fasta",
            "all_sequences_fasta": str(destination) + "_all_seqs.fasta",
        }
        cluster_path = Path(outputs["cluster_tsv"])
        outputs["cluster_count"] = len({
            line.split("\t", 1)[0]
            for line in cluster_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }) if cluster_path.is_file() else None
    return json.dumps({"operation": operation, **outputs, "stderr_tail": process.stderr[-3000:]})
