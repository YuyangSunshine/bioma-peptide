"""Validated molecular-structure ensemble export tools."""

from __future__ import annotations

from bioma.peptide.contracts import result_validator as peptide_result_validator
import csv
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError
from tools.our_tools._crosslinked_impl.ensemble_contract import (
    contract_hash,
    require_final_target,
)


_STABLE_ID = "_BioMAStableAtomId"


class StructureEnsembleExportInput(ToolInput):
    final_selection_manifest_path: str = Field(
        description="Manifest from the final conformer-selection stage."
    )
    output_dir: str = Field(description="Directory for final structure artifacts.")
    export_options_json: str = Field(
        default="{}",
        description="Optional JSON settings including basename and hydrogen output.",
    )


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"{label} is not valid JSON: {exc}",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        ) from exc
    if not isinstance(value, dict):
        raise ToolError(
            f"{label} must be a JSON object.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensemble_result_hash(molecules: list[Any]) -> str:
    """Hash exported molecular identities and coordinates without artifact paths."""
    records: list[dict[str, Any]] = []
    for index, molecule in enumerate(molecules, start=1):
        conformer = molecule.GetConformer()
        records.append(
            {
                "candidate_id": (
                    molecule.GetProp("candidate_id")
                    if molecule.HasProp("candidate_id")
                    else f"candidate_{index:04d}"
                ),
                "atoms": [
                    [
                        atom.GetAtomicNum(),
                        atom.GetIsotope(),
                        atom.GetFormalCharge(),
                    ]
                    for atom in molecule.GetAtoms()
                ],
                "bonds": [
                    [
                        min(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                        max(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                        float(bond.GetBondTypeAsDouble()),
                    ]
                    for bond in molecule.GetBonds()
                ],
                "coordinates": [
                    [round(float(value), 8) for value in row]
                    for row in conformer.GetPositions()
                ],
            }
        )
    return _stable_hash(records)


def _rdkit() -> tuple[Any, Any, Any]:
    try:
        import numpy as np
        from rdkit import Chem, rdBase
    except ImportError as exc:
        raise ToolError(
            "RDKit and NumPy are required for structure ensemble export.",
            error_type="unavailable",
            retriable=False,
            backend="RDKit",
        ) from exc
    return Chem, rdBase, np


def _read_sdf(Chem: Any, path: Path) -> list[Any]:
    supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=True)
    molecules = [mol for mol in supplier if mol is not None]
    if not molecules:
        raise ValueError(f"No readable structures were found in {path}.")
    return molecules


def _apply_atom_map(mol: Any, atom_map_path: Path) -> list[int]:
    records = _load_json(atom_map_path).get("atoms", [])
    core: list[int] = []
    for record in records:
        index = int(record["atom_index"])
        if index >= mol.GetNumAtoms():
            raise ValueError("Atom map is incompatible with the exported molecule.")
        atom = mol.GetAtomWithIdx(index)
        if atom.GetSymbol() != record["element"]:
            raise ValueError("Atom-map element order differs from the exported molecule.")
        atom.SetProp(_STABLE_ID, str(record["stable_id"]))
        if record.get("core_heavy_atom"):
            core.append(index)
    return core


def _kabsch_rmsd(first: Any, second: Any, atom_indices: list[int], np: Any) -> float:
    p = np.asarray(first.GetConformer().GetPositions(), dtype=float)[atom_indices]
    q = np.asarray(second.GetConformer().GetPositions(), dtype=float)[atom_indices]
    p = p - p.mean(axis=0)
    q = q - q.mean(axis=0)
    covariance = p.T @ q
    left, _, right_t = np.linalg.svd(covariance)
    rotation = left @ right_t
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right_t
    delta = p @ rotation - q
    return float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))


def _pdb_remarks(
    rank: int,
    mol: Any,
    topology_manifest: dict[str, Any],
    final_manifest: dict[str, Any],
) -> str:
    lines = [
        f"REMARK 950 BIOMA ENSEMBLE RANK {rank}",
        f"REMARK 950 CANDIDATE ID {mol.GetProp('candidate_id') if mol.HasProp('candidate_id') else rank}",
        f"REMARK 950 TOTAL FORMAL CHARGE {topology_manifest['total_charge']}",
        f"REMARK 950 UHF {topology_manifest['uhf']}",
        "REMARK 950 SDF AND JSON RECORD BOND ORDERS AND FORMAL CHARGES",
    ]
    if mol.HasProp("xtb_energy_hartree"):
        lines.append(f"REMARK 950 XTB ENERGY HARTREE {mol.GetProp('xtb_energy_hartree')}")
    if mol.HasProp("source_backend"):
        lines.append(f"REMARK 950 COORDINATE SOURCE {mol.GetProp('source_backend')}")
    if mol.HasProp("relative_energy_kcal_mol"):
        lines.append(
            f"REMARK 950 RELATIVE ENERGY KCAL MOL {mol.GetProp('relative_energy_kcal_mol')}"
        )
    optimization = final_manifest.get("optimization", {})
    if optimization:
        lines.append(
            "REMARK 950 XTB METHOD "
            f"{optimization.get('method', '')} {optimization.get('solvent_model', '')}({optimization.get('solvent', '')})"
        )
    for connection in topology_manifest.get("connections", []):
        lines.append(
            "REMARK 950 CONNECTION "
            f"{connection['atom1']} -- {connection['atom2']} ORDER {connection['bond_order']}"
        )
    policy = final_manifest.get("selection_policy", {})
    if policy.get("cluster_rmsd_angstrom") is not None:
        lines.append(
            f"REMARK 950 CORE RMSD CLUSTER {policy['cluster_rmsd_angstrom']} ANGSTROM"
        )
    return "\n".join(lines) + "\n"


@bioma_tool(
    name="export_structure_ensemble",
    description=(
        "Export a validated conformer ensemble as complete per-conformer PDB files, "
        "a multi-record SDF, summary tables, metadata, and round-trip checks."
    ),
    category="file",
    version="1.1.0",
    author="BioMA",
    args_schema=StructureEnsembleExportInput,
    tags=["structure", "export", "pdb", "sdf", "ensemble"],
 result_kind="computed_prediction", result_validator=peptide_result_validator)
def export_structure_ensemble(
    final_selection_manifest_path: str,
    output_dir: str,
    export_options_json: str = "{}",
) -> str:
    Chem, rd_base, np = _rdkit()
    options = _json_object(export_options_json, "export_options_json")
    final_manifest_file = Path(final_selection_manifest_path).expanduser().resolve()
    final_manifest = _load_json(final_manifest_file)
    if final_manifest.get("stage") != "select_for_final":
        raise ToolError(
            "final_selection_manifest_path must identify a select_for_final manifest.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    topology_manifest_file = Path(final_manifest["topology_manifest"]).resolve()
    topology_manifest = _load_json(topology_manifest_file)
    chemical_spec = _load_json(
        Path(topology_manifest["artifacts"]["chemical_spec_json"]).resolve()
    )
    atom_map_path = Path(topology_manifest["artifacts"]["atom_map_json"]).resolve()
    source_path = Path(final_manifest["artifacts"]["selected_sdf"]).resolve()
    molecules = _read_sdf(Chem, source_path)
    target_count = int(final_manifest["target_ensemble_size"])
    contract = require_final_target(target_count, stage="export")
    if len(molecules) != target_count:
        raise ToolError(
            f"Final selection contains {len(molecules)} structures; expected {target_count}.",
            error_type="invalid_input",
            retriable=True,
            backend="RDKit",
        )
    core: list[int] = []
    for mol in molecules:
        current_core = _apply_atom_map(mol, atom_map_path)
        if not core:
            core = current_core
        elif current_core != core:
            raise ValueError("Core atom mapping differs across final conformers.")
    if not core:
        raise ValueError("The atom map defines no core heavy atoms.")

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    basename = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(options.get("basename", "peptide_ensemble"))).strip("._")
    if not basename:
        basename = "peptide_ensemble"
    include_hydrogens = bool(options.get("include_hydrogens", True))

    pdb_paths: list[str] = []
    summary_rows: list[dict[str, Any]] = []
    export_molecules: list[Any] = []
    for rank, source in enumerate(molecules, start=1):
        mol = Chem.Mol(source)
        if not include_hydrogens:
            mol = Chem.RemoveHs(mol, sanitize=True)
        candidate_id = mol.GetProp("candidate_id") if mol.HasProp("candidate_id") else f"conformer_{rank:03d}"
        mol.SetProp("ensemble_rank", str(rank))
        mol.SetProp("candidate_id", candidate_id)
        mol.SetProp("total_formal_charge", str(topology_manifest["total_charge"]))
        mol.SetProp("uhf", str(topology_manifest["uhf"]))
        export_molecules.append(mol)
        pdb_path = destination / f"{basename}_conf_{rank:03d}.pdb"
        pdb_block = Chem.MolToPDBBlock(mol, confId=0, flavor=4)
        pdb_path.write_text(
            _pdb_remarks(rank, mol, topology_manifest, final_manifest) + pdb_block,
            encoding="utf-8",
        )
        pdb_paths.append(str(pdb_path))
        summary_rows.append(
            {
                "ensemble_rank": rank,
                "candidate_id": candidate_id,
                "source_candidate_id": mol.GetProp("source_candidate_id") if mol.HasProp("source_candidate_id") else "",
                "source_backend": mol.GetProp("source_backend") if mol.HasProp("source_backend") else "",
                "boltz_confidence": mol.GetProp("boltz_confidence") if mol.HasProp("boltz_confidence") else "",
                "cluster_id": mol.GetProp("cluster_id") if mol.HasProp("cluster_id") else "",
                "xtb_energy_hartree": mol.GetProp("xtb_energy_hartree") if mol.HasProp("xtb_energy_hartree") else "",
                "relative_energy_kcal_mol": mol.GetProp("relative_energy_kcal_mol") if mol.HasProp("relative_energy_kcal_mol") else "",
                "total_formal_charge": topology_manifest["total_charge"],
                "uhf": topology_manifest["uhf"],
            }
        )

    ensemble_sdf = destination / f"{basename}.sdf"
    sdf_writer = Chem.SDWriter(str(ensemble_sdf))
    for mol in export_molecules:
        creator = getattr(Chem, "CreateAtomStringPropertyList", None)
        if creator is not None:
            creator(mol, _STABLE_ID)
        sdf_writer.write(mol)
    sdf_writer.close()

    summary_csv = destination / f"{basename}_summary.csv"
    with summary_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    rmsd_csv = destination / f"{basename}_rmsd_matrix.csv"
    labels = [row["candidate_id"] for row in summary_rows]
    rmsd_values: list[list[float]] = []
    with rmsd_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["candidate_id", *labels])
        for first_index, first in enumerate(export_molecules):
            row: list[float] = []
            for second in export_molecules:
                row.append(_kabsch_rmsd(first, second, core, np))
            rmsd_values.append(row)
            writer.writerow([labels[first_index], *[f"{value:.6f}" for value in row]])

    loaded_sdf = _read_sdf(Chem, ensemble_sdf)
    pdb_loaded = [
        Chem.MolFromPDBFile(path, sanitize=False, removeHs=False) is not None
        for path in pdb_paths
    ]
    charge_values = [
        int(sum(atom.GetFormalCharge() for atom in mol.GetAtoms())) for mol in loaded_sdf
    ]
    minimum_off_diagonal = min(
        rmsd_values[first][second]
        for first in range(len(rmsd_values))
        for second in range(first)
    ) if len(rmsd_values) > 1 else None
    checks = {
        "requested_pdb_count_written": len(pdb_paths) == target_count,
        "all_pdb_files_readable": all(pdb_loaded),
        "sdf_record_count_matches": len(loaded_sdf) == target_count,
        "sdf_formal_charge_matches": all(
            charge == int(topology_manifest["total_charge"]) for charge in charge_values
        ),
        "all_output_files_nonempty": all(Path(path).stat().st_size > 0 for path in pdb_paths)
        and ensemble_sdf.stat().st_size > 0,
    }
    validation_path = destination / "export_validation.json"
    validation = {
        "passed": all(checks.values()),
        "checks": checks,
        "pdb_count": len(pdb_paths),
        "sdf_record_count": len(loaded_sdf),
        "minimum_pairwise_core_rmsd_angstrom": minimum_off_diagonal,
    }
    _write_json(validation_path, validation)

    metadata_path = destination / f"{basename}_metadata.json"
    metadata = {
        "chemical_hash": topology_manifest["chemical_hash"],
        "sequence": topology_manifest.get("sequence"),
        "pH": topology_manifest.get("pH"),
        "experimental_conditions": topology_manifest.get("experimental_conditions", {}),
        "chemical_state": {
            "total_formal_charge": topology_manifest["total_charge"],
            "uhf": topology_manifest["uhf"],
            "connections": topology_manifest.get("connections", []),
            "charge_states": chemical_spec.get("charge_states", chemical_spec.get("site_charges", [])),
        },
        "computational_state": {
            "optimization": final_manifest.get("optimization", {}),
            "optimization_timing": final_manifest.get("optimization_timing", {}),
            "solvent_selection": final_manifest.get("solvent_selection", {}),
            "selection_policy": final_manifest.get("selection_policy", {}),
            "source_manifest": str(final_manifest_file),
            "rdkit_version": rd_base.rdkitVersion,
        },
        "ensemble": summary_rows,
        "coordinate_source_counts": {
            source: sum(1 for row in summary_rows if row["source_backend"] == source)
            for source in sorted({str(row["source_backend"]) for row in summary_rows})
        },
        "formal_charge_and_bond_reference": "ensemble_sdf_and_metadata",
        "export_validation": validation,
    }
    _write_json(metadata_path, metadata)

    manifest_path = destination / "export_manifest.json"
    config_hash = _stable_hash(
        {
            "input": final_manifest["config_hash"],
            "basename": basename,
            "include_hydrogens": include_hydrogens,
            "rdkit": rd_base.rdkitVersion,
        }
    )
    ensemble_hash = _ensemble_result_hash(export_molecules)
    result_hash = _stable_hash(
        {
            "status": "ok" if validation["passed"] else "partial_success",
            "chemical_hash": topology_manifest["chemical_hash"],
            "candidate_ids": labels,
            "summary": summary_rows,
            "rmsd_matrix": [
                [round(float(value), 8) for value in row] for row in rmsd_values
            ],
            "validation": validation,
            "ensemble_hash": ensemble_hash,
        }
    )
    manifest = {
        "contract_version": "1.0",
        "tool_name": "export_structure_ensemble",
        "tool_version": "1.1.0",
        "stage": "final_export",
        "status": "ok" if validation["passed"] else "partial_success",
        "chemical_hash": topology_manifest["chemical_hash"],
        "input_hash": final_manifest["config_hash"],
        "config_hash": config_hash,
        "result_hash": result_hash,
        "parent_manifest": str(final_manifest_file),
        "parent_manifests": [str(final_manifest_file)],
        "topology_manifest": str(topology_manifest_file),
        "target_ensemble_size": target_count,
        "ensemble_contract_hash": contract_hash(contract),
        "candidate_count": len(export_molecules),
        "candidate_ids": labels,
        "artifacts": {
            "pdb_files": pdb_paths,
            "ensemble_sdf": str(ensemble_sdf),
            "summary_csv": str(summary_csv),
            "rmsd_matrix_csv": str(rmsd_csv),
            "metadata_json": str(metadata_path),
            "export_validation_json": str(validation_path),
        },
        "artifact_hashes": {
            "ensemble_scientific_sha256": ensemble_hash,
        },
        "warnings": [],
        "recommended_action": None,
    }
    _write_json(manifest_path, manifest)
    return json.dumps(
        {
            "status": manifest["status"],
            "manifest_path": str(manifest_path),
            "pdb_files": pdb_paths,
            "ensemble_sdf": str(ensemble_sdf),
            "summary_csv": str(summary_csv),
            "metadata_json": str(metadata_path),
            "validation_path": str(validation_path),
            "config_hash": config_hash,
            "result_hash": result_hash,
        },
        ensure_ascii=False,
        indent=2,
    )
