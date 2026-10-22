"""Biopython sequence operations extracted from the Skill examples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class BiopythonSequenceInput(ToolInput):
    operation: Literal["summary", "reverse_complement", "translate"] = Field(description="Sequence operation")
    sequence: str = Field(default="", description="Raw nucleotide/protein sequence")
    fasta_path: str = Field(default="", description="Optional FASTA input")
    molecule: Literal["dna", "rna", "protein"] = Field(default="dna", description="Molecule type")
    genetic_code: int = Field(default=1, ge=1, le=33, description="NCBI translation table")
    to_stop: bool = Field(default=False, description="Stop translation at first stop codon")


@bioma_tool(
    name="sciagent_analyze_biopython_sequence",
    description="Summarize, reverse-complement, or translate nucleotide/protein sequences with Biopython.",
    category="sequence-analysis",
    author="SciAgent",
    args_schema=BiopythonSequenceInput,
)
def analyze_biopython_sequence(operation: str, sequence: str = "", fasta_path: str = "",
                               molecule: str = "dna", genetic_code: int = 1,
                               to_stop: bool = False) -> str:
    try:
        from Bio import SeqIO
        from Bio.Seq import Seq
        from Bio.SeqUtils import gc_fraction, molecular_weight
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install biopython") from exc
    records: list[tuple[str, str]] = []
    if fasta_path:
        path = Path(fasta_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        records = [(record.id, str(record.seq)) for record in SeqIO.parse(path, "fasta")]
    else:
        clean = "".join(sequence.split()).upper()
        records = [("sequence", clean)] if clean else []
    if not records:
        raise ValueError("No sequence records were provided")
    results = []
    for identifier, raw in records:
        seq = Seq(raw)
        if operation == "reverse_complement":
            value: object = str(seq.reverse_complement())
        elif operation == "translate":
            if molecule == "protein":
                raise ValueError("translate requires a DNA or RNA sequence")
            value = str(seq.translate(table=genetic_code, to_stop=to_stop))
        else:
            info: dict[str, object] = {"length": len(seq)}
            if molecule in {"dna", "rna"}:
                info["gc_fraction"] = gc_fraction(seq)
                info["base_counts"] = {base: int(raw.upper().count(base)) for base in "ACGTUN"}
            try:
                info["molecular_weight_da"] = molecular_weight(
                    str(seq), seq_type={"dna": "DNA", "rna": "RNA", "protein": "protein"}[molecule]
                )
            except ValueError as exc:
                info["molecular_weight_error"] = str(exc)
            value = info
        results.append({"id": identifier, "result": value})
    return json.dumps({"operation": operation, "molecule": molecule, "records": results}, ensure_ascii=False)
