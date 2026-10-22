"""Pairwise and multiple-sequence alignment operations for the alignment Skill."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class SequenceAlignmentInput(ToolInput):
    operation: Literal["pairwise", "multiple", "summary"] = Field(
        description="Run a pairwise alignment, create a multiple alignment, or summarize an existing alignment"
    )
    sequences: list[str] = Field(
        default_factory=list,
        description="Two raw sequences for pairwise alignment; ignored when input_path is used",
    )
    input_path: str = Field(default="", description="FASTA input for pairwise/multiple or an existing alignment for summary")
    input_format: str = Field(default="fasta", description="Bio.AlignIO format for summary input")
    output_path: str = Field(default="", description="Required for MSA; optional full text output for pairwise alignment")
    molecule: Literal["dna", "protein"] = "dna"
    mode: Literal["global", "local"] = "global"
    program: Literal["auto", "mafft", "muscle"] = "auto"
    substitution_matrix: str = Field(default="", description="Optional Biopython matrix name such as BLOSUM62")
    match_score: float = 2.0
    mismatch_score: float = -1.0
    gap_open_score: float = -5.0
    gap_extend_score: float = -0.5
    threads: int = Field(default=4, ge=1, le=128)


def _read_fasta(path: Path) -> list[tuple[str, str]]:
    from Bio import SeqIO

    records = [(record.id, str(record.seq)) for record in SeqIO.parse(path, "fasta")]
    if not records:
        raise ValueError(f"No FASTA records found in {path}")
    return records


@bioma_tool(
    name="bio_sequence_alignment",
    description="Align two DNA/protein sequences, create an MSA with MAFFT/MUSCLE, or summarize an alignment.",
    category="sequence-alignment",
    author="BioAgent",
    args_schema=SequenceAlignmentInput,
)
def sequence_alignment(
    operation: str,
    sequences: list[str] | None = None,
    input_path: str = "",
    input_format: str = "fasta",
    output_path: str = "",
    molecule: str = "dna",
    mode: str = "global",
    program: str = "auto",
    substitution_matrix: str = "",
    match_score: float = 2.0,
    mismatch_score: float = -1.0,
    gap_open_score: float = -5.0,
    gap_extend_score: float = -0.5,
    threads: int = 4,
) -> str:
    source = Path(input_path).expanduser().resolve() if input_path else None
    if source and not source.is_file():
        raise FileNotFoundError(source)

    if operation == "pairwise":
        from Bio.Align import PairwiseAligner, substitution_matrices

        records = _read_fasta(source) if source else [
            (f"sequence_{index + 1}", "".join(value.split()).upper())
            for index, value in enumerate(sequences or [])
        ]
        if len(records) != 2 or any(not value for _, value in records):
            raise ValueError("Pairwise alignment requires exactly two non-empty sequences")
        aligner = PairwiseAligner(mode=mode)
        if substitution_matrix or molecule == "protein":
            aligner.substitution_matrix = substitution_matrices.load(substitution_matrix or "BLOSUM62")
        else:
            aligner.match_score = match_score
            aligner.mismatch_score = mismatch_score
        aligner.open_gap_score = gap_open_score
        aligner.extend_gap_score = gap_extend_score
        alignments = aligner.align(records[0][1], records[1][1])
        try:
            best = alignments[0]
        except IndexError:
            raise RuntimeError("Biopython did not produce an alignment")
        alignment_text = str(best)
        saved_alignment = None
        if output_path:
            destination = Path(output_path).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(alignment_text, encoding="utf-8")
            saved_alignment = str(destination)
        return json.dumps(
            {
                "operation": operation,
                "mode": mode,
                "molecule": molecule,
                "ids": [records[0][0], records[1][0]],
                "score": float(best.score),
                "coordinates": best.coordinates.tolist(),
                "alignment": alignment_text if len(alignment_text) <= 12000 else alignment_text[:12000],
                "alignment_truncated": len(alignment_text) > 12000,
                "output_path": saved_alignment,
            },
            ensure_ascii=False,
        )

    if operation == "multiple":
        if source is None:
            raise ValueError("input_path is required for multiple alignment")
        if len(_read_fasta(source)) < 2:
            raise ValueError("Multiple alignment requires at least two FASTA records")
        if not output_path:
            raise ValueError("output_path is required for multiple alignment")
        destination = Path(output_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        selected = program
        if selected == "auto":
            selected = "mafft" if shutil.which("mafft") else "muscle"
        executable = shutil.which(selected)
        if not executable:
            raise RuntimeError(f"Missing executable: install {selected}")
        if selected == "mafft":
            process = subprocess.run(
                [executable, "--auto", "--thread", str(threads), str(source)],
                text=True,
                capture_output=True,
                timeout=3600,
            )
            if process.returncode:
                raise RuntimeError(f"MAFFT failed: {process.stderr[-8000:]}")
            destination.write_text(process.stdout, encoding="utf-8")
        else:
            version = subprocess.run([executable, "-version"], text=True, capture_output=True).stdout
            command = (
                [executable, "-align", str(source), "-output", str(destination)]
                if "MUSCLE v5" in version
                else [executable, "-in", str(source), "-out", str(destination)]
            )
            process = subprocess.run(command, text=True, capture_output=True, timeout=3600)
            if process.returncode:
                raise RuntimeError(f"MUSCLE failed: {(process.stderr or process.stdout)[-8000:]}")
        summary = json.loads(sequence_alignment(operation="summary", input_path=str(destination), input_format="fasta"))
        summary.update({"operation": operation, "program": selected, "output_path": str(destination)})
        return json.dumps(summary, ensure_ascii=False)

    if source is None:
        raise ValueError("input_path is required for alignment summary")
    from Bio import AlignIO

    alignment = AlignIO.read(str(source), input_format)
    sequence_count, alignment_length = len(alignment), alignment.get_alignment_length()
    if not sequence_count or not alignment_length:
        raise ValueError("Alignment is empty")
    strings = [str(record.seq).upper() for record in alignment]
    gap_count = sum(value.count("-") + value.count(".") for value in strings)
    identities: list[float] = []
    for left in range(sequence_count):
        for right in range(left + 1, sequence_count):
            comparable = [(a, b) for a, b in zip(strings[left], strings[right]) if a not in "-." and b not in "-."]
            if comparable:
                identities.append(sum(a == b for a, b in comparable) / len(comparable))
    return json.dumps(
        {
            "operation": operation,
            "input_path": str(source),
            "format": input_format,
            "sequence_count": sequence_count,
            "alignment_length": alignment_length,
            "gap_fraction": gap_count / (sequence_count * alignment_length),
            "mean_pairwise_identity": sum(identities) / len(identities) if identities else None,
            "ids": [record.id for record in alignment],
        },
        ensure_ascii=False,
    )
