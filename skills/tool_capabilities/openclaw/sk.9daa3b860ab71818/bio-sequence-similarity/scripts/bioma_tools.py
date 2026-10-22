"""Local iterative and profile-based sequence similarity searches."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class SequenceSimilarityInput(ToolInput):
    operation: Literal["psiblast", "jackhmmer", "hmmsearch", "reciprocal_blast"] = Field(
        description="Similarity workflow to execute"
    )
    query_path: str = Field(description="Query FASTA, or HMM profile for hmmsearch")
    database_path: str = Field(description="BLAST database prefix or target FASTA")
    output_path: str = Field(description="Tabular output path")
    molecule: Literal["protein", "dna"] = "protein"
    evalue: float = Field(default=1e-5, gt=0)
    iterations: int = Field(default=3, ge=1, le=20)
    max_hits: int = Field(default=100, ge=1, le=100000)
    threads: int = Field(default=4, ge=1, le=128)


def _executable(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"Missing executable: install {name}")
    return value


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, text=True, capture_output=True, timeout=7200)
    if process.returncode:
        raise RuntimeError(f"{label} failed: {(process.stderr or process.stdout)[-12000:]}")
    return process


@bioma_tool(
    name="bio_sequence_similarity_search",
    description="Run local PSI-BLAST, JackHMMER, HMMER profile search, or reciprocal-best-hit analysis.",
    category="sequence-similarity",
    author="BioAgent",
    args_schema=SequenceSimilarityInput,
)
def sequence_similarity_search(
    operation: str,
    query_path: str,
    database_path: str,
    output_path: str,
    molecule: str = "protein",
    evalue: float = 1e-5,
    iterations: int = 3,
    max_hits: int = 100,
    threads: int = 4,
) -> str:
    query = Path(query_path).expanduser().resolve()
    database = Path(database_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not query.is_file():
        raise FileNotFoundError(query)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if operation == "psiblast":
        if molecule != "protein":
            raise ValueError("PSI-BLAST accepts protein queries; use local BLAST for DNA")
        if not database.exists() and not list(database.parent.glob(database.name + ".*")):
            raise FileNotFoundError(f"BLAST database prefix not found: {database}")
        fields = "qseqid sseqid pident length qlen slen evalue bitscore"
        command = [
            _executable("psiblast"), "-query", str(query), "-db", str(database),
            "-out", str(destination), "-outfmt", f"6 {fields}", "-evalue", str(evalue),
            "-num_iterations", str(iterations), "-num_threads", str(threads),
            "-max_target_seqs", str(max_hits),
        ]
        process = _run(command, "PSI-BLAST")
        hit_count = sum(1 for line in destination.read_text(encoding="utf-8").splitlines() if line.strip())
        return json.dumps({"operation": operation, "output_path": str(destination), "hit_count": hit_count,
                           "stderr_tail": process.stderr[-2000:]})

    if not database.is_file():
        raise FileNotFoundError(database)
    if operation in {"jackhmmer", "hmmsearch"}:
        executable = _executable(operation)
        command = [executable, "--noali", "--tblout", str(destination), "-E", str(evalue), "--cpu", str(threads)]
        if operation == "jackhmmer":
            command.extend(["-N", str(iterations)])
        command.extend([str(query), str(database)])
        process = _run(command, operation)
        hit_count = sum(
            1 for line in destination.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
        return json.dumps({"operation": operation, "output_path": str(destination), "hit_count": hit_count,
                           "stdout_tail": process.stdout[-2000:], "stderr_tail": process.stderr[-2000:]})

    makeblastdb = _executable("makeblastdb")
    program = _executable("blastp" if molecule == "protein" else "blastn")
    dbtype = "prot" if molecule == "protein" else "nucl"
    fields = ["qseqid", "sseqid", "pident", "length", "qlen", "slen", "evalue", "bitscore"]
    with tempfile.TemporaryDirectory(prefix="bioagent-rbh-") as temp_dir:
        temp = Path(temp_dir)
        query_db, target_db = temp / "query", temp / "target"
        _run([makeblastdb, "-in", str(query), "-dbtype", dbtype, "-out", str(query_db)], "makeblastdb query")
        _run([makeblastdb, "-in", str(database), "-dbtype", dbtype, "-out", str(target_db)], "makeblastdb target")
        forward_path, reverse_path = temp / "forward.tsv", temp / "reverse.tsv"
        common = ["-outfmt", "6 " + " ".join(fields), "-evalue", str(evalue),
                  "-max_target_seqs", str(max_hits), "-num_threads", str(threads)]
        _run([program, "-query", str(query), "-db", str(target_db), "-out", str(forward_path), *common], "forward BLAST")
        _run([program, "-query", str(database), "-db", str(query_db), "-out", str(reverse_path), *common], "reverse BLAST")

        def best_hits(path: Path) -> dict[str, list[str]]:
            best: dict[str, list[str]] = {}
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.reader(handle, delimiter="\t"):
                    if len(row) == len(fields) and (row[0] not in best or float(row[-1]) > float(best[row[0]][-1])):
                        best[row[0]] = row
            return best

        forward, reverse = best_hits(forward_path), best_hits(reverse_path)
        reciprocal = [row for query_id, row in forward.items() if row[1] in reverse and reverse[row[1]][1] == query_id]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(reciprocal)
    return json.dumps({"operation": operation, "output_path": str(destination),
                       "reciprocal_pair_count": len(reciprocal), "molecule": molecule})
