"""Unified public collection of the six crosslinked-peptide structure tools."""

import importlib.util
import sys
from pathlib import Path


def _load_private_module(module_name: str):
    path = Path(__file__).with_name("_crosslinked_impl") / f"{module_name}.py"
    qualified_name = f"bioma_crosslinked_impl_{module_name}"
    module = sys.modules.get(qualified_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(qualified_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load crosslinked implementation {path}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
    return module


_boltz = _load_private_module("boltz_structure")
_peptide = _load_private_module("peptide_structure")
_small_molecule = _load_private_module("small_molecule")
_export = _load_private_module("structure_export")

build_crosslinked_peptide_topology = _peptide.build_crosslinked_peptide_topology
generate_rdkit_conformer_ensemble = _small_molecule.generate_rdkit_conformer_ensemble
generate_boltz_conformer_ensemble = _boltz.generate_boltz_conformer_ensemble
optimize_conformer_ensemble_with_xtb = _small_molecule.optimize_conformer_ensemble_with_xtb
validate_cluster_select_conformers = _peptide.validate_cluster_select_conformers
export_structure_ensemble = _export.export_structure_ensemble

__all__ = [
    "build_crosslinked_peptide_topology",
    "generate_rdkit_conformer_ensemble",
    "generate_boltz_conformer_ensemble",
    "optimize_conformer_ensemble_with_xtb",
    "validate_cluster_select_conformers",
    "export_structure_ensemble",
]
