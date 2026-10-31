"""Only load domain contracts; do not initialize the old Agent runtime."""
from pathlib import Path
from bioma_peptide_bridge.catalog import source_files
from bioma_peptide_bridge.contracts import sha256
from bioma_peptide_bridge.domain import load_tools
ROOT=Path(__file__).resolve().parents[3]
load_tools(ROOT, {p:sha256(ROOT/p) for p in source_files(ROOT)})
