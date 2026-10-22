"""
DNA/RNA sequence tools: GC content, ORF detection, translation, codon usage.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


# Standard genetic code (NCBI table 1)
CODON_TABLE: dict[str, str] = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}


class GCInput(ToolInput):
    sequence: str = Field(description="DNA or RNA nucleotide sequence")
    window: Optional[int] = Field(default=None, description="Sliding window size for local GC content")


@bioma_tool(
    name="compute_gc_content",
    description="Compute GC content (%) of a DNA/RNA sequence, optionally with sliding window.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=GCInput,
)
def compute_gc_content(sequence: str, window: int | None = None) -> str:
    """Compute overall or windowed GC content."""
    seq = sequence.upper().replace("U", "T").replace(" ", "").replace("\n", "")
    if not seq:
        return "Error: empty sequence"

    gc = sum(1 for c in seq if c in "GC")
    overall = round(100 * gc / len(seq), 2)
    result = f"Overall GC content: {overall}% ({gc}/{len(seq)} bases)"

    if window and window < len(seq):
        windows = []
        for i in range(0, len(seq) - window + 1, window // 2):
            chunk = seq[i:i + window]
            w_gc = sum(1 for c in chunk if c in "GC")
            windows.append(f"pos {i+1}-{i+window}: {round(100*w_gc/window, 1)}%")
        result += f"\n\nSliding window ({window} bp):\n" + "\n".join(windows[:20])
        if len(windows) > 20:
            result += f"\n... ({len(windows)} windows total)"

    return result


class RCInput(ToolInput):
    sequence: str = Field(description="DNA sequence")


@bioma_tool(
    name="reverse_complement",
    description="Return the reverse complement of a DNA sequence.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=RCInput,
)
def reverse_complement(sequence: str) -> str:
    complement = {"A": "T", "T": "A", "G": "C", "C": "G",
                  "a": "t", "t": "a", "g": "c", "c": "g", "N": "N", "n": "n"}
    seq = sequence.replace(" ", "").replace("\n", "")
    rc = "".join(complement.get(b, "N") for b in reversed(seq))
    return rc


class ORFInput(ToolInput):
    sequence: str = Field(description="DNA sequence")
    min_length: Optional[int] = Field(default=100, description="Minimum ORF length in nucleotides")


@bioma_tool(
    name="find_open_reading_frames",
    description="Find all ORFs (ATG to stop codon) in a DNA sequence across all 6 reading frames.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ORFInput,
)
def find_open_reading_frames(sequence: str, min_length: int = 100) -> str:
    """Detect ORFs in all 6 reading frames."""
    import json

    seq = sequence.upper().replace("U", "T").replace(" ", "")
    rc_seq = reverse_complement(seq)
    orfs = []

    for strand, s in [("+", seq), ("-", rc_seq)]:
        for frame in range(3):
            i = frame
            while i < len(s) - 2:
                codon = s[i:i+3]
                if codon == "ATG":
                    j = i + 3
                    while j < len(s) - 2:
                        stop = s[j:j+3]
                        if stop in ("TAA", "TAG", "TGA"):
                            length = j + 3 - i
                            if length >= min_length:
                                orfs.append({
                                    "strand": strand,
                                    "frame": frame + 1,
                                    "start": i + 1,
                                    "end": j + 3,
                                    "length_nt": length,
                                    "length_aa": length // 3 - 1,
                                })
                            break
                        j += 3
                i += 3

    orfs.sort(key=lambda x: x["length_nt"], reverse=True)
    return json.dumps(orfs[:20], indent=2) + f"\n\nTotal ORFs found: {len(orfs)}"


class TranslateInput(ToolInput):
    sequence: str = Field(description="DNA coding sequence starting with ATG")


@bioma_tool(
    name="translate_dna_to_protein",
    description="Translate a DNA coding sequence to protein using the standard genetic code.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=TranslateInput,
)
def translate_dna_to_protein(sequence: str) -> str:
    seq = sequence.upper().replace("U", "T").replace(" ", "")
    protein = []
    for i in range(0, len(seq) - 2, 3):
        codon = seq[i:i+3]
        aa = CODON_TABLE.get(codon, "X")
        if aa == "*":
            break
        protein.append(aa)
    return "".join(protein)


class CodonUsageInput(ToolInput):
    sequence: str = Field(description="CDS DNA sequence")


@bioma_tool(
    name="analyze_codon_usage",
    description="Analyze codon usage frequency in a coding DNA sequence.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=CodonUsageInput,
)
def analyze_codon_usage(sequence: str) -> str:
    """Compute codon usage frequencies."""
    import json
    from collections import Counter

    seq = sequence.upper().replace("U", "T").replace(" ", "")
    codons = [seq[i:i+3] for i in range(0, len(seq) - 2, 3) if len(seq[i:i+3]) == 3]
    counts = Counter(codons)

    # Group by amino acid
    aa_codons: dict[str, dict[str, int]] = {}
    for codon, count in counts.items():
        aa = CODON_TABLE.get(codon, "?")
        aa_codons.setdefault(aa, {})[codon] = count

    result = {"total_codons": len(codons), "by_amino_acid": aa_codons}
    return json.dumps(result, indent=2)
