"""
Document parsing tools: FASTA, PDB, CSV, and PDF file parsers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class FastaInput(ToolInput):
    file_path: str = Field(description="Path to FASTA file")
    max_sequences: Optional[int] = Field(default=50, description="Maximum sequences to return")


@bioma_tool(
    name="parse_fasta_file",
    description="Parse a FASTA file and return a list of sequence IDs and sequences.",
    category="file",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=FastaInput,
)
def parse_fasta_file(file_path: str, max_sequences: int = 50) -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    sequences = []
    current_id = None
    current_seq: list[str] = []

    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if current_id:
                sequences.append({"id": current_id, "sequence": "".join(current_seq),
                                   "length": len("".join(current_seq))})
            current_id = line[1:].split()[0]
            current_seq = []
        else:
            current_seq.append(line)

    if current_id:
        sequences.append({"id": current_id, "sequence": "".join(current_seq),
                          "length": len("".join(current_seq))})

    total = len(sequences)
    sequences = sequences[:max_sequences]
    result = {"total_sequences": total, "returned": len(sequences), "sequences": sequences}
    return json.dumps(result, indent=2)


class PDBInput(ToolInput):
    file_path: str = Field(description="Path to PDB file")


@bioma_tool(
    name="parse_pdb_file",
    description="Parse a PDB file and return chain info, sequence, and basic statistics.",
    category="file",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=PDBInput,
)
def parse_pdb_file(file_path: str) -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    chains: dict[str, list[str]] = {}
    ligands: list[str] = []
    resolution = None

    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("REMARK   2 RESOLUTION"):
            try:
                resolution = float(line.split()[-2])
            except (ValueError, IndexError):
                pass
        elif line.startswith("ATOM  ") or line.startswith("HETATM"):
            chain_id = line[21]
            if line.startswith("HETATM"):
                res_name = line[17:20].strip()
                if res_name not in ("HOH", "WAT") and res_name not in ligands:
                    ligands.append(res_name)
            else:
                chains.setdefault(chain_id, [])

    result = {
        "chains": {k: f"{len(v)} residues (approx)" for k, v in chains.items()},
        "ligands": ligands[:10],
        "resolution_angstrom": resolution,
        "file": path.name,
    }
    return json.dumps(result, indent=2)


class CSVInput(ToolInput):
    file_path: str = Field(description="Path to CSV or TSV file")
    max_rows: Optional[int] = Field(default=5, description="Number of preview rows")


@bioma_tool(
    name="parse_csv_data",
    description="Parse a CSV/TSV file and return column names, shape, and preview rows.",
    category="file",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=CSVInput,
)
def parse_csv_data(file_path: str, max_rows: int = 5) -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    try:
        import csv
        sep = "\t" if path.suffix.lower() == ".tsv" else ","
        with open(path, newline="", errors="replace") as f:
            reader = csv.DictReader(f, delimiter=sep)
            rows = list(reader)

        columns = rows[0].keys() if rows else []
        preview = rows[:max_rows]
        result = {
            "file": path.name,
            "total_rows": len(rows),
            "columns": list(columns),
            "preview": preview,
        }
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as exc:
        return f"Error parsing CSV: {exc}"


class PDFInput(ToolInput):
    file_path: str = Field(description="Path to PDF file")
    max_chars: Optional[int] = Field(default=3000, description="Maximum characters to extract")


@bioma_tool(
    name="extract_text_from_pdf",
    description="Extract text content from a PDF file (scientific paper, report, etc.).",
    category="file",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=PDFInput,
)
def extract_text_from_pdf(file_path: str, max_chars: int = 3000) -> str:
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"

    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        text_parts = []
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
        full_text = "\n".join(text_parts)
        truncated = full_text[:max_chars]
        if len(full_text) > max_chars:
            truncated += f"\n\n[... truncated. Total: {len(full_text)} chars across {len(reader.pages)} pages]"
        return truncated
    except ImportError:
        return "pypdf not installed. Install with: pip install pypdf"
    except Exception as exc:
        return f"Error extracting PDF: {exc}"
