"""Deterministic protein sequence analysis tools with reproducible outputs."""

from __future__ import annotations

import hashlib

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class ProteinSequenceAnalysisInput(ToolInput):
    sequence: str = Field(description="Protein amino acid sequence in one-letter code.")
    sequence_id: str = Field(
        default="query-sequence",
        description="Stable identifier written into the calculation result.",
    )


def validate_sequence_descriptors(result, arguments):
    """Independent input/count/range checks for the execution receipt."""
    import math
    sequence = ''.join(arguments['sequence'].split()).upper()
    return (isinstance(result, dict) and result.get('success') is True
        and result.get('sequence_sha256') == hashlib.sha256(sequence.encode('ascii')).hexdigest()
        and result.get('length_aa') == len(sequence)
        and sum(result.get('amino_acid_counts', {}).values()) == len(sequence)
        and math.isfinite(result.get('molecular_weight_da', float('nan')))
        and result['molecular_weight_da'] > 0)


@bioma_tool(
    name="analyze_protein_sequence",
    description=(
        "Calculate reproducible protein sequence descriptors with Biopython: length, "
        "molecular weight, aromaticity, instability index, isoelectric point, GRAVY, "
        "secondary-structure fractions, extinction coefficients, and composition."
    ),
    category="analysis",
    version="1.0.0",
    author="BioMA",
    args_schema=ProteinSequenceAnalysisInput,
    tags=["protein", "sequence", "physicochemical", "biopython"],
    result_kind="calculation",
    result_validator=validate_sequence_descriptors,
)
def analyze_protein_sequence(
    sequence: str,
    sequence_id: str = "query-sequence",
) -> dict:
    """Return directly computed physicochemical descriptors and provenance."""
    from Bio import __version__ as biopython_version
    from Bio.SeqUtils.ProtParam import ProteinAnalysis

    normalized = "".join(sequence.split()).upper()
    valid = set("ACDEFGHIKLMNPQRSTVWY")
    invalid = sorted(set(normalized) - valid)
    if not normalized:
        raise ValueError("Protein sequence is empty.")
    if invalid:
        raise ValueError(f"Protein sequence contains invalid residues: {invalid}")

    analysis = ProteinAnalysis(normalized)
    counts = analysis.count_amino_acids()
    length = len(normalized)
    helix, turn, sheet = analysis.secondary_structure_fraction()
    reduced, oxidized = analysis.molar_extinction_coefficient()
    return {
        "success": True,
        "sequence_id": sequence_id,
        "sequence": normalized,
        "sequence_sha256": hashlib.sha256(normalized.encode("ascii")).hexdigest(),
        "method": "Bio.SeqUtils.ProtParam.ProteinAnalysis",
        "biopython_version": biopython_version,
        "length_aa": length,
        "molecular_weight_da": round(analysis.molecular_weight(), 3),
        "aromaticity": round(analysis.aromaticity(), 6),
        "instability_index": round(analysis.instability_index(), 6),
        "isoelectric_point": round(analysis.isoelectric_point(), 6),
        "gravy": round(analysis.gravy(), 6),
        "secondary_structure_fraction": {
            "helix": round(helix, 6),
            "turn": round(turn, 6),
            "sheet": round(sheet, 6),
        },
        "molar_extinction_coefficient": {
            "reduced_cysteines": reduced,
            "cystines": oxidized,
        },
        "amino_acid_counts": counts,
        "amino_acid_fraction": {
            residue: round(count / length, 6) for residue, count in counts.items()
        },
    }
