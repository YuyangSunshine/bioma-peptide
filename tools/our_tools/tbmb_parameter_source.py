#!/usr/bin/env python3
"""Auditable TBMB--methionine local QM/RESP parameter-source workflow.

This program builds an auditable parameter *source*: a reusable local model
(TBMB plus three Met side-chain fragments), diverse authoritative conformers,
HF/6-31G* ESPs, a multi-conformer two-stage RESP fit, a bonded-term review
handoff, and AMBER residue-template/FRCMOD candidates.  The candidate builder
uses ff14SB only for the standard peptide backbone/CB boundary and GAFF2 only
for atom typing/analogue suggestions in the non-standard region.  It never
turns an unreviewed analogue into a production parameter.

Two incompatible scientific environments exchange only ordinary files:

* ``tbmb_resp``: RDKit, QCElemental and PsiRESP (model/grid/RESP stages)
* ``tbmb_qm``: Psi4 (electronic-structure/ESP stage)

No stage sets ``approved: true``.  The generated MSU/CMU/TMB libraries and
FRCMOD are candidates until the charge-transfer correction and every flagged
bonded term have qualified review.  After that review, the existing
``parameterize_tbmb_scaffold`` docking tool seals the files and independently
repeats the reference-sequence TLEaP assembly test.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import traceback
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_SEQUENCE = ""  # No private case sequence is embedded; provide --sequence.
DEFAULT_MET_POSITIONS = (2, 9, 16)
DEFAULT_FORMAL_CHARGE = 5
LOCAL_MODEL_CHARGE = 3
LOCAL_MODEL_MULTIPLICITY = 1
LOCAL_MET_ATOMS = ("CB", "CG", "SD", "CE")
INTERNAL_MET_TEMPLATE = "MSU"
N_TERMINAL_MET_TEMPLATE = "NMU"
C_TERMINAL_MET_TEMPLATE = "CMU"
LINKER_TEMPLATE = "TMB"


class WorkflowError(RuntimeError):
    """A stage failed a scientific or provenance gate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> Path:
    _mkdir(path.parent)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise WorkflowError(f"Required JSON file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read JSON {path}: {exc}") from exc


def _import_rdkit():
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolAlign
    except ImportError as exc:
        raise WorkflowError("RDKit is required; run this stage in the tbmb_resp environment.") from exc
    return Chem, rdMolAlign


def _layout(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "validation": root / "validation",
        "inputs": root / "raw" / "input_conformers",
        "audit": root / "validation" / "input_audit.json",
        "atom_map": root / "validation" / "authoritative_atom_map.json",
        "models": root / "derived" / "qm_models",
        "selection": root / "derived" / "qm_models" / "selection.json",
        "jobs": root / "raw" / "qm_jobs",
        "job_manifest": root / "raw" / "qm_jobs" / "jobs.json",
        "qm_logs": root / "raw" / "qm_logs",
        "qm_collection": root / "validation" / "qm_collection.json",
        "resp": root / "raw" / "resp_inputs_outputs",
        "charges": root / "derived" / "resp_charges.json",
        "charges_csv": root / "derived" / "resp_charges.csv",
        "charge_audit": root / "validation" / "resp_charge_audit.json",
        "scan_plan": root / "derived" / "bonded_scan_plan.json",
        "scan_review": root / "raw" / "bonded_scan_review.json",
        "scan_audit": root / "validation" / "bonded_scan_audit.json",
        "scan_jobs": root / "raw" / "bonded_qm_jobs",
        "scan_job_manifest": root / "raw" / "bonded_qm_jobs" / "jobs.json",
        "scan_logs": root / "raw" / "bonded_qm_logs",
        "scan_collection": root / "validation" / "bonded_qm_collection.json",
        "scan_fit": root / "derived" / "bonded_fit_candidates.json",
        "mm_baseline": root / "derived" / "mm_baseline",
        "mm_baseline_audit": root / "validation" / "mm_baseline_build.json",
        "mm_evaluation": root / "derived" / "mm_baseline" / "scan_energies.json",
        "mm_evaluation_audit": root / "validation" / "mm_baseline_evaluation.json",
        "qm_minus_mm_fit": root / "derived" / "qm_minus_mm_fit_candidates.json",
        "qm_minus_mm_audit": root / "validation" / "qm_minus_mm_fit_audit.json",
        "amber": root / "derived" / "amber_candidates",
        "amber_local_input": root / "derived" / "amber_candidates" / "local_model_resp.mol2",
        "amber_local_typed": root / "derived" / "amber_candidates" / "local_model_gaff2.mol2",
        "amber_hybrid_typed": root / "derived" / "amber_candidates" / "local_model_hybrid.mol2",
        "amber_atom_types": root / "derived" / "amber_candidates" / "local_atom_types.json",
        "amber_charge_transfer": root / "validation" / "charge_transfer_audit.json",
        "amber_build_audit": root / "validation" / "amber_candidate_build.json",
        "amber_validation": root / "validation" / "amber_candidate_validation.json",
        "amber_frcmod": root / "derived" / "amber_candidates" / "tbmb_met_link.frcmod",
        "amber_frcmod_raw": root / "derived" / "amber_candidates" / "tbmb_met_link_parmchk2_raw.frcmod",
        "amber_boundary_frcmod": root / "derived" / "amber_candidates" / "tbmb_met_boundary.frcmod",
        "amber_boundary_frcmod_raw": root / "derived" / "amber_candidates" / "tbmb_met_boundary_parmchk2_raw.frcmod",
        "amber_frcmod_filter_audit": root / "validation" / "frcmod_filter_audit.json",
        "amber_reviewed_frcmod": root / "derived" / "amber_candidates" / "tbmb_met_reviewed.frcmod",
        "amber_reviewed_audit": root / "validation" / "reviewed_frcmod_audit.json",
        "source_manifest": root / "source_manifest.yaml",
        "status": root / "validation" / "status.json",
    }


def _load_metadata(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = _read_json(path)
    if not isinstance(metadata, dict):
        raise WorkflowError("Ligand metadata must be a JSON object.")
    chemical = metadata.get("chemical_definition") or metadata
    if not isinstance(chemical, dict):
        raise WorkflowError("Ligand metadata lacks chemical_definition.")
    return metadata, chemical


def _pdb_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        element = line[76:78].strip().title() if len(line) >= 78 else ""
        if not element:
            name = "".join(char for char in line[12:16] if char.isalpha()).upper()
            element = name[:1].title()
        if element.upper() == "H":
            continue
        try:
            xyz = [float(line[30:38]), float(line[38:46]), float(line[46:54])]
        except ValueError as exc:
            raise WorkflowError(f"Invalid PDB coordinates: {line}") from exc
        records.append({
            "serial": int(line[6:11]),
            "atom_name": line[12:16].strip(),
            "residue_name": line[17:20].strip(),
            "chain": line[21:22].strip(),
            "residue_number": int(line[22:26]),
            "element": element,
            "xyz": xyz,
        })
    if not records:
        raise WorkflowError(f"No heavy atoms found in reference PDB: {path}")
    return records


def _stable_id(record: dict[str, Any]) -> str:
    residue = str(record["residue_name"]).upper()
    atom = str(record["atom_name"]).upper()
    if residue in {"TBM", "TMB"}:
        return f"TBM:{atom}"
    if residue in {"AMD", "NHE"}:
        return f"CTN:{atom}"
    if residue == "ACE":
        return f"ACE:{atom}"
    return f"PEP:{int(record['residue_number'])}:{atom}"


def _topology_signature(mol: Any) -> tuple[Any, ...]:
    atoms = tuple((atom.GetAtomicNum(), atom.GetFormalCharge(), atom.GetIsotope()) for atom in mol.GetAtoms())
    bonds = tuple(sorted((min(b.GetBeginAtomIdx(), b.GetEndAtomIdx()),
                          max(b.GetBeginAtomIdx(), b.GetEndAtomIdx()),
                          float(b.GetBondTypeAsDouble())) for b in mol.GetBonds()))
    return atoms, bonds


def _sulfonium_count(mol: Any) -> int:
    return sum(atom.GetAtomicNum() == 16 and atom.GetFormalCharge() == 1 and atom.GetDegree() == 3
               for atom in mol.GetAtoms())


def _map_reference_atoms(mol: Any, pdb_path: Path, tolerance: float = 0.25) -> list[dict[str, Any]]:
    records = _pdb_records(pdb_path)
    heavy = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1]
    if len(heavy) != len(records):
        raise WorkflowError(f"Reference SDF has {len(heavy)} heavy atoms but PDB has {len(records)}.")
    conformer = mol.GetConformer()
    unused = set(range(len(records)))
    mapping: list[dict[str, Any]] = []
    for atom in heavy:
        point = conformer.GetAtomPosition(atom.GetIdx())
        candidates: list[tuple[float, int]] = []
        for index in unused:
            record = records[index]
            if str(record["element"]).upper() != atom.GetSymbol().upper():
                continue
            distance = math.dist((point.x, point.y, point.z), record["xyz"])
            candidates.append((distance, index))
        if not candidates:
            raise WorkflowError(f"Cannot map SDF atom {atom.GetIdx()} ({atom.GetSymbol()}) to PDB.")
        distance, selected = min(candidates)
        if distance > tolerance:
            raise WorkflowError(
                f"SDF/PDB mapping exceeds {tolerance:.2f} A at atom {atom.GetIdx()}: {distance:.3f} A"
            )
        unused.remove(selected)
        record = dict(records[selected])
        record.update({
            "sdf_atom_index": atom.GetIdx(),
            "stable_id": _stable_id(record),
            "mapping_distance_angstrom": distance,
            "formal_charge": atom.GetFormalCharge(),
            "degree": atom.GetDegree(),
        })
        mapping.append(record)
    stable_ids = [item["stable_id"] for item in mapping]
    if len(stable_ids) != len(set(stable_ids)):
        duplicates = sorted({value for value in stable_ids if stable_ids.count(value) > 1})
        raise WorkflowError(f"Reference PDB produced duplicate stable atom IDs: {duplicates}")
    return sorted(mapping, key=lambda item: item["sdf_atom_index"])


def _load_ensemble(sdf: Path) -> list[Any]:
    Chem, _ = _import_rdkit()
    supplier = Chem.SDMolSupplier(str(sdf), removeHs=False, sanitize=True)
    molecules: list[Any] = []
    failures: list[int] = []
    for index, molecule in enumerate(supplier, 1):
        if molecule is None:
            failures.append(index)
        else:
            molecules.append(molecule)
    if failures:
        raise WorkflowError(f"Unreadable SDF records: {failures}")
    if not molecules:
        raise WorkflowError(f"No readable molecules in {sdf}")
    return molecules


def audit_inputs(args: argparse.Namespace) -> dict[str, Any]:
    Chem, _ = _import_rdkit()
    paths = _layout(_path(args.root))
    sdf, metadata_path, pdb = _path(args.sdf), _path(args.metadata), _path(args.reference_pdb)
    for required in (sdf, metadata_path, pdb):
        if not required.is_file():
            raise WorkflowError(f"Required authoritative input is missing: {required}")
    _, chemical = _load_metadata(metadata_path)
    molecules = _load_ensemble(sdf)
    sequence = str(chemical.get("sequence", "")).upper()
    met_positions = tuple(int(value) for value in chemical.get("met_positions", []))
    expected_sequence = str(args.sequence).upper()
    expected_positions = tuple(int(value) for value in args.met_positions)
    failures: list[str] = []
    if sequence != expected_sequence:
        failures.append(f"metadata sequence {sequence!r} != expected {expected_sequence!r}")
    if met_positions != expected_positions:
        failures.append(f"metadata Met positions {met_positions} != expected {expected_positions}")
    if any(not 1 <= position <= len(sequence) or sequence[position - 1] != "M" for position in met_positions):
        failures.append("one or more declared crosslink positions is not a methionine in the metadata sequence")
    if int(chemical.get("total_charge", args.formal_charge)) != int(args.formal_charge):
        failures.append(
            f"metadata total charge {chemical.get('total_charge')} != expected {args.formal_charge}"
        )
    if str(chemical.get("n_terminus", "")).lower() != "acetylated":
        failures.append("metadata N terminus is not acetylated")
    if str(chemical.get("c_terminus", "")).lower() != "primary_amide":
        failures.append("metadata C terminus is not a primary amide")
    if len(molecules) < int(args.conformers):
        failures.append(f"only {len(molecules)} conformers are available; need at least {args.conformers}")
    reference_signature = _topology_signature(molecules[0])
    topology_matches = [_topology_signature(mol) == reference_signature for mol in molecules]
    if not all(topology_matches):
        failures.append("ensemble records do not share identical ordered atom/bond topology")
    charges = [Chem.GetFormalCharge(mol) for mol in molecules]
    crosslinks = [_sulfonium_count(mol) for mol in molecules]
    if any(value != int(args.formal_charge) for value in charges):
        failures.append(f"ensemble formal charges are {sorted(set(charges))}, expected {args.formal_charge}")
    if any(value != 3 for value in crosslinks):
        failures.append(f"ensemble sulfonium counts are {sorted(set(crosslinks))}, expected 3")
    mapping: list[dict[str, Any]] = []
    try:
        mapping = _map_reference_atoms(molecules[0], pdb)
    except WorkflowError as exc:
        failures.append(str(exc))
    required_ids = {f"PEP:{position}:{atom}" for position in expected_positions for atom in LOCAL_MET_ATOMS}
    required_ids.update({"TBM:CM1", "TBM:CM2", "TBM:CM3"})
    present_ids = {item["stable_id"] for item in mapping}
    missing_ids = sorted(required_ids - present_ids)
    if missing_ids:
        failures.append("stable atom map is missing: " + ", ".join(missing_ids))
    snapshot_dir = _mkdir(paths["inputs"])
    snapshots = {
        "ensemble_sdf": snapshot_dir / sdf.name,
        "metadata": snapshot_dir / metadata_path.name,
        "reference_pdb": snapshot_dir / pdb.name,
    }
    for source, target in ((sdf, snapshots["ensemble_sdf"]),
                           (metadata_path, snapshots["metadata"]),
                           (pdb, snapshots["reference_pdb"])):
        if source != target:
            shutil.copy2(source, target)
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": _now(),
        "passed": not failures,
        "authoritative_inputs": {
            "ensemble_sdf": {"path": str(snapshots["ensemble_sdf"]), "source_path": str(sdf),
                             "sha256": _sha256(snapshots["ensemble_sdf"])},
            "metadata": {"path": str(snapshots["metadata"]), "source_path": str(metadata_path),
                         "sha256": _sha256(snapshots["metadata"])},
            "reference_pdb": {"path": str(snapshots["reference_pdb"]), "source_path": str(pdb),
                              "sha256": _sha256(snapshots["reference_pdb"])},
        },
        "chemical_definition": {
            "sequence": sequence,
            "met_positions": list(met_positions),
            "formal_charge": charges[0] if charges else None,
            "sulfonium_count": crosslinks[0] if crosslinks else None,
            "n_terminus": chemical.get("n_terminus"),
            "c_terminus": chemical.get("c_terminus"),
            "sulfonium_stereochemistry": chemical.get("sulfonium_stereochemistry"),
        },
        "ensemble": {
            "record_count": len(molecules),
            "atom_count": molecules[0].GetNumAtoms(),
            "heavy_atom_count": sum(atom.GetAtomicNum() > 1 for atom in molecules[0].GetAtoms()),
            "bond_count": molecules[0].GetNumBonds(),
            "ordered_topology_identical": all(topology_matches),
        },
        "failures": failures,
    }
    _write_json(paths["atom_map"], {"schema_version": 1, "atoms": mapping})
    _write_json(paths["audit"], report)
    if failures:
        raise WorkflowError(f"Authoritative input audit failed; see {paths['audit']}")
    return {"input_audit": str(paths["audit"]), "atom_map": str(paths["atom_map"]), **report["ensemble"]}


def _require_pass(path: Path, label: str) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("passed") is not True:
        raise WorkflowError(f"{label} has not passed: {path}")
    return value


def _label_molecule(mol: Any, atom_map: list[dict[str, Any]]) -> None:
    by_index = {int(item["sdf_atom_index"]): str(item["stable_id"]) for item in atom_map}
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        if atom.GetIdx() not in by_index:
            raise WorkflowError(f"No stable ID for heavy SDF atom {atom.GetIdx()}")
        atom.SetProp("stable_id", by_index[atom.GetIdx()])
        atom.SetIntProp("source_sdf_atom_index", atom.GetIdx())


def _local_model(mol: Any, positions: Iterable[int]) -> tuple[Any, list[dict[str, Any]]]:
    Chem, _ = _import_rdkit()
    keep_ids = {f"PEP:{position}:{atom}" for position in positions for atom in LOCAL_MET_ATOMS}
    keep_indices = []
    for atom in mol.GetAtoms():
        stable_id = atom.GetProp("stable_id") if atom.HasProp("stable_id") else ""
        if stable_id.startswith("TBM:") or stable_id in keep_ids:
            keep_indices.append(atom.GetIdx())
    editable = Chem.RWMol(mol)
    for index in sorted(set(range(mol.GetNumAtoms())) - set(keep_indices), reverse=True):
        editable.RemoveAtom(index)
    fragment = editable.GetMol()
    fragment.UpdatePropertyCache(strict=False)
    Chem.SanitizeMol(fragment)
    fragment = Chem.AddHs(fragment, addCoords=True)
    hydrogen_counts: dict[str, int] = {}
    for atom in fragment.GetAtoms():
        if atom.GetAtomicNum() != 1:
            continue
        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 1 or not neighbors[0].HasProp("stable_id"):
            raise WorkflowError("Cannot assign a stable ID to an added model hydrogen.")
        parent = neighbors[0].GetProp("stable_id")
        hydrogen_counts[parent] = hydrogen_counts.get(parent, 0) + 1
        atom.SetProp("stable_id", f"{parent}:H{hydrogen_counts[parent]}")
        atom.SetIntProp("source_sdf_atom_index", -1)
    if Chem.GetFormalCharge(fragment) != LOCAL_MODEL_CHARGE:
        raise WorkflowError(
            f"Local model formal charge is {Chem.GetFormalCharge(fragment)}, expected {LOCAL_MODEL_CHARGE}."
        )
    if _sulfonium_count(fragment) != 3:
        raise WorkflowError("Local model does not retain all three three-coordinate +1 sulfonium centers.")
    mapping = []
    for atom in fragment.GetAtoms():
        mapping.append({
            "model_atom_index": atom.GetIdx(),
            "stable_id": atom.GetProp("stable_id"),
            "element": atom.GetSymbol(),
            "formal_charge": atom.GetFormalCharge(),
            "source_sdf_atom_index": atom.GetIntProp("source_sdf_atom_index"),
            "boundary_cap": atom.GetProp("stable_id").split(":H", 1)[0].endswith(":CB")
                or atom.GetProp("stable_id").endswith(":CB"),
        })
    return fragment, mapping


def _coordinates(mol: Any, indices: list[int] | None = None) -> list[list[float]]:
    conformer = mol.GetConformer()
    selected = indices if indices is not None else list(range(mol.GetNumAtoms()))
    return [[float(conformer.GetAtomPosition(i).x), float(conformer.GetAtomPosition(i).y),
             float(conformer.GetAtomPosition(i).z)] for i in selected]


def _kabsch_rmsd(first: list[list[float]], second: list[list[float]]) -> float:
    try:
        import numpy as np
    except ImportError as exc:
        raise WorkflowError("NumPy is required in the tbmb_resp environment.") from exc
    left, right = np.asarray(first, dtype=float), np.asarray(second, dtype=float)
    left -= left.mean(axis=0); right -= right.mean(axis=0)
    covariance = left.T @ right
    u, _, vt = np.linalg.svd(covariance)
    correction = np.eye(3); correction[-1, -1] = np.sign(np.linalg.det(u @ vt))
    rotation = u @ correction @ vt
    delta = left @ rotation - right
    return float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))


def _select_diverse(models: list[Any], count: int) -> tuple[list[int], list[list[float]]]:
    heavy_indices = [atom.GetIdx() for atom in models[0].GetAtoms() if atom.GetAtomicNum() > 1]
    xyz = [_coordinates(model, heavy_indices) for model in models]
    matrix = [[0.0 for _ in models] for _ in models]
    for i in range(len(models)):
        for j in range(i):
            matrix[i][j] = matrix[j][i] = _kabsch_rmsd(xyz[i], xyz[j])
    def energy(index: int) -> float:
        for key in ("relative_energy_kcal_mol", "xtb_energy_hartree"):
            if models[index].HasProp(key):
                try:
                    return float(models[index].GetProp(key))
                except ValueError:
                    pass
        return float(index)
    selected = [min(range(len(models)), key=energy)]
    while len(selected) < min(count, len(models)):
        remaining = [index for index in range(len(models)) if index not in selected]
        selected.append(max(remaining, key=lambda index: min(matrix[index][chosen] for chosen in selected)))
    return selected, matrix


def _write_xyz(mol: Any, path: Path) -> None:
    lines = [str(mol.GetNumAtoms()), "TBMB plus three methyl-capped methionine side chains; charge +3"]
    for atom, xyz in zip(mol.GetAtoms(), _coordinates(mol)):
        lines.append(f"{atom.GetSymbol():<2s} {xyz[0]: .10f} {xyz[1]: .10f} {xyz[2]: .10f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_qm_models(args: argparse.Namespace) -> dict[str, Any]:
    Chem, _ = _import_rdkit()
    paths = _layout(_path(args.root))
    audit = _require_pass(paths["audit"], "Input audit")
    inputs = audit["authoritative_inputs"]
    sdf = _path(args.sdf) if args.sdf else _path(inputs["ensemble_sdf"]["path"])
    if _sha256(sdf) != inputs["ensemble_sdf"]["sha256"]:
        raise WorkflowError("Ensemble SDF changed after input audit; rerun audit-inputs.")
    atom_map = _read_json(paths["atom_map"])["atoms"]
    molecules = _load_ensemble(sdf)
    models = []
    mappings = []
    for molecule in molecules:
        _label_molecule(molecule, atom_map)
        model, mapping = _local_model(molecule, args.met_positions)
        models.append(model); mappings.append(mapping)
    signatures = [_topology_signature(model) for model in models]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise WorkflowError("Local QM models do not share one ordered topology.")
    selected, rmsd = _select_diverse(models, int(args.conformers))
    _mkdir(paths["models"])
    records = []
    for rank, ensemble_index in enumerate(selected, 1):
        model = models[ensemble_index]
        sdf_path = paths["models"] / f"qm_model_conf_{rank:02d}.sdf"
        xyz_path = paths["models"] / f"qm_model_conf_{rank:02d}.xyz"
        writer = Chem.SDWriter(str(sdf_path)); writer.write(model); writer.close()
        _write_xyz(model, xyz_path)
        records.append({
            "qm_conformer_id": f"conf_{rank:02d}",
            "ensemble_record_index": ensemble_index + 1,
            "sdf": str(sdf_path), "sdf_sha256": _sha256(sdf_path),
            "xyz": str(xyz_path), "xyz_sha256": _sha256(xyz_path),
        })
    output = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "model_definition": {
            "description": "TBMB plus Met CB-CG-SD-CE at all three crosslinks; CB boundaries methyl-capped",
            "charge": LOCAL_MODEL_CHARGE, "multiplicity": LOCAL_MODEL_MULTIPLICITY,
            "met_positions": list(args.met_positions), "standard_backbone_forcefield": "ff14SB",
            "transfer_boundary": "CB cap groups are constrained to net zero and are not transferred",
        },
        "model_atom_map": mappings[0],
        "model_connectivity": [
            [bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), float(bond.GetBondTypeAsDouble())]
            for bond in models[0].GetBonds()
        ],
        "selected_conformers": records,
        "heavy_atom_rmsd_matrix_angstrom": rmsd,
    }
    _write_json(paths["selection"], output)
    return {"selection": str(paths["selection"]), "selected": len(records),
            "model_atoms": models[0].GetNumAtoms(), "model_charge": LOCAL_MODEL_CHARGE}


def _xyz_data(path: Path) -> tuple[list[str], list[list[float]]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        count = int(lines[0].strip())
    except (IndexError, ValueError) as exc:
        raise WorkflowError(f"Invalid XYZ header: {path}") from exc
    symbols, coordinates = [], []
    for line in lines[2:2 + count]:
        fields = line.split()
        if len(fields) < 4:
            raise WorkflowError(f"Invalid XYZ atom line in {path}: {line}")
        symbols.append(fields[0]); coordinates.append([float(value) for value in fields[1:4]])
    if len(symbols) != count:
        raise WorkflowError(f"XYZ atom count mismatch in {path}")
    return symbols, coordinates


def _qcmol(symbols: list[str], coordinates_angstrom: list[list[float]], charge: int,
           connectivity: list[list[float]] | None = None):
    try:
        import numpy as np
        import qcelemental as qcel
    except ImportError as exc:
        raise WorkflowError("QCElemental and NumPy are required in the tbmb_resp environment.") from exc
    bohr_to_angstrom = qcel.constants.conversion_factor("bohr", "angstrom")
    return qcel.models.Molecule(
        symbols=symbols,
        geometry=np.asarray(coordinates_angstrom, dtype=float) / bohr_to_angstrom,
        molecular_charge=charge,
        molecular_multiplicity=LOCAL_MODEL_MULTIPLICITY,
        fix_com=True, fix_orientation=True,
        connectivity=connectivity,
    )


def prepare_psi4(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    selection = _require_pass(paths["selection"], "QM model construction")
    try:
        import numpy as np
        import psiresp
    except ImportError as exc:
        raise WorkflowError("PsiRESP and NumPy are required; run prepare-psi4 in tbmb_resp.") from exc
    jobs = []
    grid_options = psiresp.GridOptions(
        use_radii="msk", vdw_scale_factors=[1.4, 1.6, 1.8, 2.0], vdw_point_density=1.0,
    )
    for record in selection["selected_conformers"]:
        xyz = _path(record["xyz"])
        symbols, coordinates = _xyz_data(xyz)
        molecule = _qcmol(symbols, coordinates, LOCAL_MODEL_CHARGE,
                           selection.get("model_connectivity"))
        grid = grid_options.generate_grid(molecule)
        if len(grid) < 100:
            raise WorkflowError(f"ESP grid for {record['qm_conformer_id']} has only {len(grid)} points.")
        job_dir = _mkdir(paths["jobs"] / record["qm_conformer_id"])
        grid_path = job_dir / "grid_angstrom.txt"
        np.savetxt(grid_path, grid, fmt="%.10f")
        jobs.append({
            **record,
            "job_directory": str(job_dir),
            "grid_angstrom": str(grid_path),
            "grid_sha256": _sha256(grid_path),
            "grid_point_count": len(grid),
            "qm_method": "HF", "qm_basis": "6-31G*", "threads": 1,
            "result_json": str(paths["qm_logs"] / record["qm_conformer_id"] / "result.json"),
            "esp": str(paths["qm_logs"] / record["qm_conformer_id"] / "esp_au.txt"),
            "psi4_output": str(paths["qm_logs"] / record["qm_conformer_id"] / "psi4.out"),
        })
    manifest = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "grid": {"radii": "msk", "scale_factors": [1.4, 1.6, 1.8, 2.0], "point_density": 1.0,
                 "coordinate_units": "angstrom"},
        "qm": {"method": "HF", "basis": "6-31G*", "charge": LOCAL_MODEL_CHARGE,
               "multiplicity": LOCAL_MODEL_MULTIPLICITY, "single_thread_esp": True,
               "geometry": "authoritative xTB-refined conformers; no additional QM optimization"},
        "jobs": jobs,
    }
    _write_json(paths["job_manifest"], manifest)
    return {"jobs_manifest": str(paths["job_manifest"]), "job_count": len(jobs),
            "total_grid_points": sum(job["grid_point_count"] for job in jobs)}


def _run_one_psi4(job: dict[str, Any], memory: str) -> dict[str, Any]:
    try:
        import numpy as np
        import psi4
    except ImportError as exc:
        raise WorkflowError("Psi4 and NumPy are required; run run-psi4 in tbmb_qm.") from exc
    result_path, esp_path, output_path = (_path(job["result_json"]), _path(job["esp"]),
                                          _path(job["psi4_output"]))
    _mkdir(result_path.parent)
    started = _now()
    result: dict[str, Any] = {
        "schema_version": 1, "job": job["qm_conformer_id"], "started_at_utc": started,
        "success": False, "method": job["qm_method"], "basis": job["qm_basis"],
        "threads": 1, "xyz_sha256": job["xyz_sha256"], "grid_sha256": job["grid_sha256"],
    }
    try:
        if _sha256(_path(job["xyz"])) != job["xyz_sha256"]:
            raise WorkflowError("XYZ hash changed after job preparation.")
        if _sha256(_path(job["grid_angstrom"])) != job["grid_sha256"]:
            raise WorkflowError("ESP grid hash changed after job preparation.")
        symbols, coordinates = _xyz_data(_path(job["xyz"]))
        geometry = [f"{LOCAL_MODEL_CHARGE} {LOCAL_MODEL_MULTIPLICITY}"]
        geometry.extend(f"{symbol} {x:.12f} {y:.12f} {z:.12f}"
                        for symbol, (x, y, z) in zip(symbols, coordinates))
        geometry.extend(["units angstrom", "symmetry c1", "no_reorient", "no_com"])
        psi4.core.clean()
        psi4.set_num_threads(1, quiet=True)
        psi4.set_memory(memory)
        psi4.core.set_output_file(str(output_path), False)
        molecule = psi4.geometry("\n".join(geometry))
        psi4.set_options({
            "basis": job["qm_basis"], "reference": "rhf", "scf_type": "df",
            "e_convergence": 1e-8, "d_convergence": 1e-8, "maxiter": 200,
        })
        energy, wavefunction = psi4.energy("scf", molecule=molecule, return_wfn=True)
        # Match PsiRESP 0.4.2 psi4utils.compute_esp: its MSK grid is stored in
        # Angstrom and passed directly to ESPPropCalc.
        grid_angstrom = np.loadtxt(job["grid_angstrom"], dtype=float)
        calculator = psi4.core.ESPPropCalc(wavefunction)
        esp = np.asarray(calculator.compute_esp_over_grid_in_memory(
            psi4.core.Matrix.from_array(grid_angstrom)
        ), dtype=float).reshape(-1)
        if len(esp) != int(job["grid_point_count"]) or not np.all(np.isfinite(esp)):
            raise WorkflowError("Psi4 returned an invalid ESP vector.")
        np.savetxt(esp_path, esp, fmt="%.16e")
        result.update({
            "success": True, "scf_converged": True, "energy_hartree": float(energy),
            "esp_point_count": len(esp), "esp": str(esp_path),
            "esp_sha256": _sha256(esp_path), "finished_at_utc": _now(),
        })
    except Exception as exc:  # noqa: BLE001 - preserve a machine-readable Psi4 failure
        result.update({"success": False, "scf_converged": False, "finished_at_utc": _now(),
                       "error_type": type(exc).__name__, "error": str(exc),
                       "traceback": traceback.format_exc()})
    _write_json(result_path, result)
    return result


def run_psi4(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    manifest = _require_pass(paths["job_manifest"], "Psi4 job preparation")
    jobs = manifest["jobs"]
    if args.only:
        jobs = [job for job in jobs if job["qm_conformer_id"] == args.only]
        if not jobs:
            raise WorkflowError(f"Unknown Psi4 job: {args.only}")
    results = []
    for job in jobs:
        result_path = _path(job["result_json"])
        if args.resume and result_path.is_file():
            old = _read_json(result_path)
            if (old.get("success") is True
                    and old.get("xyz_sha256") == job["xyz_sha256"]
                    and old.get("grid_sha256") == job["grid_sha256"]
                    and _path(old.get("esp", "")).is_file()
                    and _sha256(_path(old["esp"])) == old.get("esp_sha256")):
                results.append(old); continue
        print(f"[psi4] {job['qm_conformer_id']} HF/6-31G*", flush=True)
        result = _run_one_psi4(job, args.memory)
        results.append(result)
        if not result["success"] and not args.keep_going:
            raise WorkflowError(f"Psi4 failed for {job['qm_conformer_id']}; see {job['result_json']}")
    failed = [item["job"] for item in results if not item.get("success")]
    return {"attempted": len(results), "succeeded": len(results) - len(failed), "failed": failed,
            "qm_logs": str(paths["qm_logs"])}


def collect_psi4(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError as exc:
        raise WorkflowError("NumPy is required.") from exc
    paths = _layout(_path(args.root))
    manifest = _require_pass(paths["job_manifest"], "Psi4 job preparation")
    failures, records = [], []
    for job in manifest["jobs"]:
        result_path = _path(job["result_json"])
        if not result_path.is_file():
            failures.append(f"missing result for {job['qm_conformer_id']}"); continue
        result = _read_json(result_path)
        if result.get("success") is not True or result.get("scf_converged") is not True:
            failures.append(f"unconverged result for {job['qm_conformer_id']}"); continue
        esp_path = _path(result.get("esp", ""))
        if not esp_path.is_file() or _sha256(esp_path) != result.get("esp_sha256"):
            failures.append(f"missing or modified ESP for {job['qm_conformer_id']}"); continue
        esp = np.loadtxt(esp_path, dtype=float).reshape(-1)
        if len(esp) != int(job["grid_point_count"]) or not np.all(np.isfinite(esp)):
            failures.append(f"invalid ESP array for {job['qm_conformer_id']}"); continue
        energy = result.get("energy_hartree")
        if not isinstance(energy, (int, float)) or not math.isfinite(float(energy)):
            failures.append(f"invalid SCF energy for {job['qm_conformer_id']}"); continue
        records.append({"job": job["qm_conformer_id"], "energy_hartree": energy,
                        "grid_points": len(esp), "esp": str(esp_path), "esp_sha256": _sha256(esp_path)})
    report = {"schema_version": 1, "created_at_utc": _now(), "passed": not failures,
              "electronic_convergence_verified": not failures and len(records) == len(manifest["jobs"]),
              "method": "HF/6-31G*", "records": records, "failures": failures}
    _write_json(paths["qm_collection"], report)
    if failures:
        raise WorkflowError(f"Psi4 collection failed; see {paths['qm_collection']}")
    return {"qm_collection": str(paths["qm_collection"]), "converged": len(records)}


def fit_resp(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import numpy as np
        import psiresp
    except ImportError as exc:
        raise WorkflowError("PsiRESP and NumPy are required; run fit-resp in tbmb_resp.") from exc
    paths = _layout(_path(args.root))
    _require_pass(paths["qm_collection"], "Psi4 collection")
    selection = _require_pass(paths["selection"], "QM model construction")
    jobs = _require_pass(paths["job_manifest"], "Psi4 job preparation")["jobs"]
    symbols, first_coordinates = _xyz_data(_path(jobs[0]["xyz"]))
    molecule = psiresp.Molecule(
        qcmol=_qcmol(symbols, first_coordinates, LOCAL_MODEL_CHARGE,
                     selection.get("model_connectivity")),
        charge=LOCAL_MODEL_CHARGE, multiplicity=LOCAL_MODEL_MULTIPLICITY,
        optimize_geometry=False, keep_original_orientation=True,
    )
    for job in jobs:
        _, coordinates = _xyz_data(_path(job["xyz"]))
        molecule.add_conformer_with_coordinates(np.asarray(coordinates), units="angstrom")
    molecule.generate_orientations()
    if len(molecule.conformers) != len(jobs):
        raise WorkflowError("PsiRESP conformer count differs from prepared Psi4 jobs.")
    for conformer, job in zip(molecule.conformers, jobs):
        if len(conformer.orientations) != 1:
            raise WorkflowError("Expected one preserved orientation per conformer.")
        orientation = conformer.orientations[0]
        orientation.grid = np.loadtxt(job["grid_angstrom"], dtype=float)
        orientation.esp = np.loadtxt(job["esp"], dtype=float).reshape(-1)
    constraints = psiresp.ChargeConstraintOptions(
        symmetric_methyls=True, symmetric_methylenes=True,
        symmetric_atoms_are_equivalent=True, split_conformers=False,
    )
    atom_map = selection["model_atom_map"]
    # Three artificial CB methyl caps are constrained to net zero and are excluded
    # from charge transfer to the ff14SB residue templates.
    cap_groups = []
    for position in selection["model_definition"]["met_positions"]:
        prefix = f"PEP:{position}:CB"
        indices = [int(item["model_atom_index"]) for item in atom_map
                   if item["stable_id"] == prefix or item["stable_id"].startswith(prefix + ":H")]
        if len(indices) < 2:
            raise WorkflowError(f"Boundary cap atom group is incomplete: {prefix}")
        constraints.add_charge_sum_constraint_for_molecule(molecule, charge=0.0, indices=indices)
        cap_groups.append({"stable_id_prefix": prefix, "indices": indices, "charge": 0.0})
    job = psiresp.Job(molecules=[molecule], charge_constraints=constraints,
                      working_directory=paths["resp"])
    fitted = job.compute_charges(update_molecules=True)
    charges = np.asarray(fitted[0] if isinstance(fitted, (list, tuple)) or np.asarray(fitted).ndim > 1 else fitted,
                         dtype=float).reshape(-1)
    if len(charges) != len(atom_map):
        raise WorkflowError(f"PsiRESP returned {len(charges)} charges for {len(atom_map)} atoms.")
    total = float(charges.sum())
    if abs(total - LOCAL_MODEL_CHARGE) > 1e-5:
        raise WorkflowError(f"RESP total charge {total:.8f} differs from {LOCAL_MODEL_CHARGE}.")
    rows = []
    for item, charge in zip(atom_map, charges):
        row = {**item, "resp_charge": float(charge), "transferable": not bool(item["boundary_cap"])}
        rows.append(row)
    charge_output = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "method": "multi-conformer two-stage RESP", "qm_level": "HF/6-31G*",
        "conformer_count": len(jobs), "local_model_charge": LOCAL_MODEL_CHARGE,
        "fitted_charge_sum": total,
        "resp_options": {
            "restraint_slope": float(job.resp_options.restraint_slope),
            "restraint_height_stage_1": float(job.resp_options.restraint_height_stage_1),
            "restraint_height_stage_2": float(job.resp_options.restraint_height_stage_2),
            "exclude_hydrogens_from_restraint": bool(job.resp_options.exclude_hydrogens),
        },
        "constraints": {
            "symmetric_methyls": True, "symmetric_methylenes": True,
            "symmetric_atoms_are_equivalent": True, "split_conformers": False,
            "zero_charge_boundary_caps": cap_groups,
        },
        "transfer_rule": "Transfer only TBM and Met CG/SD/CE groups; retain ff14SB backbone/CB and reconcile integer residue/template charges.",
        "atoms": rows,
    }
    _write_json(paths["charges"], charge_output)
    with paths["charges_csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["model_atom_index", "stable_id", "element",
                                                    "resp_charge", "transferable", "boundary_cap"])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in writer.fieldnames})
    audit = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "charge_constraints_verified": True,
        "charge_sum": total, "expected_charge": LOCAL_MODEL_CHARGE,
        "boundary_cap_sums": {
            group["stable_id_prefix"]: float(sum(charges[index] for index in group["indices"]))
            for group in cap_groups
        },
        "charges_sha256": _sha256(paths["charges"]),
        "limitations": [
            "These are local-model charges, not complete MSU/CMU/TMB library files.",
            "A qualified reviewer must approve charge transfer and integer template-charge reconciliation.",
            "Experimentally unassigned sulfur stereochemistry is not resolved by RESP.",
        ],
    }
    _write_json(paths["charge_audit"], audit)
    return {"charges": str(paths["charges"]), "charges_csv": str(paths["charges_csv"]),
            "charge_audit": str(paths["charge_audit"]), "charge_sum": total, "atoms": len(rows)}


def _neighbors_by_stable_id(model: Any) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for atom in model.GetAtoms():
        stable_id = atom.GetProp("stable_id")
        output[stable_id] = sorted(neighbor.GetProp("stable_id") for neighbor in atom.GetNeighbors())
    return output


def prepare_bonded_scans(args: argparse.Namespace) -> dict[str, Any]:
    Chem, _ = _import_rdkit()
    paths = _layout(_path(args.root))
    selection = _require_pass(paths["selection"], "QM model construction")
    model = Chem.SDMolSupplier(selection["selected_conformers"][0]["sdf"], removeHs=False)[0]
    if model is None:
        raise WorkflowError("Cannot read the first local QM model.")
    if model.GetNumAtoms() != len(selection["model_atom_map"]):
        raise WorkflowError("Local model atom count no longer matches the recorded atom map.")
    for atom, item in zip(model.GetAtoms(), selection["model_atom_map"]):
        atom.SetProp("stable_id", str(item["stable_id"]))
    neighbors = _neighbors_by_stable_id(model)
    tasks, seen = [], set()
    for position, arm in zip(selection["model_definition"]["met_positions"], ("CM1", "CM2", "CM3")):
        sulfur, carbon = f"PEP:{position}:SD", f"TBM:{arm}"
        if carbon not in neighbors.get(sulfur, []):
            raise WorkflowError(f"Missing local-model crosslink bond {sulfur}--{carbon}")
        candidates: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
        candidates.append(("bond", (sulfur, carbon), {"range_angstrom": [1.65, 2.45], "step": 0.05}))
        for left in neighbors[sulfur]:
            if left != carbon:
                candidates.append(("angle", (left, sulfur, carbon), {"range_degree": [70, 150], "step": 5}))
        for right in neighbors[carbon]:
            if right != sulfur:
                candidates.append(("angle", (sulfur, carbon, right), {"range_degree": [80, 160], "step": 5}))
        for left in neighbors[sulfur]:
            for right in neighbors[carbon]:
                if left != carbon and right != sulfur:
                    candidates.append(("dihedral", (left, sulfur, carbon, right),
                                       {"range_degree": [-180, 180], "step": 15}))
        for kind, atoms, scan in candidates:
            # Keep each chemically equivalent arm explicit; reviewers may later tie
            # terms only after confirming equivalent atom types and scan profiles.
            key = (kind, atoms)
            if key in seen:
                continue
            seen.add(key)
            tasks.append({
                "term_id": f"{kind}_{len(tasks) + 1:03d}", "kind": kind,
                "atoms": list(atoms), "representative_crosslink": position,
                "scan": scan, "qm_recommendation": "relaxed constrained scan; fit against QM-minus-MM",
                "status": "planned", "reviewed": False,
            })
    plan = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "model": selection["selected_conformers"][0]["sdf"],
        "qm_recommendation": {
            "minimum_level": "HF/6-31G* for consistency with RESP; higher-level validation recommended",
            "do_not_do": "Do not infer zero or placeholder force constants from RESP charges.",
            "analogue_policy": "ff14SB/GAFF2 analogues require explicit citation and qualified review.",
        },
        "tasks": tasks,
    }
    _write_json(paths["scan_plan"], plan)
    if not paths["scan_review"].exists():
        review_terms = []
        for task in tasks:
            item: dict[str, Any] = {
                "term_id": task["term_id"], "kind": task["kind"], "atoms": task["atoms"],
                "source": "", "reviewed": False, "reviewed_by": "", "notes": "",
            }
            if task["kind"] == "bond":
                item.update({"equilibrium_angstrom": None, "force_constant_kcal_mol_a2": None})
            elif task["kind"] == "angle":
                item.update({"equilibrium_degree": None, "force_constant_kcal_mol_rad2": None})
            else:
                item["fourier_terms"] = []
            review_terms.append(item)
        _write_json(paths["scan_review"], {
            "schema_version": 1,
            "instructions": "Populate only from real QM scans or cited analogues; then obtain qualified review.",
            "terms": review_terms,
        })
    return {"bonded_scan_plan": str(paths["scan_plan"]), "planned_terms": len(tasks),
            "review_input_expected": str(paths["scan_review"])}


def prepare_bonded_qm(args: argparse.Namespace) -> dict[str, Any]:
    """Materialize constrained relaxed-scan jobs from the bonded-term plan."""
    Chem, _ = _import_rdkit()
    from rdkit.Chem import rdMolTransforms

    paths = _layout(_path(args.root))
    plan = _require_pass(paths["scan_plan"], "Bonded scan planning")
    selection = _require_pass(paths["selection"], "QM model construction")
    model = Chem.SDMolSupplier(selection["selected_conformers"][0]["sdf"], removeHs=False)[0]
    if model is None:
        raise WorkflowError("Cannot read local model for bonded scans.")
    stable_to_index = {str(item["stable_id"]): int(item["model_atom_index"])
                       for item in selection["model_atom_map"]}
    tasks = list(plan["tasks"])
    if not args.all_arms:
        representative = int(selection["model_definition"]["met_positions"][0])
        tasks = [task for task in tasks if int(task["representative_crosslink"]) == representative]
    selected_terms = set(args.terms or [])
    if selected_terms:
        tasks = [task for task in tasks if task["term_id"] in selected_terms]
        unknown = selected_terms - {task["term_id"] for task in plan["tasks"]}
        if unknown:
            raise WorkflowError("Unknown bonded term IDs: " + ", ".join(sorted(unknown)))
    if not tasks:
        raise WorkflowError("No bonded scan tasks selected.")
    _mkdir(paths["scan_jobs"])
    jobs = []
    for task in tasks:
        indices = [stable_to_index.get(value) for value in task["atoms"]]
        if any(value is None for value in indices):
            raise WorkflowError(f"Bonded task {task['term_id']} contains an unknown stable atom ID.")
        start, stop = [float(value) for value in task["scan"][next(key for key in task["scan"] if key.startswith("range_"))]]
        step = float(task["scan"]["step"])
        point_count = round((stop - start) / step) + 1
        values = [start + index * step for index in range(point_count)]
        conformer = model.GetConformer()
        if task["kind"] == "bond":
            initial_value = float(rdMolTransforms.GetBondLength(conformer, *indices))
        elif task["kind"] == "angle":
            initial_value = float(rdMolTransforms.GetAngleDeg(conformer, *indices))
        else:
            initial_value = float(rdMolTransforms.GetDihedralDeg(conformer, *indices))

        # A relaxed scan must advance continuously from the nearest grid point.
        # Driving a cyclic, charged 54-atom model directly from its starting
        # geometry to an extreme endpoint is prone to OptKing back-transform
        # failures.  Build two branches that both start at the nearest point.
        nearest_index = min(range(len(values)), key=lambda index: abs(values[index] - initial_value))
        continuation: list[tuple[float, int | None]] = [(values[nearest_index], None)]
        previous_point = 1
        for value_index in range(nearest_index - 1, -1, -1):
            continuation.append((values[value_index], previous_point))
            previous_point = len(continuation)
        previous_point = 1
        for value_index in range(nearest_index + 1, len(values)):
            continuation.append((values[value_index], previous_point))
            previous_point = len(continuation)

        for point_index, (target, parent_point) in enumerate(continuation, 1):
            scanned = Chem.Mol(model)
            # Modern Python OptKing removed the old FIXED_* options in favour
            # of RANGED_*.  A very narrow interval is the supported way to
            # drive a relaxed scan to a requested coordinate value.
            if task["kind"] == "bond":
                constraint = "ranged_distance"
            elif task["kind"] == "angle":
                constraint = "ranged_bend"
            else:
                constraint = "ranged_dihedral"
            directory = _mkdir(paths["scan_jobs"] / task["term_id"] / f"point_{point_index:03d}")
            xyz = directory / "input.xyz"
            # OptKing's FIXED_* keyword contains the requested target value and
            # drives the coordinate during optimization.  Keeping the same
            # authoritative starting geometry avoids illegal Cartesian edits of
            # bonds/dihedrals that belong to the bicyclic ring system.
            _write_xyz(scanned, xyz)
            result = paths["scan_logs"] / task["term_id"] / f"point_{point_index:03d}.json"
            job = {
                "job_id": f"{task['term_id']}_point_{point_index:03d}",
                "term_id": task["term_id"], "kind": task["kind"], "atoms": task["atoms"],
                "atom_indices_zero_based": indices,
                "constraint_option": constraint,
                "constraint_atoms_one_based": [int(value) + 1 for value in indices],
                "target": float(target),
                "target_unit": "angstrom" if task["kind"] == "bond" else "degree",
                "xyz": str(xyz), "xyz_sha256": _sha256(xyz),
                "result_json": str(result),
                "psi4_output": str(result.with_suffix(".out")),
                "initial_coordinate_value": initial_value,
                "continuation_parent_job_id": (
                    f"{task['term_id']}_point_{parent_point:03d}" if parent_point is not None else None
                ),
                "continuation_parent_result": (
                    str(paths["scan_logs"] / task["term_id"] / f"point_{parent_point:03d}.json")
                    if parent_point is not None else None
                ),
            }
            job["job_signature"] = _bonded_job_signature(job)
            jobs.append(job)
    manifest = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "qm": {"method": "HF", "basis": "6-31G*", "charge": LOCAL_MODEL_CHARGE,
               "multiplicity": LOCAL_MODEL_MULTIPLICITY, "relaxed_constrained_scan": True},
        "selection": {"all_arms": bool(args.all_arms), "term_ids": [task["term_id"] for task in tasks]},
        "warning": "Representative-arm scans assume local chemical equivalence; validate other arms before production use.",
        "jobs": jobs,
    }
    _write_json(paths["scan_job_manifest"], manifest)
    return {"bonded_qm_jobs": str(paths["scan_job_manifest"]), "terms": len(tasks),
            "jobs": len(jobs), "all_arms": bool(args.all_arms)}


def _bonded_job_signature(job: dict[str, Any]) -> str:
    fields = {
        "job_id": job.get("job_id"), "term_id": job.get("term_id"),
        "kind": job.get("kind"), "atoms": job.get("atoms"),
        "constraint_option": job.get("constraint_option"),
        "constraint_atoms_one_based": job.get("constraint_atoms_one_based"),
        "target": job.get("target"), "target_unit": job.get("target_unit"),
        "xyz_sha256": job.get("xyz_sha256"),
        "continuation_parent_job_id": job.get("continuation_parent_job_id"),
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _optking_ranged_constraint(job: dict[str, Any]) -> tuple[str, str, float]:
    """Return a version-compatible narrow OptKing range for one scan point.

    Existing manifests written before the OptKing compatibility fix contain
    ``fixed_*`` names.  Accept those names as aliases so a 252-job manifest
    does not need to be regenerated solely because the Psi4 environment uses
    the current Python OptKing interface.
    """
    aliases = {
        "fixed_distance": "ranged_distance",
        "fixed_bend": "ranged_bend",
        "fixed_dihedral": "ranged_dihedral",
        "ranged_distance": "ranged_distance",
        "ranged_bend": "ranged_bend",
        "ranged_dihedral": "ranged_dihedral",
    }
    recorded = str(job.get("constraint_option", ""))
    try:
        option = aliases[recorded]
    except KeyError as exc:
        raise WorkflowError(f"Unsupported OptKing constraint option in bonded-QM job: {recorded!r}") from exc

    atoms = [int(value) for value in job["constraint_atoms_one_based"]]
    expected_atoms = {"ranged_distance": 2, "ranged_bend": 3, "ranged_dihedral": 4}[option]
    if len(atoms) != expected_atoms:
        raise WorkflowError(
            f"{option} requires {expected_atoms} atom indices, but {job.get('job_id')} has {len(atoms)}."
        )
    target = float(job["target"])
    # The official OptKing examples use a 0.001-A distance interval.  Use the
    # same width for bonds and a 0.01-degree interval for angular coordinates.
    half_width = 0.0005 if option == "ranged_distance" else 0.005
    lower, upper = target - half_width, target + half_width
    value = " ".join([*(str(index) for index in atoms), f"{lower:.10f}", f"{upper:.10f}"])
    return option, value, half_width


def _geometric_set_constraint(job: dict[str, Any]) -> dict[str, Any]:
    """Build the zero-based constraint dictionary expected by Psi4/geomeTRIC."""
    kind = str(job.get("kind", ""))
    constraint_type = {"bond": "distance", "angle": "angle", "dihedral": "dihedral"}.get(kind)
    if constraint_type is None:
        raise WorkflowError(f"Unsupported bonded-QM coordinate kind: {kind!r}")
    indices = [int(value) for value in job["atom_indices_zero_based"]]
    expected = {"distance": 2, "angle": 3, "dihedral": 4}[constraint_type]
    if len(indices) != expected:
        raise WorkflowError(
            f"geomeTRIC {constraint_type} requires {expected} atom indices, "
            f"but {job.get('job_id')} has {len(indices)}."
        )
    target = float(job["target"])
    # Psi4's direct geomeTRIC dictionary follows the QCSchema convention:
    # distances enter in bohr and are converted to Angstrom while the
    # constraints string is assembled; angles and dihedrals enter in degrees.
    if constraint_type == "distance":
        target /= 0.529177210903
    return {"type": constraint_type, "indices": indices, "value": target}


def _bonded_execution_signature(job: dict[str, Any], optimizer: str) -> str:
    value = {
        "job_signature": job.get("job_signature") or _bonded_job_signature(job),
        "optimizer": optimizer, "method": "HF", "basis": "6-31G*",
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run_one_bonded_qm(
    job: dict[str, Any], memory: str, threads: int, optimizer: str
) -> dict[str, Any]:
    try:
        import numpy as np
        import psi4
    except ImportError as exc:
        raise WorkflowError("Psi4 and NumPy are required; run run-bonded-qm in tbmb_qm.") from exc
    result_path = _path(job["result_json"])
    output_path = _path(job["psi4_output"])
    _mkdir(result_path.parent)
    job_signature = str(job.get("job_signature") or _bonded_job_signature(job))
    execution_signature = _bonded_execution_signature(job, optimizer)
    result = {
        "schema_version": 1, "job_id": job["job_id"], "term_id": job["term_id"],
        "target": job["target"], "target_unit": job["target_unit"],
        "xyz_sha256": job["xyz_sha256"], "job_signature": job_signature,
        "execution_signature": execution_signature, "optimizer": optimizer,
        "requested_memory": memory, "threads": int(threads),
        "success": False, "started_at_utc": _now(),
    }
    try:
        source_xyz = _path(job["xyz"])
        if _sha256(source_xyz) != job["xyz_sha256"]:
            raise WorkflowError("Bonded-scan XYZ changed after preparation.")
        xyz = source_xyz
        parent_result_path = job.get("continuation_parent_result")
        if parent_result_path:
            parent_result = _read_json(_path(parent_result_path))
            if parent_result.get("success") is not True:
                raise WorkflowError(
                    f"Continuation parent {job.get('continuation_parent_job_id')} did not converge."
                )
            xyz = _path(parent_result.get("final_xyz", ""))
            if not xyz.is_file() or _sha256(xyz) != parent_result.get("final_xyz_sha256"):
                raise WorkflowError(
                    f"Continuation parent geometry is missing or changed: {xyz}"
                )
        result["starting_xyz"] = str(xyz)
        result["starting_xyz_sha256"] = _sha256(xyz)
        symbols, coordinates = _xyz_data(xyz)
        geometry = [f"{LOCAL_MODEL_CHARGE} {LOCAL_MODEL_MULTIPLICITY}"]
        geometry.extend(f"{symbol} {x:.12f} {y:.12f} {z:.12f}"
                        for symbol, (x, y, z) in zip(symbols, coordinates))
        geometry.extend(["units angstrom", "symmetry c1", "no_reorient", "no_com"])
        psi4.core.clean()
        # set_options() is process-global.  Without resetting it, a distance
        # constraint from one scan point can survive into a later bend or
        # dihedral job when all jobs run in one Python process.
        psi4.core.clean_options()
        psi4.set_num_threads(int(threads), quiet=True); psi4.set_memory(memory)
        psi4.core.set_output_file(str(output_path), False)
        molecule = psi4.geometry("\n".join(geometry))
        options = {
            "basis": "6-31G*", "reference": "rhf", "scf_type": "df",
            "e_convergence": 1e-8, "d_convergence": 1e-8, "maxiter": 200,
            "geom_maxiter": 150,
        }
        psi4.set_options(options)
        if optimizer == "geometric":
            geometric_constraint = _geometric_set_constraint(job)
            energy, _wavefunction = psi4.optimize(
                "scf", molecule=molecule, return_wfn=True, engine="geometric",
                optimizer_keywords={
                    "coordsys": "tric", "maxiter": 150, "convergence_set": "GAU_TIGHT",
                    "enforce": 0.1, "constraints": {"set": [geometric_constraint]},
                },
            )
            constraint_option = f"geometric_set_{geometric_constraint['type']}"
            constraint_value: Any = geometric_constraint
            constraint_half_width: float | None = None
        else:
            constraint_option, constraint_value, constraint_half_width = _optking_ranged_constraint(job)
            psi4.set_options({
                "optking__intrafrag_step_limit": 0.10,
                "optking__dynamic_level": 1,
                "optking__ensure_bt_convergence": True,
                f"optking__{constraint_option}": constraint_value,
            })
            energy, _wavefunction = psi4.optimize("scf", molecule=molecule, return_wfn=True)
        bohr_to_angstrom = 0.529177210903
        final_xyz = np.asarray(molecule.geometry().to_array(), dtype=float) * bohr_to_angstrom
        final_path = result_path.with_suffix(".xyz")
        lines = [str(len(symbols)), f"relaxed constrained scan {job['job_id']}"]
        lines.extend(f"{symbol:<2s} {point[0]: .10f} {point[1]: .10f} {point[2]: .10f}"
                     for symbol, point in zip(symbols, final_xyz))
        final_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result.update({
            "success": True, "scf_converged": True, "geometry_optimized": True,
            "energy_hartree": float(energy), "final_xyz": str(final_path),
            "final_xyz_sha256": _sha256(final_path), "finished_at_utc": _now(),
            "constraint_option_used": constraint_option,
            "constraint_value_used": constraint_value,
            "constraint_half_width": constraint_half_width,
        })
    except Exception as exc:  # noqa: BLE001 - preserve a machine-readable Psi4 failure
        result.update({"success": False, "finished_at_utc": _now(),
                       "error_type": type(exc).__name__, "error": str(exc),
                       "traceback": traceback.format_exc()})
    _write_json(result_path, result)
    return result


def run_bonded_qm(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    if int(args.threads) < 1:
        raise WorkflowError("--threads must be a positive integer.")
    manifest = _require_pass(paths["scan_job_manifest"], "Bonded QM job preparation")
    jobs = manifest["jobs"]
    if args.only:
        jobs = [job for job in jobs if job["job_id"] == args.only or job["term_id"] == args.only]
        if not jobs:
            raise WorkflowError(f"Unknown bonded QM job/term: {args.only}")
    results = []
    for index, job in enumerate(jobs, 1):
        result_path = _path(job["result_json"])
        if args.resume and result_path.is_file():
            old = _read_json(result_path)
            final = _path(old.get("final_xyz", ""))
            if (old.get("success") is True and old.get("xyz_sha256") == job["xyz_sha256"]
                    and old.get("job_signature") == (job.get("job_signature") or _bonded_job_signature(job))
                    and old.get("execution_signature") == _bonded_execution_signature(job, args.optimizer)
                    and final.is_file() and _sha256(final) == old.get("final_xyz_sha256")):
                results.append(old); continue
        print(
            f"[bonded-qm] {index}/{len(jobs)} {job['job_id']} "
            f"target={job['target']} {job['target_unit']} threads={args.threads} "
            f"optimizer={args.optimizer} log={job['psi4_output']}",
            flush=True,
        )
        result = _run_one_bonded_qm(job, args.memory, int(args.threads), args.optimizer)
        results.append(result)
        if not result["success"] and not args.keep_going:
            raise WorkflowError(f"Bonded QM failed for {job['job_id']}; see {job['result_json']}")
    failed = [item["job_id"] for item in results if not item.get("success")]
    return {"attempted": len(results), "succeeded": len(results) - len(failed),
            "failed": failed, "bonded_qm_logs": str(paths["scan_logs"])}


def collect_bonded_qm(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    manifest = _require_pass(paths["scan_job_manifest"], "Bonded QM job preparation")
    requested_values = list(getattr(args, "exclude_failed_job", None) or [])
    exclusion_reason = str(getattr(args, "exclusion_reason", None) or "").strip()
    exclusion_recorded_by = str(getattr(args, "exclusion_recorded_by", None) or "").strip()
    requested_exclusions = list(dict.fromkeys(requested_values))
    if len(requested_exclusions) != len(requested_values):
        raise WorkflowError("--exclude-failed-job contains duplicate job IDs.")
    if requested_exclusions and not exclusion_reason:
        raise WorkflowError("--exclusion-reason is required when excluding failed bonded-QM jobs.")
    if requested_exclusions and not exclusion_recorded_by:
        raise WorkflowError("--exclusion-recorded-by is required for exclusion provenance.")

    jobs_by_id = {job["job_id"]: job for job in manifest["jobs"]}
    unknown_exclusions = sorted(set(requested_exclusions) - set(jobs_by_id))
    if unknown_exclusions:
        raise WorkflowError("Unknown bonded-QM exclusion job IDs: " + ", ".join(unknown_exclusions))

    planned_targets: dict[str, list[float]] = {}
    for job in manifest["jobs"]:
        planned_targets.setdefault(job["term_id"], []).append(float(job["target"]))
    exclusions_by_term: dict[str, list[str]] = {}
    for job_id in requested_exclusions:
        job = jobs_by_id[job_id]
        if job["kind"] not in {"bond", "angle"}:
            raise WorkflowError(
                f"Excluding {job_id} is not allowed: dihedral scans require complete periodic coverage."
            )
        targets = planned_targets[job["term_id"]]
        if float(job["target"]) not in {min(targets), max(targets)}:
            raise WorkflowError(
                f"Excluding {job_id} is not allowed: only terminal bond/angle scan points may be excluded."
            )
        exclusions_by_term.setdefault(job["term_id"], []).append(job_id)
    multiple = {term_id: ids for term_id, ids in exclusions_by_term.items() if len(ids) > 1}
    if multiple:
        details = "; ".join(f"{term_id}: {', '.join(ids)}" for term_id, ids in sorted(multiple.items()))
        raise WorkflowError("At most one terminal point may be excluded per bonded term; got " + details)

    records, exclusions, failures = [], [], []
    for job in manifest["jobs"]:
        result_path = _path(job["result_json"])
        if not result_path.is_file():
            failures.append(f"missing {job['job_id']}"); continue
        result = _read_json(result_path)
        expected_signature = job.get("job_signature") or _bonded_job_signature(job)
        if (result.get("job_signature") != expected_signature
                or result.get("xyz_sha256") != job.get("xyz_sha256")):
            failures.append(f"stale/mismatched scan definition {job['job_id']}"); continue
        converged = result.get("success") is True and math.isfinite(
            float(result.get("energy_hartree", math.nan))
        )
        if not converged:
            if job["job_id"] in requested_exclusions:
                exclusions.append({
                    "job_id": job["job_id"], "term_id": job["term_id"], "kind": job["kind"],
                    "atoms": job["atoms"], "target": job["target"],
                    "target_unit": job["target_unit"], "error_type": result.get("error_type"),
                    "error": result.get("error"), "result_json": str(result_path),
                    "result_sha256": _sha256(result_path),
                    "reason": exclusion_reason,
                    "recorded_by": exclusion_recorded_by,
                })
                continue
            failures.append(f"failed/unconverged {job['job_id']}"); continue
        if job["job_id"] in requested_exclusions:
            failures.append(f"refused exclusion of converged job {job['job_id']}"); continue
        records.append({
            "job_id": job["job_id"], "term_id": job["term_id"], "kind": job["kind"],
            "atoms": job["atoms"], "target": job["target"], "target_unit": job["target_unit"],
            "energy_hartree": float(result["energy_hartree"]),
            "result_json": str(result_path), "result_sha256": _sha256(result_path),
        })

    excluded_ids = {item["job_id"] for item in exclusions}
    unaccounted = sorted(set(requested_exclusions) - excluded_ids)
    if unaccounted:
        failures.append("requested exclusions were not accepted: " + ", ".join(unaccounted))
    record_count_by_term: dict[str, int] = {}
    for record in records:
        record_count_by_term[record["term_id"]] = record_count_by_term.get(record["term_id"], 0) + 1
    for term_id in exclusions_by_term:
        if record_count_by_term.get(term_id, 0) < 7:
            failures.append(f"too few converged points remain for provisional fitting of {term_id}")

    report = {
        "schema_version": 2, "created_at_utc": _now(), "passed": not failures,
        "all_jobs_converged": not failures and not exclusions
            and len(records) == len(manifest["jobs"]),
        "collection_complete_for_provisional_fitting": not failures
            and len(records) + len(exclusions) == len(manifest["jobs"]),
        "records": records, "excluded_failed_terminal_points": exclusions,
        "exclusion_policy": {
            "provisional_fitting_only": True,
            "production_parameter_approval": False,
            "reason": exclusion_reason or None,
            "recorded_by": exclusion_recorded_by or None,
            "requirements": [
                "only failed or unconverged bond/angle terminal points",
                "at most one excluded terminal point per term",
                "at least seven converged points remain for each affected term",
                "dihedral exclusions are forbidden",
            ],
        },
        "failures": failures,
    }
    _write_json(paths["scan_collection"], report)
    if failures:
        raise WorkflowError(f"Bonded QM collection is incomplete; see {paths['scan_collection']}")
    return {
        "bonded_qm_collection": str(paths["scan_collection"]),
        "planned_jobs": len(manifest["jobs"]), "converged_jobs": len(records),
        "excluded_failed_terminal_points": len(exclusions),
        "all_jobs_converged": report["all_jobs_converged"],
        "approved_for_production": False,
    }


def _canonical_type_key(types: list[str]) -> tuple[str, ...]:
    forward, reverse = tuple(types), tuple(reversed(types))
    return min(forward, reverse)


def _fit_provisional_raw_qm_profile(np: Any, kind: str, x: Any, relative: Any) -> dict[str, Any]:
    """Fit one pooled raw-QM profile without implying scientific approval."""
    minimum_index = int(np.argmin(relative))
    candidate: dict[str, Any] = {}
    if kind in {"bond", "angle"}:
        order = np.argsort(np.abs(np.arange(len(x)) - minimum_index))[:min(7, len(x))]
        coordinate = x[order]
        if kind == "angle":
            coordinate = np.deg2rad(coordinate)
        coefficients = np.polyfit(coordinate, relative[order], 2)
        vertex = -coefficients[1] / (2.0 * coefficients[0]) if coefficients[0] > 0 else math.nan
        if kind == "bond":
            candidate.update({"equilibrium_angstrom": float(vertex),
                              "force_constant_kcal_mol_a2": float(coefficients[0])})
        else:
            candidate.update({"equilibrium_degree": float(np.rad2deg(vertex)),
                              "force_constant_kcal_mol_rad2": float(coefficients[0])})
        candidate["positive_curvature"] = bool(coefficients[0] > 0)
        return candidate

    phi = np.deg2rad(x)
    design = [np.ones_like(phi)]
    for periodicity in (1, 2, 3):
        design.extend([np.cos(periodicity * phi), np.sin(periodicity * phi)])
    coefficients, *_ = np.linalg.lstsq(np.column_stack(design), relative, rcond=None)
    fourier = []
    for index, periodicity in enumerate((1, 2, 3)):
        cosine, sine = coefficients[1 + index * 2:3 + index * 2]
        fourier.append({
            "periodicity": periodicity,
            "amplitude_kcal_mol": float(math.hypot(cosine, sine)),
            "phase_degree": float(math.degrees(math.atan2(sine, cosine))),
            "idivf": 1,
        })
    candidate["raw_qm_fourier_terms"] = fourier
    return candidate


def fit_bonded_candidates(args: argparse.Namespace) -> dict[str, Any]:
    """Fit type-consistent provisional raw-QM profiles; never approve parameters."""
    try:
        import numpy as np
    except ImportError as exc:
        raise WorkflowError("NumPy is required to fit bonded scan candidates.") from exc
    paths = _layout(_path(args.root))
    collection = _require_pass(paths["scan_collection"], "Bonded QM collection")
    plan = _require_pass(paths["scan_plan"], "Bonded scan planning")
    type_report = _read_json(paths["amber_atom_types"])
    stable_to_type = {
        str(item["stable_id"]): str(item["hybrid_type"])
        for item in type_report.get("model_atom_types", [])
    }
    if not stable_to_type:
        raise WorkflowError(
            "AMBER hybrid atom types are required before bonded fitting; run build-amber-candidates first."
        )

    by_term: dict[str, list[dict[str, Any]]] = {}
    for record in collection["records"]:
        by_term.setdefault(record["term_id"], []).append(record)
    exclusions_by_term: dict[str, list[dict[str, Any]]] = {}
    for item in collection.get("excluded_failed_terminal_points", []):
        exclusions_by_term.setdefault(str(item.get("term_id")), []).append(item)

    hartree_to_kcal = 627.5094740631
    representative_profiles = []
    profiles_by_type: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    task_type_keys: dict[str, tuple[str, ...]] = {}
    task_amber_types: dict[str, list[str]] = {}
    for task in plan["tasks"]:
        missing = [atom for atom in task["atoms"] if atom not in stable_to_type]
        if missing:
            raise WorkflowError(f"Bonded task {task['term_id']} lacks AMBER types for {missing}")
        amber_types = [stable_to_type[atom] for atom in task["atoms"]]
        type_key = _canonical_type_key(amber_types)
        task_type_keys[task["term_id"]] = type_key
        task_amber_types[task["term_id"]] = amber_types
        points = sorted(by_term.get(task["term_id"], []), key=lambda item: item["target"])
        if not points:
            continue
        x = np.asarray([item["target"] for item in points], dtype=float)
        energy = np.asarray([item["energy_hartree"] for item in points], dtype=float)
        relative = (energy - energy.min()) * hartree_to_kcal
        profile = {
            "term_id": task["term_id"], "kind": task["kind"], "atoms": task["atoms"],
            "amber_types": amber_types, "canonical_amber_type_key": list(type_key),
            "targets": [float(value) for value in x],
            "relative_energy_kcal_mol": [float(value) for value in relative],
            "excluded_failed_terminal_points": exclusions_by_term.get(task["term_id"], []),
        }
        representative_profiles.append(profile)
        profiles_by_type.setdefault((task["kind"], type_key), []).append(profile)

    type_fits = []
    type_fit_by_key: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for (kind, type_key), profiles in sorted(profiles_by_type.items()):
        reference_targets = np.asarray(profiles[0]["targets"], dtype=float)
        for profile in profiles[1:]:
            targets = np.asarray(profile["targets"], dtype=float)
            if len(targets) != len(reference_targets) or not np.allclose(targets, reference_targets):
                raise WorkflowError(
                    f"Cannot pool {kind} {'-'.join(type_key)}: representative scan grids differ."
                )
        relative_matrix = np.asarray(
            [profile["relative_energy_kcal_mol"] for profile in profiles], dtype=float
        )
        pooled_relative = relative_matrix.mean(axis=0)
        between_profile_spread = relative_matrix.std(axis=0)
        candidate: dict[str, Any] = {
            "kind": kind, "canonical_amber_type_key": list(type_key),
            "representative_term_ids": [profile["term_id"] for profile in profiles],
            "profile_count": len(profiles),
            "pooling_method": "mean of per-profile relative HF/6-31G* energies at matching targets",
            "points": [
                {
                    "target": float(target),
                    "pooled_relative_energy_kcal_mol": float(energy),
                    "between_profile_std_kcal_mol": float(spread),
                }
                for target, energy, spread in zip(
                    reference_targets, pooled_relative, between_profile_spread
                )
            ],
            "excluded_failed_terminal_points": [
                excluded
                for profile in profiles
                for excluded in profile["excluded_failed_terminal_points"]
            ],
            "source": "HF/6-31G* representative-arm relaxed constrained raw-QM profiles",
            "reviewed": False,
            "scientific_limitation": (
                "Raw QM was not corrected by MM-with-target-term-removed; type pooling and "
                "three-arm transfer are provisional and require qualified review."
            ),
        }
        candidate.update(_fit_provisional_raw_qm_profile(np, kind, reference_targets, pooled_relative))
        type_fits.append(candidate)
        type_fit_by_key[(kind, type_key)] = candidate

    fits = []
    directly_scanned = set(by_term)
    for task in plan["tasks"]:
        key = (task["kind"], task_type_keys[task["term_id"]])
        type_fit = type_fit_by_key.get(key)
        if type_fit is None:
            raise WorkflowError(
                f"No representative type-level fit is available for planned term {task['term_id']}."
            )
        candidate = {
            **type_fit,
            "term_id": task["term_id"], "atoms": task["atoms"],
            "amber_types": task_amber_types[task["term_id"]],
            "representative_crosslink": task["representative_crosslink"],
            "direct_qm_scan": task["term_id"] in directly_scanned,
            "transferred_from_representative_terms": type_fit["representative_term_ids"],
            "transfer_basis": (
                "identical canonical hybrid AMBER type key and assumed local equivalence of the "
                "three TBMB-Met crosslink arms"
            ),
            "transfer_requires_review": True,
        }
        fits.append(candidate)

    output = {
        "schema_version": 3, "created_at_utc": _now(), "passed": bool(fits),
        "candidate_only": True, "fits": fits, "type_fits": type_fits,
        "representative_raw_profiles": representative_profiles,
        "direct_qm_profile_count": len(representative_profiles),
        "type_fit_group_count": len(type_fits),
        "planned_term_candidate_count": len(fits),
        "excluded_failed_terminal_points": collection.get("excluded_failed_terminal_points", []),
        "all_jobs_converged": collection.get("all_jobs_converged") is True,
        "transfer_assumption_reviewed": False,
        "required_next_step": (
            "Use a baseline MM model with each target type term removed, fit QM-minus-MM, "
            "review the three-arm transfer assumption, then obtain qualified sign-off."
        ),
    }
    _write_json(paths["scan_fit"], output)
    review = _read_json(paths["scan_review"]) if paths["scan_review"].is_file() else {"terms": []}
    fit_by_id = {item["term_id"]: item for item in fits}
    for item in review.get("terms", []):
        if item.get("reviewed") is not True:
            if item.get("term_id") in fit_by_id:
                item["provisional_raw_qm_fit"] = fit_by_id[item["term_id"]]
            else:
                item.pop("provisional_raw_qm_fit", None)
    review["instructions"] = (
        "Type-consistent raw-QM fits and three-arm transfers are evidence only. Replace with "
        "reviewed QM-minus-MM values, set source/reviewed/reviewed_by, document transfer review, "
        "and retain reviewer notes."
    )
    review["provisional_fit_summary"] = {
        "direct_qm_profiles": len(representative_profiles),
        "canonical_amber_type_groups": len(type_fits),
        "planned_terms_with_candidates": len(fits),
        "three_arm_transfer_reviewed": False,
    }
    _write_json(paths["scan_review"], review)
    return {
        "bonded_fit_candidates": str(paths["scan_fit"]),
        "direct_qm_profiles": len(representative_profiles),
        "canonical_amber_type_groups": len(type_fits),
        "planned_term_candidates": len(fits),
        "review_template": str(paths["scan_review"]), "approved": False,
    }


def collect_bonded_scans(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    plan = _require_pass(paths["scan_plan"], "Bonded scan planning")
    review_path = _path(args.review) if args.review else paths["scan_review"]
    review = _read_json(review_path)
    records = review.get("terms", []) if isinstance(review, dict) else []
    by_id = {item.get("term_id"): item for item in records if isinstance(item, dict)}
    failures = []

    def finite_number(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    for task in plan["tasks"]:
        item = by_id.get(task["term_id"])
        if not item:
            failures.append(f"missing reviewed result for {task['term_id']}"); continue
        if item.get("reviewed") is not True or not str(item.get("reviewed_by", "")).strip():
            failures.append(f"{task['term_id']} is not signed off by a reviewer")
        if not str(item.get("source", "")).strip():
            failures.append(f"{task['term_id']} lacks its QM/analogue source")
        if task["kind"] == "bond":
            if (not finite_number(item.get("equilibrium_angstrom"))
                    or not finite_number(item.get("force_constant_kcal_mol_a2"))
                    or float(item.get("force_constant_kcal_mol_a2") or 0.0) <= 0.0):
                failures.append(f"{task['term_id']} lacks finite positive bond fit values")
        elif task["kind"] == "angle":
            if (not finite_number(item.get("equilibrium_degree"))
                    or not finite_number(item.get("force_constant_kcal_mol_rad2"))
                    or float(item.get("force_constant_kcal_mol_rad2") or 0.0) <= 0.0):
                failures.append(f"{task['term_id']} lacks finite positive angle fit values")
        else:
            fourier_terms = item.get("fourier_terms")
            if not isinstance(fourier_terms, list) or not fourier_terms:
                failures.append(f"{task['term_id']} lacks non-empty dihedral Fourier terms")
            else:
                for index, term in enumerate(fourier_terms, 1):
                    valid = (
                        isinstance(term, dict)
                        and finite_number(term.get("amplitude_kcal_mol"))
                        and float(term.get("amplitude_kcal_mol") or -1.0) >= 0.0
                        and finite_number(term.get("phase_degree"))
                        and finite_number(term.get("periodicity"))
                        and int(float(term.get("periodicity"))) > 0
                        and finite_number(term.get("idivf", 1))
                        and int(float(term.get("idivf", 1))) > 0
                    )
                    if not valid:
                        failures.append(
                            f"{task['term_id']} has invalid dihedral Fourier term {index}"
                        )
    extra = sorted(set(by_id) - {task["term_id"] for task in plan["tasks"]})
    if extra:
        failures.append("review contains unknown term IDs: " + ", ".join(extra))
    report = {
        "schema_version": 1, "created_at_utc": _now(), "passed": not failures,
        "review_file": str(review_path), "review_sha256": _sha256(review_path),
        "planned_terms": len(plan["tasks"]), "reviewed_terms": len(by_id),
        "bonded_parameters_reviewed": not failures, "failures": failures,
    }
    _write_json(paths["scan_audit"], report)
    if failures:
        raise WorkflowError(f"Bonded scan review is incomplete; see {paths['scan_audit']}")
    return {"bonded_scan_audit": str(paths["scan_audit"]), "reviewed_terms": len(by_id)}


def apply_reviewed_bonded(args: argparse.Namespace) -> dict[str, Any]:
    """Translate signed stable-ID bonded terms into a separate AMBER override FRCMOD."""
    paths = _layout(_path(args.root))
    audit = _require_pass(paths["scan_audit"], "Bonded scan review")
    review = _read_json(_path(audit["review_file"]))
    type_report = _read_json(paths["amber_atom_types"])
    stable_to_type = {
        str(item["stable_id"]): str(item["hybrid_type"])
        for item in type_report.get("model_atom_types", [])
    }
    sections: dict[str, dict[tuple[str, ...], Any]] = {"BOND": {}, "ANGLE": {}, "DIHE": {}}
    provenance = []
    for item in review.get("terms", []):
        stable_ids = [str(value) for value in item.get("atoms", [])]
        missing = [value for value in stable_ids if value not in stable_to_type]
        if missing:
            raise WorkflowError(f"Reviewed term {item.get('term_id')} lacks atom types for {missing}")
        types = [stable_to_type[value] for value in stable_ids]
        key = _canonical_type_key(types)
        kind = str(item.get("kind", "")).lower()
        if kind == "bond":
            value = (float(item["force_constant_kcal_mol_a2"]), float(item["equilibrium_angstrom"]))
            section = "BOND"
        elif kind == "angle":
            value = (float(item["force_constant_kcal_mol_rad2"]), float(item["equilibrium_degree"]))
            section = "ANGLE"
        elif kind == "dihedral":
            terms = item.get("fourier_terms")
            if not isinstance(terms, list) or not terms:
                raise WorkflowError(f"Reviewed dihedral {item.get('term_id')} has no Fourier terms.")
            value = tuple((int(term.get("idivf", 1)), float(term["amplitude_kcal_mol"]),
                           float(term["phase_degree"]), int(term["periodicity"])) for term in terms)
            section = "DIHE"
        else:
            raise WorkflowError(f"Unsupported reviewed term kind: {kind}")
        if key in sections[section] and sections[section][key] != value:
            raise WorkflowError(
                f"Equivalent AMBER type term {'-'.join(key)} has inconsistent reviewed values; "
                "resolve arm equivalence explicitly before writing FRCMOD."
            )
        sections[section][key] = value
        provenance.append({"term_id": item.get("term_id"), "stable_ids": stable_ids,
                           "amber_types": types, "source": item.get("source"),
                           "reviewed_by": item.get("reviewed_by")})
    lines = ["TBMB-Met reviewed bonded overrides; values require qualified scientific sign-off", "MASS", ""]
    lines.append("BOND")
    for key, (force_constant, equilibrium) in sorted(sections["BOND"].items()):
        lines.append(f"{'-'.join(key):<14s} {force_constant:10.4f} {equilibrium:10.5f}")
    lines.extend(["", "ANGLE"])
    for key, (force_constant, equilibrium) in sorted(sections["ANGLE"].items()):
        lines.append(f"{'-'.join(key):<18s} {force_constant:10.4f} {equilibrium:10.4f}")
    lines.extend(["", "DIHE"])
    for key, terms in sorted(sections["DIHE"].items()):
        for index, (idivf, amplitude, phase, periodicity) in enumerate(terms):
            signed_periodicity = periodicity if index == len(terms) - 1 else -abs(periodicity)
            lines.append(
                f"{'-'.join(key):<23s} {idivf:3d} {amplitude:10.5f} {phase:10.4f} {signed_periodicity:6.1f}"
            )
    lines.extend(["", "IMPROPER", "", "NONBON", ""])
    paths["amber_reviewed_frcmod"].write_text("\n".join(lines), encoding="utf-8")
    report = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "review_file": audit["review_file"], "review_sha256": audit["review_sha256"],
        "frcmod": str(paths["amber_reviewed_frcmod"]),
        "frcmod_sha256": _sha256(paths["amber_reviewed_frcmod"]),
        "unique_terms": {key.lower(): len(value) for key, value in sections.items()},
        "provenance": provenance,
        "warning": "This override does not cure unrelated parmchk2 ATTN lines in the base candidate FRCMOD.",
    }
    _write_json(paths["amber_reviewed_audit"], report)
    return {"reviewed_frcmod": str(paths["amber_reviewed_frcmod"]),
            "reviewed_frcmod_audit": str(paths["amber_reviewed_audit"]),
            "unique_terms": report["unique_terms"]}


def _run_logged(command: list[str], log: Path, cwd: Path | None = None) -> None:
    """Run one external AmberTools command and retain the complete provenance log."""
    executable = command[0]
    if not Path(executable).is_file() and shutil.which(executable) is None:
        raise WorkflowError(f"Required executable is not available: {executable}")
    _mkdir(log.parent)
    result = subprocess.run(
        command, cwd=str(cwd) if cwd else None, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    log.write_text(
        "$ " + " ".join(command) + "\n\n" + (result.stdout or ""),
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise WorkflowError(
            f"External command failed with exit code {result.returncode}; see {log}"
        )


def _mol2_type_for_rdkit_atom(atom: Any) -> str:
    symbol = atom.GetSymbol()
    if atom.GetIsAromatic():
        return {"C": "C.ar", "N": "N.ar"}.get(symbol, symbol)
    hybrid = str(atom.GetHybridization())
    suffix = "2" if "SP2" in hybrid else "1" if hybrid.endswith("SP") else "3"
    if symbol in {"C", "N", "O", "S", "P"}:
        return f"{symbol}.{suffix}"
    return symbol


def _mol2_bond_type(bond: Any) -> str:
    if bond.GetIsAromatic():
        return "ar"
    order = float(bond.GetBondTypeAsDouble())
    return "1" if abs(order - 1.0) < 0.1 else "2" if abs(order - 2.0) < 0.1 else "3"


def _write_mol2(path: Path, title: str, atoms: list[dict[str, Any]],
                bonds: list[dict[str, Any]], residue_name: str) -> Path:
    _mkdir(path.parent)
    lines = [
        "@<TRIPOS>MOLECULE", title, f"{len(atoms)} {len(bonds)} 1 0 0",
        "SMALL", "USER_CHARGES", "", "@<TRIPOS>ATOM",
    ]
    for index, atom in enumerate(atoms, 1):
        x, y, z = atom["xyz"]
        lines.append(
            f"{index:7d} {str(atom['name'])[:4]:<4s} {x: .8f} {y: .8f} {z: .8f} "
            f"{atom['type']!s:<8s} 1 {residue_name:<4s} {float(atom['charge']): .8f}"
        )
    lines.append("@<TRIPOS>BOND")
    for index, bond in enumerate(bonds, 1):
        lines.append(
            f"{index:6d} {int(bond['begin']) + 1:5d} {int(bond['end']) + 1:5d} {bond['type']}"
        )
    lines.extend(["@<TRIPOS>SUBSTRUCTURE", f"     1 {residue_name:<4s}        1 TEMP              0 ****  ****    0 ROOT", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _parse_mol2(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    section = ""
    atoms: list[dict[str, Any]] = []
    bonds: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("@<TRIPOS>"):
            section = line[9:]
            continue
        if not line.strip():
            continue
        fields = line.split()
        if section == "ATOM":
            if len(fields) < 9:
                raise WorkflowError(f"Malformed MOL2 atom line in {path}: {line}")
            atoms.append({
                "id": int(fields[0]), "name": fields[1],
                "xyz": [float(fields[2]), float(fields[3]), float(fields[4])],
                "type": fields[5], "subst_id": int(fields[6]),
                "subst_name": fields[7], "charge": float(fields[8]),
            })
        elif section == "BOND":
            if len(fields) < 4:
                raise WorkflowError(f"Malformed MOL2 bond line in {path}: {line}")
            bonds.append({
                "id": int(fields[0]), "begin_id": int(fields[1]),
                "end_id": int(fields[2]), "type": fields[3],
            })
    if not atoms:
        raise WorkflowError(f"No atoms found in MOL2: {path}")
    return {"atoms": atoms, "bonds": bonds}


def _mol2_element(record: dict[str, Any]) -> str:
    """Infer an element from an AMBER/SYBYL MOL2 atom type.

    ``tleap saveMol2 ... 1`` writes AMBER atom types, not element symbols.
    In particular, protein hydrogens can be typed ``HC``, ``HA``, ``H1``
    and so on.  Treating the first two letters as an element turns ``HC``
    into the fictitious element ``Hc`` and causes ff14SB hydrogens to be
    classified as heavy atoms.  AMBER carbon/nitrogen types such as ``CA``
    and ``NA`` likewise must not be interpreted as calcium or sodium.
    """
    value = re.sub(r"[^A-Za-z]", "", str(record["type"]).split(".", 1)[0]).upper()
    if not value:
        raise WorkflowError(f"Cannot infer an element from MOL2 atom type {record.get('type')!r}.")
    if value.startswith("CL"):
        return "Cl"
    if value.startswith("BR"):
        return "Br"
    # AMBER hydrogen types include H, HA, HC, HO, HP, HS, HW, H1-H5.
    # All remaining atom types used here are identified by their first
    # element letter (C/N/O/S/P/F/I); e.g. CA and NA are force-field types.
    return value[0]


def _residue_base(value: str) -> str:
    return re.sub(r"[0-9]+$", "", value).upper()


def _first_audited_sdf(paths: dict[str, Path]) -> Path:
    audit = _require_pass(paths["audit"], "Input audit")
    path = _path(audit["authoritative_inputs"]["ensemble_sdf"]["path"])
    if _sha256(path) != audit["authoritative_inputs"]["ensemble_sdf"]["sha256"]:
        raise WorkflowError("The audited ensemble SDF changed after audit-inputs.")
    return path


def _reference_with_stable_hydrogens(paths: dict[str, Path]):
    Chem, _ = _import_rdkit()
    reference = _load_ensemble(_first_audited_sdf(paths))[0]
    atom_map = _read_json(paths["atom_map"])["atoms"]
    _label_molecule(reference, atom_map)
    molecule = Chem.AddHs(reference, addCoords=True)
    counters: dict[str, int] = {}
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() != 1:
            continue
        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 1 or not neighbors[0].HasProp("stable_id"):
            raise WorkflowError("Cannot assign a stable identifier to a full-ligand hydrogen.")
        parent = neighbors[0].GetProp("stable_id")
        counters[parent] = counters.get(parent, 0) + 1
        atom.SetProp("stable_id", f"{parent}:H{counters[parent]}")
    return molecule


def _prepare_local_resp_mol2(paths: dict[str, Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    Chem, _ = _import_rdkit()
    selection = _require_pass(paths["selection"], "QM model construction")
    charge_data = _require_pass(paths["charges"], "RESP fit")
    model = Chem.SDMolSupplier(selection["selected_conformers"][0]["sdf"], removeHs=False)[0]
    if model is None:
        raise WorkflowError("Cannot read the selected local QM model.")
    atom_map = selection["model_atom_map"]
    charges = {int(item["model_atom_index"]): float(item["resp_charge"])
               for item in charge_data["atoms"]}
    if model.GetNumAtoms() != len(atom_map) or set(charges) != set(range(model.GetNumAtoms())):
        raise WorkflowError("Local-model atom order no longer matches RESP output.")
    conformer = model.GetConformer()
    atoms = []
    for atom, mapping in zip(model.GetAtoms(), atom_map):
        point = conformer.GetAtomPosition(atom.GetIdx())
        atoms.append({
            "name": f"{atom.GetSymbol().upper()}{atom.GetIdx() + 1}"[:4],
            "type": _mol2_type_for_rdkit_atom(atom), "charge": charges[atom.GetIdx()],
            "xyz": [float(point.x), float(point.y), float(point.z)],
            "stable_id": str(mapping["stable_id"]),
        })
    bonds = [{"begin": bond.GetBeginAtomIdx(), "end": bond.GetEndAtomIdx(),
              "type": _mol2_bond_type(bond)} for bond in model.GetBonds()]
    _write_mol2(paths["amber_local_input"], "TBMB_MET_LOCAL_RESP", atoms, bonds, "LOC")
    charge_file = paths["amber"] / "resp_charges.dat"
    charge_file.write_text("\n".join(f"{atom['charge']:.10f}" for atom in atoms) + "\n", encoding="utf-8")
    return atoms, bonds


def _standard_residue_records(mol2: dict[str, Any], residue: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    selected = [item for item in mol2["atoms"] if _residue_base(item["subst_name"]) == residue]
    if not selected:
        raise WorkflowError(f"ff14SB reference MOL2 contains no {residue} residue.")
    by_id = {item["id"]: item for item in selected}
    heavy_records = [item for item in selected if _mol2_element(item).upper() != "H"]
    heavy = {item["name"].upper(): item for item in heavy_records}
    if len(heavy) != len(heavy_records):
        raise WorkflowError(f"ff14SB {residue} reference contains duplicate heavy-atom names.")
    hydrogens: dict[str, list[dict[str, Any]]] = {name: [] for name in heavy}
    all_atoms = {item["id"]: item for item in mol2["atoms"]}
    assigned_hydrogens: set[int] = set()
    for bond in mol2["bonds"]:
        first, second = all_atoms.get(bond["begin_id"]), all_atoms.get(bond["end_id"])
        if not first or not second or first["id"] not in by_id or second["id"] not in by_id:
            continue
        if _mol2_element(first).upper() == "H" and _mol2_element(second).upper() != "H":
            hydrogens.setdefault(second["name"].upper(), []).append(first)
            assigned_hydrogens.add(first["id"])
        elif _mol2_element(second).upper() == "H" and _mol2_element(first).upper() != "H":
            hydrogens.setdefault(first["name"].upper(), []).append(second)
            assigned_hydrogens.add(second["id"])
    for values in hydrogens.values():
        values.sort(key=lambda item: item["name"])
    expected_hydrogens = {
        item["id"] for item in selected if _mol2_element(item).upper() == "H"
    }
    if assigned_hydrogens != expected_hydrogens:
        missing = [
            item["name"] for item in selected
            if item["id"] in expected_hydrogens - assigned_hydrogens
        ]
        raise WorkflowError(
            f"ff14SB {residue} reference contains hydrogens without an intra-residue "
            f"heavy-atom parent: {missing}"
        )
    return heavy, hydrogens


def _validate_met_template_boundary(molecule: Any, position: int,
                                    met_h: dict[str, list[dict[str, Any]]]) -> None:
    """Verify the ff14SB/RESP boundary before assigning template parameters."""
    prefix = f"PEP:{position}:"
    by_sid = {
        atom.GetProp("stable_id"): atom
        for atom in molecule.GetAtoms()
        if atom.HasProp("stable_id") and atom.GetProp("stable_id").startswith(prefix)
    }
    required = [f"{prefix}{name}" for name in ("N", "CA", "C", "O", "CB", "CG")]
    missing = [stable_id for stable_id in required if stable_id not in by_sid]
    if missing:
        raise WorkflowError(
            f"MET {position} template boundary is missing required heavy atoms: {missing}"
        )
    cb = by_sid[f"{prefix}CB"]
    cb_heavy_neighbors = {
        neighbor.GetProp("stable_id")
        for neighbor in cb.GetNeighbors()
        if neighbor.GetAtomicNum() != 1 and neighbor.HasProp("stable_id")
    }
    expected_cb_neighbors = {f"{prefix}CA", f"{prefix}CG"}
    if cb_heavy_neighbors != expected_cb_neighbors:
        raise WorkflowError(
            f"MET {position} CB boundary topology is not CA-CB-CG: "
            f"observed heavy neighbors={sorted(cb_heavy_neighbors)}, "
            f"expected={sorted(expected_cb_neighbors)}."
        )
    observed_counts: dict[str, int] = {}
    for stable_id, atom in by_sid.items():
        if atom.GetAtomicNum() != 1:
            continue
        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 1 or not neighbors[0].HasProp("stable_id"):
            raise WorkflowError(f"MET {position} hydrogen {stable_id} has no unique stable parent.")
        parent = neighbors[0].GetProp("stable_id")
        if not parent.startswith(prefix):
            raise WorkflowError(f"MET {position} hydrogen {stable_id} belongs to {parent}.")
        parent_name = parent[len(prefix):].upper()
        observed_counts[parent_name] = observed_counts.get(parent_name, 0) + 1
    for parent_name in ("N", "CA", "C", "O", "CB"):
        observed = observed_counts.get(parent_name, 0)
        candidates = met_h.get(parent_name, [])
        expected = len(candidates)
        if observed != expected:
            candidate_names = [str(item["name"]) for item in candidates]
            raise WorkflowError(
                f"Hydrogen count mismatch for ff14SB MET {position} {parent_name}: "
                f"RDKit observed={observed}; ff14SB expected={expected}; "
                f"ff14SB hydrogen names={candidate_names}."
            )


def _normalized_local_id(stable_id: str) -> str:
    return re.sub(r"^PEP:[0-9]+:", "PEP:*:", stable_id)


def _local_parameter_sources(selection: dict[str, Any], typed: dict[str, Any],
                             charges: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if len(typed["atoms"]) != len(selection["model_atom_map"]):
        raise WorkflowError("Antechamber changed the local-model atom count/order.")
    charge_rows = {int(item["model_atom_index"]): item for item in charges["atoms"]}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, (mapping, typed_atom) in enumerate(zip(selection["model_atom_map"], typed["atoms"])):
        key = _normalized_local_id(str(mapping["stable_id"]))
        grouped.setdefault(key, []).append({
            "type": typed_atom["type"], "charge": float(charge_rows[index]["resp_charge"]),
            "element": str(mapping["element"]),
        })
    output: dict[str, dict[str, Any]] = {}
    for key, values in grouped.items():
        types = {item["type"] for item in values}
        elements = {item["element"] for item in values}
        if len(types) != 1 or len(elements) != 1:
            raise WorkflowError(f"Chemically equivalent local atoms disagree in GAFF2 type: {key}")
        output[key] = {
            "type": next(iter(types)), "element": next(iter(elements)),
            "charge": sum(item["charge"] for item in values) / len(values),
            "equivalent_atom_count": len(values),
        }
    return output


def _template_bonds(molecule: Any, indices: list[int]) -> list[dict[str, Any]]:
    local = {atom_index: local_index for local_index, atom_index in enumerate(indices)}
    output = []
    for bond in molecule.GetBonds():
        first, second = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if first in local and second in local:
            output.append({"begin": local[first], "end": local[second], "type": _mol2_bond_type(bond)})
    return output


def _template_for_met(molecule: Any, position: int, residue_name: str,
                      local_sources: dict[str, dict[str, Any]],
                      met_heavy: dict[str, dict[str, Any]],
                      met_h: dict[str, list[dict[str, Any]]],
                      nhe_heavy: dict[str, dict[str, Any]],
                      nhe_h: dict[str, list[dict[str, Any]]], include_c_cap: bool) -> dict[str, Any]:
    _validate_met_template_boundary(molecule, position, met_h)
    conformer = molecule.GetConformer()
    selected = []
    prefix = f"PEP:{position}:"
    for atom in molecule.GetAtoms():
        sid = atom.GetProp("stable_id") if atom.HasProp("stable_id") else ""
        if sid.startswith(prefix) or (include_c_cap and sid.startswith("CTN:")):
            selected.append(atom.GetIdx())
    records: list[dict[str, Any]] = []
    standard_names = {"N", "CA", "C", "O", "CB"}
    hydrogen_slots: dict[str, int] = {}
    for atom_index in selected:
        atom = molecule.GetAtomWithIdx(atom_index)
        sid = atom.GetProp("stable_id")
        point = conformer.GetAtomPosition(atom_index)
        if sid.startswith("CTN:"):
            if atom.GetAtomicNum() == 1:
                slot = hydrogen_slots.get("CTN", 0); hydrogen_slots["CTN"] = slot + 1
                if len(nhe_heavy) != 1:
                    raise WorkflowError(
                        f"Expected exactly one heavy atom in ff14SB NHE; observed {sorted(nhe_heavy)}."
                    )
                nhe_parent = next(iter(nhe_heavy))
                candidates = nhe_h.get(nhe_parent, [])
                if slot >= len(candidates):
                    raise WorkflowError(
                        "C-terminal amide hydrogen count differs from ff14SB NHE: "
                        f"RDKit observed at least {slot + 1}; ff14SB expected={len(candidates)}; "
                        f"ff14SB hydrogen names={[item['name'] for item in candidates]}."
                    )
                source = candidates[slot]; name = f"HN{slot + 1}"
                parameter_source = "ff14SB:NHE:hydrogen"
            else:
                candidates = list(nhe_heavy.values())
                if len(candidates) != 1:
                    raise WorkflowError("Expected exactly one heavy atom in ff14SB NHE.")
                source = candidates[0]; name = "NT"
                parameter_source = "ff14SB:NHE:heavy"
        else:
            tail = sid[len(prefix):]
            parent_name = tail.split(":H", 1)[0].upper()
            if atom.GetAtomicNum() == 1:
                slot = hydrogen_slots.get(parent_name, 0); hydrogen_slots[parent_name] = slot + 1
                if parent_name in standard_names:
                    candidates = met_h.get(parent_name, [])
                    if slot >= len(candidates):
                        raise WorkflowError(
                            f"Hydrogen count mismatch for ff14SB MET {position} {parent_name}: "
                            f"RDKit observed at least {slot + 1}; ff14SB expected={len(candidates)}; "
                            f"ff14SB hydrogen names={[item['name'] for item in candidates]}."
                        )
                    source = candidates[slot]; name = source["name"]
                    parameter_source = "ff14SB:MET:hydrogen"
                else:
                    key = _normalized_local_id(sid)
                    if key not in local_sources:
                        raise WorkflowError(f"RESP/GAFF2 source missing for {sid}")
                    source = local_sources[key]
                    conventional = met_h.get(parent_name, [])
                    name = conventional[slot]["name"] if slot < len(conventional) else f"H{parent_name[:2]}{slot + 1}"[:4]
                    parameter_source = "RESP:charge+GAFF2:type"
            else:
                name = parent_name
                if parent_name in standard_names:
                    if parent_name not in met_heavy:
                        raise WorkflowError(f"ff14SB MET source lacks {parent_name}")
                    source = met_heavy[parent_name]
                    parameter_source = "ff14SB:MET:heavy"
                else:
                    key = _normalized_local_id(sid)
                    if key not in local_sources:
                        raise WorkflowError(f"RESP/GAFF2 source missing for {sid}")
                    source = local_sources[key]
                    parameter_source = "RESP:charge+GAFF2:type"
        records.append({
            "name": name, "type": source["type"], "charge": float(source["charge"]),
            "xyz": [float(point.x), float(point.y), float(point.z)],
            "stable_id": sid, "source_atom_index": atom_index,
            "parameter_source": parameter_source,
        })
    names = [item["name"] for item in records]
    if len(names) != len(set(names)):
        raise WorkflowError(f"Duplicate atom names in {residue_name}: {names}")
    if include_c_cap:
        if len(nhe_heavy) != 1:
            raise WorkflowError(
                f"Expected exactly one heavy atom in ff14SB NHE; observed {sorted(nhe_heavy)}."
            )
        nhe_parent = next(iter(nhe_heavy))
        expected_nhe_h = len(nhe_h.get(nhe_parent, []))
        observed_nhe_h = hydrogen_slots.get("CTN", 0)
        if observed_nhe_h != expected_nhe_h:
            raise WorkflowError(
                "C-terminal amide hydrogen count differs from ff14SB NHE: "
                f"RDKit observed={observed_nhe_h}; ff14SB expected={expected_nhe_h}; "
                f"ff14SB hydrogen names="
                f"{[item['name'] for item in nhe_h.get(nhe_parent, [])]}."
            )
    return {"name": residue_name, "atoms": records,
            "bonds": _template_bonds(molecule, selected), "source_indices": selected}


def _template_for_tmb(molecule: Any, local_sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    conformer = molecule.GetConformer()
    selected = [atom.GetIdx() for atom in molecule.GetAtoms()
                if atom.HasProp("stable_id") and atom.GetProp("stable_id").startswith("TBM:")]
    records = []
    hydrogen_number = 0
    for atom_index in selected:
        atom = molecule.GetAtomWithIdx(atom_index)
        sid = atom.GetProp("stable_id")
        key = _normalized_local_id(sid)
        if key not in local_sources:
            raise WorkflowError(f"RESP/GAFF2 source missing for {sid}")
        point = conformer.GetAtomPosition(atom_index)
        if atom.GetAtomicNum() == 1:
            hydrogen_number += 1; name = f"H{hydrogen_number}"
        else:
            name = sid.split(":", 1)[1].split(":H", 1)[0].upper()
        records.append({
            "name": name[:4], "type": local_sources[key]["type"],
            "charge": float(local_sources[key]["charge"]),
            "xyz": [float(point.x), float(point.y), float(point.z)],
            "stable_id": sid, "source_atom_index": atom_index,
            "parameter_source": "RESP:charge+GAFF2:type",
        })
    names = [item["name"] for item in records]
    if len(names) != len(set(names)):
        raise WorkflowError(f"Duplicate TMB atom names: {names}")
    return {"name": "TMB", "atoms": records,
            "bonds": _template_bonds(molecule, selected), "source_indices": selected}


_FRCMOD_SECTIONS = ("MASS", "BOND", "ANGLE", "DIHE", "IMPROPER", "NONBON")


def _frcmod_entry_types(line: str) -> tuple[str, ...]:
    """Read the atom-type expression before the first numeric FRCMOD field."""
    pieces: list[str] = []
    for token in line.split():
        try:
            float(token)
        except ValueError:
            pieces.append(token)
            continue
        break
    expression = "".join(pieces).strip("-")
    return tuple(value for value in expression.split("-") if value)


def _filter_candidate_frcmod(source: Path, output: Path, standard_types: set[str],
                             required_boundary_type: str | None = None) -> dict[str, Any]:
    """Prevent parmchk2 analogues from overriding authoritative ff14SB terms.

    The raw parmchk2 files are retained as provenance.  The runtime candidate
    FRCMOD keeps only entries containing a non-ff14SB type.  For the separate
    peptide-boundary pass it additionally keeps only terms involving the
    ff14SB alpha-carbon type (CX), which are absent from the capped local RESP
    model but required by the complete MSU residue.
    """
    sections: dict[str, list[str]] = {name: [] for name in _FRCMOD_SECTIONS}
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    section = ""
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.upper() in _FRCMOD_SECTIONS:
            section = stripped.upper()
            continue
        if not section or not stripped:
            continue
        atom_types = _frcmod_entry_types(line)
        if not atom_types:
            excluded.append({"section": section, "line": stripped, "reason": "unparseable"})
            continue
        has_nonstandard = any(atom_type not in standard_types for atom_type in atom_types)
        has_boundary = required_boundary_type is None or required_boundary_type in atom_types
        record = {"section": section, "atom_types": list(atom_types), "line": stripped}
        if has_nonstandard and has_boundary:
            sections[section].append(line.rstrip())
            kept.append(record)
        else:
            record["reason"] = (
                "ff14SB-only term" if not has_nonstandard
                else f"does not contain boundary type {required_boundary_type}"
            )
            excluded.append(record)
    title = (
        "Filtered parmchk2 candidate terms; ff14SB-only entries excluded; "
        "qualified review required"
    )
    rendered = [title]
    for name in _FRCMOD_SECTIONS:
        rendered.extend([name, *sections[name], ""])
    output.write_text("\n".join(rendered), encoding="utf-8")
    return {
        "source": str(source), "source_sha256": _sha256(source),
        "output": str(output), "output_sha256": _sha256(output),
        "standard_types": sorted(standard_types),
        "required_boundary_type": required_boundary_type,
        "kept": kept, "excluded": excluded,
    }


def _write_reference_amber_pdb(paths: dict[str, Path], args: argparse.Namespace,
                               output: Path) -> dict[str, Any]:
    atom_map = _read_json(paths["atom_map"])["atoms"]
    sequence = str(args.sequence).upper()
    positions = tuple(int(value) for value in args.met_positions)
    groups: list[tuple[str, Any, list[dict[str, Any]]]] = []
    ace = [item for item in atom_map if str(item["stable_id"]).startswith("ACE:")]
    if ace:
        groups.append(("ACE", "ACE", ace))
    for position in range(1, len(sequence) + 1):
        records = [item for item in atom_map if str(item["stable_id"]).startswith(f"PEP:{position}:")]
        if not records:
            raise WorkflowError(f"Reference PDB lacks peptide residue {position}")
        residue_name = str(records[0]["residue_name"]).upper()
        if position in positions:
            residue_name = (
                N_TERMINAL_MET_TEMPLATE if position == 1
                else C_TERMINAL_MET_TEMPLATE if position == len(sequence)
                else INTERNAL_MET_TEMPLATE
            )
        if position == len(sequence):
            cap = [item for item in atom_map if str(item["stable_id"]).startswith("CTN:")]
            if cap:
                for item in cap:
                    item = dict(item); item["atom_name"] = "NT"; records.append(item)
        groups.append((residue_name, position, records))
    linker = [item for item in atom_map if str(item["stable_id"]).startswith("TBM:")]
    if not linker:
        raise WorkflowError("Reference atom map lacks TBMB atoms.")
    groups.append(("TMB", "TMB", linker))
    lines, residue_indices, serial = [], {}, 1
    for leap_index, (residue_name, key, records) in enumerate(groups, 1):
        if key == "TMB":
            lines.append("TER")
        residue_indices[str(key)] = leap_index
        for record in records:
            atom_name = str(record["atom_name"])[:4]
            element = str(record["element"])[:2]
            x, y, z = record["xyz"]
            tag = "HETATM" if key == "TMB" else "ATOM  "
            chain = "B" if key == "TMB" else "A"
            lines.append(
                f"{tag}{serial:5d} {atom_name:>4s}{residue_name:>4s} {chain}{leap_index:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}"
            )
            serial += 1
    lines.extend(["TER", "END"])
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "residue_indices": residue_indices,
        "crosslinks": [{
            "position": position, "peptide_residue_index": residue_indices[str(position)],
            "linker_residue_index": residue_indices["TMB"], "methionine_atom": "SD",
            "linker_atom": arm,
        } for position, arm in zip(positions, ("CM1", "CM2", "CM3"))],
    }


def _frcmod_problems_local(path: Path) -> list[str]:
    problems, section = [], ""
    for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        stripped, upper = line.strip(), line.upper()
        if stripped.upper() in {"MASS", "BOND", "ANGLE", "DIHE", "IMPROPER", "NONBON"}:
            section = stripped.upper(); continue
        if "ATTN" in upper or "NEED REVISION" in upper:
            problems.append(f"line {number}: {line.strip()}")
            continue
        if not stripped or section not in {"BOND", "ANGLE", "DIHE", "IMPROPER"}:
            continue
        numbers = [float(value) for value in re.findall(
            r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", stripped
        )]
        if not numbers:
            continue
        if section in {"BOND", "ANGLE", "IMPROPER"} and numbers[0] <= 0:
            problems.append(f"line {number}: non-positive {section.lower()} force constant: {stripped}")
        if section == "DIHE" and len(numbers) >= 2 and numbers[1] <= 0:
            problems.append(f"line {number}: non-positive dihedral barrier: {stripped}")
    return problems


def _prmtop_charge(path: Path) -> float:
    active, values = False, []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        raise WorkflowError(f"Cannot read AMBER topology {path}: {exc}") from exc
    for line in lines:
        if line.startswith("%FLAG"):
            active = line.strip() == "%FLAG CHARGE"; continue
        if active and not line.startswith("%FORMAT"):
            try:
                values.extend(float(value) for value in line.split())
            except ValueError as exc:
                raise WorkflowError(f"Malformed CHARGE section in {path}: {line}") from exc
    if not values:
        raise WorkflowError(f"No CHARGE section found in {path}")
    return sum(values) / 18.2223


def _prmtop_fields(path: Path, flag: str) -> list[str]:
    """Read a fixed-width AMBER topology field, preserving atom order."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    marker = f"%FLAG {flag}"
    try:
        index = next(i for i, line in enumerate(lines) if line.strip() == marker)
    except StopIteration as exc:
        raise WorkflowError(f"AMBER topology lacks {marker}: {path}") from exc
    if index + 1 >= len(lines):
        raise WorkflowError(f"AMBER topology has no format for {marker}: {path}")
    fmt = re.fullmatch(r"%FORMAT\((\d+)([a-zA-Z])(\d+)(?:\.\d+)?\)", lines[index + 1].strip())
    if fmt is None:
        raise WorkflowError(f"Unsupported AMBER topology format for {marker}: {lines[index + 1]}")
    width = int(fmt.group(3))
    fields = []
    for line in lines[index + 2:]:
        if line.startswith("%FLAG"):
            break
        fields.extend(line[offset:offset + width].strip()
                      for offset in range(0, len(line), width)
                      if line[offset:offset + width].strip())
    return fields


def _xyz_order(path: Path, expected_elements: list[str]) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        declared = int(lines[0].strip())
    except (IndexError, ValueError) as exc:
        raise WorkflowError(f"Invalid QM XYZ atom count: {path}") from exc
    if declared != len(expected_elements) or len(lines) < declared + 2:
        raise WorkflowError(f"QM XYZ atom count differs from the local model: {path}")
    for index, (line, expected) in enumerate(zip(lines[2:declared + 2], expected_elements)):
        fields = line.split()
        if len(fields) < 4 or fields[0].title() != expected.title():
            raise WorkflowError(f"QM XYZ atom order differs at atom {index + 1}: {path}")
        try:
            coordinates = [float(value) for value in fields[1:4]]
        except ValueError as exc:
            raise WorkflowError(f"Invalid QM XYZ coordinates at atom {index + 1}: {path}") from exc
        if not all(math.isfinite(value) for value in coordinates):
            raise WorkflowError(f"Non-finite QM XYZ coordinates at atom {index + 1}: {path}")


def build_mm_baseline(args: argparse.Namespace) -> dict[str, Any]:
    """Build an unapproved 54-atom AMBER model for later QM-minus-MM analysis."""
    paths = _layout(_path(args.root))
    selection = _require_pass(paths["selection"], "QM model construction")
    _require_pass(paths["amber_build_audit"], "AMBER candidate build")
    job_manifest = _require_pass(paths["scan_job_manifest"], "Bonded QM job preparation")
    scan_collection = _require_pass(paths["scan_collection"], "Bonded QM collection")
    if scan_collection.get("collection_complete_for_provisional_fitting") is not True:
        raise WorkflowError("Bonded QM collection has not accounted for every planned scan point.")
    mol2_path = paths["amber_hybrid_typed"]
    input_files = [mol2_path, paths["amber_atom_types"], paths["amber_frcmod"],
                   paths["amber_boundary_frcmod"]]
    for input_path in input_files:
        if not input_path.is_file() or input_path.stat().st_size == 0:
            raise WorkflowError(f"MM baseline input is missing or empty: {input_path}")
    mol2 = _parse_mol2(mol2_path)
    atom_map = selection["model_atom_map"]
    atoms = mol2["atoms"]
    expected_count = len(atom_map)
    if expected_count != 54 or len(atoms) != expected_count:
        raise WorkflowError(f"Expected 54 local-model atoms; map={expected_count}, MOL2={len(atoms)}")
    type_report = _read_json(paths["amber_atom_types"])
    typed_atoms = type_report.get("model_atom_types", [])
    if len(typed_atoms) != expected_count:
        raise WorkflowError("Hybrid AMBER atom-type map has a different atom count.")
    expected_elements = []
    expected_names = []
    expected_types = []
    for index, (mapping, atom, typed) in enumerate(zip(atom_map, atoms, typed_atoms)):
        stable_id = str(mapping["stable_id"])
        element = str(mapping["element"]).title()
        if (int(mapping["model_atom_index"]) != index or int(typed["model_atom_index"]) != index
                or int(atom["id"]) != index + 1 or stable_id != str(typed["stable_id"])
                or element != _mol2_element(atom).title()
                or str(atom["type"]) != str(typed["hybrid_type"])):
            raise WorkflowError(f"MOL2, stable atom map, and hybrid type map disagree at atom {index + 1} ({stable_id}).")
        expected_elements.append(element)
        expected_names.append(str(atom["name"]))
        expected_types.append(str(atom["type"]))
    if len(set(expected_names)) != expected_count:
        raise WorkflowError("MOL2 atom names are not unique; ordered topology verification is unsafe.")

    model_xyz = _path(selection["selected_conformers"][0]["xyz"])
    if _sha256(model_xyz) != selection["selected_conformers"][0]["xyz_sha256"]:
        raise WorkflowError("The first QM model XYZ changed after selection.")
    _xyz_order(model_xyz, expected_elements)
    xyz_lines = model_xyz.read_text(encoding="utf-8").splitlines()[2:expected_count + 2]
    max_input_coordinate_delta = max(
        abs(float(value) - reference)
        for atom, line in zip(atoms, xyz_lines)
        for value, reference in zip(line.split()[1:4], atom["xyz"])
    )
    if max_input_coordinate_delta > 0.01:
        raise WorkflowError(
            f"MOL2 and first QM XYZ coordinates differ by {max_input_coordinate_delta:.4f} A; "
            "verify their atom order and source conformer."
        )
    scanned_xyz = set()
    for job in job_manifest["jobs"]:
        xyz_path = _path(job["xyz"])
        if _sha256(xyz_path) != job["xyz_sha256"]:
            raise WorkflowError(f"QM scan input XYZ changed: {job['job_id']}")
        _xyz_order(xyz_path, expected_elements)
        scanned_xyz.add(str(xyz_path))
    optimized_xyz = set()
    for record in scan_collection["records"]:
        result_path = _path(record["result_json"])
        if _sha256(result_path) != record["result_sha256"]:
            raise WorkflowError(f"Collected QM result changed: {record['job_id']}")
        result = _read_json(result_path)
        final_path = _path(result.get("final_xyz", ""))
        if (result.get("success") is not True or not final_path.is_file()
                or _sha256(final_path) != result.get("final_xyz_sha256")):
            raise WorkflowError(f"Optimized QM XYZ is missing or changed: {record['job_id']}")
        _xyz_order(final_path, expected_elements)
        optimized_xyz.add(str(final_path))

    expected_charge = float(selection["model_definition"]["charge"])
    mol2_charge = sum(float(atom["charge"]) for atom in atoms)
    if abs(mol2_charge - expected_charge) > 1e-4:
        raise WorkflowError(
            f"Local-model MOL2 charge {mol2_charge:.8f} differs from {expected_charge:.0f}."
        )
    output_dir = _mkdir(paths["mm_baseline"])
    script = output_dir / "build_local_model.leap"
    log = output_dir / "tleap.log"
    prmtop = output_dir / "local_model.prmtop"
    inpcrd = output_dir / "local_model.inpcrd"
    pdb = output_dir / "local_model_amber.pdb"
    script.write_text("\n".join([
        "source leaprc.protein.ff14SB", "source leaprc.gaff2",
        f'loadAmberParams "{paths["amber_frcmod"]}"',
        f'loadAmberParams "{paths["amber_boundary_frcmod"]}"',
        f'local = loadMol2 "{mol2_path}"',
        "check local", f'saveAmberParm local "{prmtop}" "{inpcrd}"',
        f'savePdb local "{pdb}"', "quit", "",
    ]), encoding="utf-8")
    # Only outputs owned by this stage are replaced; stale files must not pass verification.
    for generated in (prmtop, inpcrd, pdb):
        if generated.exists():
            generated.unlink()
    command_failure = ""
    try:
        _run_logged([args.tleap, "-f", str(script)], log, output_dir)
    except WorkflowError as exc:
        command_failure = str(exc)
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    error_patterns = (
        r"\bFATAL\b", r"Could not find", r"does not have a type",
        r"No torsion terms", r"Unknown residue", r"Errors\s*=\s*[1-9]",
        r"\bError:\s", r"Error!",
    )
    tleap_errors = [line.strip() for line in log_text.splitlines()
                    if any(re.search(pattern, line, re.IGNORECASE) for pattern in error_patterns)]
    if not re.search(r"Exiting LEaP:\s*Errors\s*=\s*0\b", log_text):
        tleap_errors.append("TLEaP zero-error exit marker is absent.")

    validation_errors = []
    observed_charge = None
    prmtop_names: list[str] = []
    prmtop_types: list[str] = []
    prmtop_elements: list[str] = []
    openmm_atom_count = None
    max_coordinate_delta = None
    if not command_failure and not tleap_errors:
        for generated in (prmtop, inpcrd, pdb):
            if not generated.is_file() or generated.stat().st_size == 0:
                validation_errors.append(f"TLEaP did not produce a non-empty {generated.name}.")
        if not validation_errors:
            try:
                pointers = _prmtop_fields(prmtop, "POINTERS")
                natom = int(pointers[0])
                prmtop_names = _prmtop_fields(prmtop, "ATOM_NAME")[:natom]
                prmtop_types = _prmtop_fields(prmtop, "AMBER_ATOM_TYPE")[:natom]
                atomic_numbers = [int(value) for value in _prmtop_fields(prmtop, "ATOMIC_NUMBER")[:natom]]
                number_to_element = {1: "H", 6: "C", 7: "N", 8: "O", 9: "F",
                                     15: "P", 16: "S", 17: "Cl", 35: "Br", 53: "I"}
                prmtop_elements = [number_to_element.get(value, f"Z{value}")
                                    for value in atomic_numbers]
                if natom != expected_count or len(prmtop_names) != expected_count or len(prmtop_types) != expected_count:
                    validation_errors.append(f"PRMTOP atom count/order length differs from {expected_count}.")
                if prmtop_names != expected_names:
                    validation_errors.append("PRMTOP atom names/order differ from the local-model MOL2.")
                if prmtop_types != expected_types:
                    validation_errors.append("PRMTOP atom types/order differ from the hybrid MOL2.")
                if prmtop_elements != expected_elements:
                    validation_errors.append("PRMTOP elements/order differ from the QM atom map.")
                observed_charge = _prmtop_charge(prmtop)
                if abs(observed_charge - expected_charge) > 1e-4:
                    validation_errors.append(
                        f"PRMTOP charge {observed_charge:.8f} differs from {expected_charge:.0f}."
                    )
                from openmm import app, unit
                topology = app.AmberPrmtopFile(str(prmtop))
                coordinates = app.AmberInpcrdFile(str(inpcrd))
                openmm_atoms = list(topology.topology.atoms())
                openmm_positions = coordinates.positions.value_in_unit(unit.angstrom)
                openmm_atom_count = len(openmm_atoms)
                if openmm_atom_count != expected_count or len(openmm_positions) != expected_count:
                    validation_errors.append("OpenMM topology or coordinates do not contain 54 atoms.")
                else:
                    for index, (topology_atom, expected_name, expected_element) in enumerate(
                        zip(openmm_atoms, expected_names, expected_elements), 1
                    ):
                        if (topology_atom.name != expected_name or topology_atom.element is None
                                or topology_atom.element.symbol != expected_element):
                            validation_errors.append(f"OpenMM topology atom order differs at index {index}.")
                            break
                    max_coordinate_delta = max(
                        abs(float(position[axis]) - float(atom["xyz"][axis]))
                        for position, atom in zip(openmm_positions, atoms)
                        for axis in range(3)
                    )
                    if max_coordinate_delta > 0.01:
                        validation_errors.append(
                            f"OpenMM coordinates differ from ordered MOL2 by {max_coordinate_delta:.4f} A."
                        )
            except (OSError, ValueError, IndexError, ImportError, RuntimeError) as exc:
                validation_errors.append(str(exc))
    passed = not command_failure and not tleap_errors and not validation_errors
    report = {
        "schema_version": 1, "created_at_utc": _now(), "passed": passed,
        "candidate_only": True, "approved_for_production": False,
        "atom_count_expected": expected_count, "mol2_atom_count": len(atoms),
        "openmm_atom_count": openmm_atom_count,
        "atom_order_verified": passed,
        "scanned_xyz_order_verified": len(scanned_xyz),
        "optimized_xyz_order_verified": len(optimized_xyz),
        "max_mol2_qm_input_coordinate_delta_angstrom": max_input_coordinate_delta,
        "max_mol2_openmm_coordinate_delta_angstrom": max_coordinate_delta,
        "mol2_charge": mol2_charge, "observed_prmtop_charge": observed_charge,
        "expected_charge": expected_charge,
        "command_failure": command_failure, "tleap_errors": tleap_errors,
        "validation_errors": validation_errors,
        "tleap_warnings": [line.strip() for line in log_text.splitlines()
                           if re.search(r"\bWarning\b", line, re.IGNORECASE)],
        "files": {"mol2": str(mol2_path), "tleap_script": str(script),
                  "tleap_log": str(log), "prmtop": str(prmtop),
                  "inpcrd": str(inpcrd), "pdb": str(pdb)},
        "input_sha256": {str(path): _sha256(path) for path in input_files},
        "output_sha256": {str(path): _sha256(path) for path in (prmtop, inpcrd, pdb)
                          if path.is_file()},
    }
    _write_json(paths["mm_baseline_audit"], report)
    if not passed:
        raise WorkflowError(f"MM baseline build failed; see {paths['mm_baseline_audit']} and {log}.")
    return {
        "mm_baseline_build": str(paths["mm_baseline_audit"]),
        "prmtop": str(prmtop), "inpcrd": str(inpcrd),
        "atom_count": expected_count, "atom_order_verified": True,
        "observed_charge": observed_charge, "scanned_xyz_verified": len(scanned_xyz),
        "optimized_xyz_verified": len(optimized_xyz),
        "tleap_errors": [], "approved_for_production": False,
    }


def _scan_xyz_positions(path: Path, elements: list[str]) -> list[list[float]]:
    _xyz_order(path, elements)
    lines = path.read_text(encoding="utf-8").splitlines()[2:len(elements) + 2]
    return [[float(value) for value in line.split()[1:4]] for line in lines]


def _matching_force_terms(system: Any, kind: str, indices: list[int]) -> list[tuple[Any, int, tuple[Any, ...]]]:
    """Match exact atom indices, including all periodic torsions, never an entire atom-type class."""
    from openmm import HarmonicAngleForce, HarmonicBondForce, PeriodicTorsionForce
    force_class, count_method, parameter_method = {
        "bond": (HarmonicBondForce, "getNumBonds", "getBondParameters"),
        "angle": (HarmonicAngleForce, "getNumAngles", "getAngleParameters"),
        "dihedral": (PeriodicTorsionForce, "getNumTorsions", "getTorsionParameters"),
    }[kind]
    matches = []
    target = tuple(indices)
    for force in system.getForces():
        if not isinstance(force, force_class):
            continue
        count = getattr(force, count_method)()
        getter = getattr(force, parameter_method)
        for index in range(count):
            parameters = tuple(getter(index))
            atom_tuple = tuple(int(atom) for atom in parameters[:len(target)])
            if atom_tuple == target or atom_tuple == target[::-1]:
                matches.append((force, index, parameters))
    return matches


def evaluate_mm_baseline(args: argparse.Namespace) -> dict[str, Any]:
    """Evaluate MM-with-exact-target-term-removed on final constrained QM geometries."""
    paths = _layout(_path(args.root))
    _write_json(paths["mm_evaluation_audit"], {
        "schema_version": 1, "created_at_utc": _now(), "passed": False,
        "candidate_only": True, "reason": "evaluation in progress or interrupted",
    })
    build = _require_pass(paths["mm_baseline_audit"], "54-atom MM baseline")
    selection = _require_pass(paths["selection"], "QM model construction")
    collection = _require_pass(paths["scan_collection"], "Bonded QM collection")
    manifest = _require_pass(paths["scan_job_manifest"], "Bonded QM job preparation")
    if collection.get("collection_complete_for_provisional_fitting") is not True:
        raise WorkflowError("Bonded QM scan accounting is incomplete.")
    prmtop = paths["mm_baseline"] / "local_model.prmtop"
    inpcrd = paths["mm_baseline"] / "local_model.inpcrd"
    for path in (prmtop, inpcrd):
        if not path.is_file() or path.stat().st_size == 0:
            raise WorkflowError(f"MM baseline file is missing: {path}")
        if build.get("output_sha256", {}).get(str(path)) != _sha256(path):
            raise WorkflowError(f"MM baseline SHA-256 differs from build audit: {path}")
    for name, digest in build.get("input_sha256", {}).items():
        source = Path(name)
        if not source.is_file() or _sha256(source) != digest:
            raise WorkflowError(f"MM baseline source changed after TLEaP build: {source}")
    atom_map = selection["model_atom_map"]
    elements = [str(item["element"]).title() for item in atom_map]
    if len(elements) != 54 or build.get("atom_order_verified") is not True:
        raise WorkflowError("54-atom ordered baseline has not been verified.")
    typed_atoms = _read_json(paths["amber_atom_types"]).get("model_atom_types", [])
    if len(typed_atoms) != len(atom_map) or any(
        str(mapping["stable_id"]) != str(typed["stable_id"])
        or int(mapping["model_atom_index"]) != index
        or int(typed["model_atom_index"]) != index
        for index, (mapping, typed) in enumerate(zip(atom_map, typed_atoms))
    ):
        raise WorkflowError("QM atom map changed relative to ordered hybrid AMBER type map.")
    jobs = {item["job_id"]: item for item in manifest["jobs"]}
    records = collection["records"]
    if len(records) != len(jobs) - len(collection.get("excluded_failed_terminal_points", [])):
        raise WorkflowError("QM record count differs from planned jobs minus explicit exclusions.")
    try:
        from openmm import Context, LangevinIntegrator, Platform, app, unit
    except ImportError as exc:
        raise WorkflowError("OpenMM is required in the bioma_met_tbmb environment.") from exc
    topology = app.AmberPrmtopFile(str(prmtop))
    system = topology.createSystem(nonbondedMethod=app.NoCutoff, constraints=None)
    topology_atoms = list(topology.topology.atoms())
    topology_names = _prmtop_fields(prmtop, "ATOM_NAME")[:54]
    topology_types = _prmtop_fields(prmtop, "AMBER_ATOM_TYPE")[:54]
    if (len(topology_atoms) != 54 or len(topology_names) != 54
            or len(topology_types) != 54 or any(
        atom.element is None or atom.element.symbol != element
        or atom.name != name or atom_type != str(typed["hybrid_type"])
        for atom, element, name, atom_type, typed in zip(
            topology_atoms, elements, topology_names, topology_types, typed_atoms)
    )
    ):
        raise WorkflowError("OpenMM topology name/type/element order differs from QM model.")
    platform = Platform.getPlatformByName("CPU")
    integrator = LangevinIntegrator(300 * unit.kelvin, 1 / unit.picosecond,
                                    0.001 * unit.picoseconds)
    context = Context(system, integrator, platform)
    output_records = []
    try:
        for ordinal, record in enumerate(records, 1):
            job_id = record["job_id"]
            job = jobs.get(job_id)
            if job is None or record["term_id"] != job["term_id"]:
                raise WorkflowError(f"QM collection and job manifest disagree: {job_id}")
            result_path = _path(record["result_json"])
            if _sha256(result_path) != record["result_sha256"]:
                raise WorkflowError(f"QM result changed after collection: {job_id}")
            qm_result = _read_json(result_path)
            if (qm_result.get("job_id") != job_id
                    or qm_result.get("job_signature") != job.get("job_signature")
                    or qm_result.get("term_id") != job["term_id"]
                    or not math.isclose(float(qm_result.get("energy_hartree", math.nan)),
                                        float(record["energy_hartree"]), rel_tol=0, abs_tol=1e-8)):
                raise WorkflowError(f"QM result energy or job identity disagrees with collection: {job_id}")
            xyz = _path(qm_result.get("final_xyz", ""))
            if (qm_result.get("success") is not True or not xyz.is_file()
                    or _sha256(xyz) != qm_result.get("final_xyz_sha256")):
                raise WorkflowError(f"Final QM geometry missing or changed: {job_id}")
            indices = [int(value) for value in job["atom_indices_zero_based"]]
            if (any(index < 0 or index >= len(atom_map) for index in indices)
                    or [atom_map[index]["stable_id"] for index in indices] != job["atoms"]):
                raise WorkflowError(f"Scan atom indices disagree with ordered QM atom map: {job_id}")
            matches = _matching_force_terms(system, job["kind"], indices)
            if not matches or (job["kind"] != "dihedral" and len(matches) != 1):
                raise WorkflowError(f"Expected exact MM {job['kind']} term absent or ambiguous: {job_id}; matches={len(matches)}")
            forces = set()
            try:
                for force, index, parameters in matches:
                    if job["kind"] == "bond":
                        force.setBondParameters(index, *parameters[:-1], 0 * parameters[-1])
                    elif job["kind"] == "angle":
                        force.setAngleParameters(index, *parameters[:-1], 0 * parameters[-1])
                    else:
                        force.setTorsionParameters(index, *parameters[:-1], 0 * parameters[-1])
                    forces.add(force)
                for force in forces:
                    force.updateParametersInContext(context)
                context.setPositions(unit.Quantity(_scan_xyz_positions(xyz, elements), unit.angstrom))
                energy = float(context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilocalories_per_mole))
            finally:
                for force, index, parameters in matches:
                    if job["kind"] == "bond":
                        force.setBondParameters(index, *parameters)
                    elif job["kind"] == "angle":
                        force.setAngleParameters(index, *parameters)
                    else:
                        force.setTorsionParameters(index, *parameters)
                for force in forces:
                    force.updateParametersInContext(context)
            if not math.isfinite(energy):
                raise WorkflowError(f"Non-finite MM energy: {job_id}")
            output_records.append({
                "job_id": job_id, "term_id": job["term_id"], "kind": job["kind"],
                "target": float(record["target"]), "atom_indices_zero_based": indices,
                "matched_mm_terms": len(matches), "final_xyz": str(xyz),
                "final_xyz_sha256": _sha256(xyz), "qm_result_sha256": record["result_sha256"],
                "mm_without_target_kcal_mol": energy,
            })
            if ordinal % 25 == 0 or ordinal == len(records):
                print(f"[mm-baseline] {ordinal}/{len(records)} {job_id}", flush=True)
                _write_json(paths["mm_evaluation"], {
                    "schema_version": 1, "passed": False, "prmtop_sha256": _sha256(prmtop),
                    "records": output_records,
                })
    finally:
        del context
        del integrator
    report = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "candidate_only": True, "approved_for_production": False,
        "prmtop": str(prmtop), "prmtop_sha256": _sha256(prmtop),
        "inpcrd_sha256": _sha256(inpcrd), "scan_collection_sha256": _sha256(paths["scan_collection"]),
        "job_manifest_sha256": _sha256(paths["scan_job_manifest"]),
        "point_count": len(output_records), "planned_count": len(jobs),
        "excluded_failed_terminal_points": collection.get("excluded_failed_terminal_points", []),
        "records": output_records,
    }
    _write_json(paths["mm_evaluation"], report)
    _write_json(paths["mm_evaluation_audit"], {
        key: value for key, value in report.items() if key != "records"
    } | {"records_sha256": _sha256(paths["mm_evaluation"]),
         "mm_evaluation": str(paths["mm_evaluation"])})
    return {"mm_evaluation": str(paths["mm_evaluation"]),
            "audit": str(paths["mm_evaluation_audit"]), "evaluated": len(output_records),
            "approved_for_production": False}


def fit_qm_minus_mm(args: argparse.Namespace) -> dict[str, Any]:
    """Fit provisional AMBER terms to per-profile QM minus MM-with-target-removed curves."""
    try:
        import numpy as np
    except ImportError as exc:
        raise WorkflowError("NumPy is required for QM-minus-MM fitting.") from exc
    paths = _layout(_path(args.root))
    _write_json(paths["qm_minus_mm_audit"], {
        "schema_version": 1, "created_at_utc": _now(), "passed": False,
        "candidate_only": True, "reason": "fitting in progress or interrupted",
    })
    evaluation_audit = _require_pass(paths["mm_evaluation_audit"], "MM baseline evaluation")
    evaluation = _require_pass(paths["mm_evaluation"], "MM point energies")
    if _sha256(paths["mm_evaluation"]) != evaluation_audit.get("records_sha256"):
        raise WorkflowError("MM point energies changed after evaluation audit.")
    collection = _require_pass(paths["scan_collection"], "Bonded QM collection")
    plan = _require_pass(paths["scan_plan"], "Bonded scan planning")
    type_report = _read_json(paths["amber_atom_types"])
    stable_to_type = {str(item["stable_id"]): str(item["hybrid_type"])
                      for item in type_report.get("model_atom_types", [])}
    if not stable_to_type:
        raise WorkflowError("Hybrid AMBER atom type mapping is empty.")
    qm_by_id = {item["job_id"]: item for item in collection["records"]}
    mm_by_id = {item["job_id"]: item for item in evaluation["records"]}
    if set(qm_by_id) != set(mm_by_id):
        raise WorkflowError("QM and MM point identifiers differ.")
    grouped_records: dict[str, list[dict[str, Any]]] = {}
    for job_id, qm in qm_by_id.items():
        mm = mm_by_id[job_id]
        if mm["qm_result_sha256"] != qm["result_sha256"]:
            raise WorkflowError(f"QM/MM result provenance differs: {job_id}")
        residual = float(qm["energy_hartree"]) * 627.5094740631 - float(mm["mm_without_target_kcal_mol"])
        if not math.isfinite(residual):
            raise WorkflowError(f"Non-finite QM-minus-MM energy: {job_id}")
        grouped_records.setdefault(qm["term_id"], []).append({
            "job_id": job_id, "target": float(qm["target"]),
            "qm_energy_hartree": float(qm["energy_hartree"]),
            "mm_without_target_kcal_mol": float(mm["mm_without_target_kcal_mol"]),
            "qm_minus_mm_kcal_mol": residual,
        })
    profiles = []
    by_type: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    task_types = {}
    for task in plan["tasks"]:
        types = [stable_to_type[atom] for atom in task["atoms"]]
        key = _canonical_type_key(types)
        task_types[task["term_id"]] = (types, key)
        points = sorted(grouped_records.get(task["term_id"], []), key=lambda item: item["target"])
        if not points:
            continue
        minimum = min(item["qm_minus_mm_kcal_mol"] for item in points)
        for item in points:
            item["relative_qm_minus_mm_kcal_mol"] = item["qm_minus_mm_kcal_mol"] - minimum
        profile = {"term_id": task["term_id"], "kind": task["kind"],
                   "atoms": task["atoms"], "amber_types": types,
                   "canonical_amber_type_key": list(key), "points": points}
        profiles.append(profile)
        by_type.setdefault((task["kind"], key), []).append(profile)
    type_fits = []
    type_by_key = {}
    for (kind, key), members in sorted(by_type.items()):
        target_sets = [{round(point["target"], 8) for point in member["points"]}
                       for member in members]
        common = sorted(set.intersection(*target_sets))
        if len(common) < 7:
            raise WorkflowError(f"Too few common scan targets to pool {kind} {'-'.join(key)}")
        x = np.asarray(common, dtype=float)
        curves = np.asarray([
            [next(point["relative_qm_minus_mm_kcal_mol"] for point in member["points"]
                  if round(point["target"], 8) == target) for target in common]
            for member in members], dtype=float)
        pooled = curves.mean(axis=0)
        candidate = _fit_provisional_raw_qm_profile(np, kind, x, pooled)
        if kind in {"bond", "angle"}:
            equilibrium = candidate["equilibrium_angstrom" if kind == "bond" else "equilibrium_degree"]
            force_constant = candidate["force_constant_kcal_mol_a2" if kind == "bond" else "force_constant_kcal_mol_rad2"]
            if not math.isfinite(equilibrium) or not math.isfinite(force_constant):
                raise WorkflowError(f"Non-finite {kind} fit for {'-'.join(key)}")
            displacement = x - equilibrium if kind == "bond" else np.deg2rad(x - equilibrium)
            predicted = force_constant * displacement ** 2
            predicted -= predicted.min()
        else:
            phi = np.deg2rad(x)
            predicted = np.zeros_like(phi)
            for term in candidate.pop("raw_qm_fourier_terms"):
                predicted += term["amplitude_kcal_mol"] * (1 + np.cos(
                    term["periodicity"] * phi - math.radians(term["phase_degree"])))
            predicted -= predicted.min()
            candidate["amber_fourier_terms"] = [
                {"periodicity": term["periodicity"],
                 "amplitude_kcal_mol": term["amplitude_kcal_mol"],
                 "phase_degree": term["phase_degree"], "idivf": 1}
                for term in _fit_provisional_raw_qm_profile(np, kind, x, pooled)["raw_qm_fourier_terms"]
            ]
        rmse = float(np.sqrt(np.mean((predicted - pooled) ** 2)))
        fit = {"kind": kind, "canonical_amber_type_key": list(key),
               "representative_term_ids": [member["term_id"] for member in members],
               "profile_count": len(members), "point_count": len(common),
               "common_targets": common, "pooled_relative_qm_minus_mm_kcal_mol": pooled.tolist(),
               "between_profile_std_kcal_mol": curves.std(axis=0).tolist(),
               "fitted_relative_kcal_mol": predicted.tolist(),
               "full_profile_rmse_kcal_mol": rmse,
               "source": "HF/6-31G* relaxed constrained scan minus exact-term-removed AMBER MM",
               "reviewed": False, "candidate_only": True, **candidate}
        if kind in {"bond", "angle"}:
            fit["positive_curvature"] = bool(force_constant > 0 and math.isfinite(equilibrium))
            fit["equilibrium_within_scanned_range"] = bool(x.min() <= equilibrium <= x.max())
        type_fits.append(fit)
        type_by_key[(kind, key)] = fit
    fits = []
    for task in plan["tasks"]:
        types, key = task_types[task["term_id"]]
        source = type_by_key.get((task["kind"], key))
        if source is None:
            raise WorkflowError(f"No QM-minus-MM type candidate for {task['term_id']}")
        fits.append({"term_id": task["term_id"], "kind": task["kind"],
                     "atoms": task["atoms"], "amber_types": types,
                     "canonical_amber_type_key": list(key),
                     "representative_crosslink": task["representative_crosslink"],
                     "direct_qm_scan": task["term_id"] in grouped_records,
                     "transferred_from_representative_terms": source["representative_term_ids"],
                     "transfer_requires_review": True, "reviewed": False,
                     "type_fit": source})
    report = {"schema_version": 1, "created_at_utc": _now(), "passed": True,
              "candidate_only": True, "approved_for_production": False,
              "qm_collection_sha256": _sha256(paths["scan_collection"]),
              "mm_evaluation_sha256": _sha256(paths["mm_evaluation"]),
              "scanned_point_count": len(qm_by_id),
              "planned_point_count": len(qm_by_id) + len(collection.get("excluded_failed_terminal_points", [])),
              "excluded_failed_terminal_points": collection.get("excluded_failed_terminal_points", []),
              "representative_profiles": profiles, "type_fits": type_fits, "fits": fits,
              "type_fit_group_count": len(type_fits), "planned_term_candidate_count": len(fits),
              "required_next_step": "Qualified review of residual fits, high-energy outliers, exclusions, and three-arm transfer; do not auto-approve."}
    _write_json(paths["qm_minus_mm_fit"], report)
    review = _read_json(paths["scan_review"]) if paths["scan_review"].is_file() else {"terms": []}
    by_id = {fit["term_id"]: fit for fit in fits}
    for item in review.get("terms", []):
        if item.get("reviewed") is not True and item.get("term_id") in by_id:
            item["qm_minus_mm_candidate"] = by_id[item["term_id"]]
    review["qm_minus_mm_candidate_file"] = str(paths["qm_minus_mm_fit"])
    review["instructions"] = ("Review QM-minus-MM curves and fits, exclusions and transfer; "
                              "copy justified values into the review fields and sign manually. "
                              "Candidates are not production-approved.")
    _write_json(paths["scan_review"], review)
    _write_json(paths["qm_minus_mm_audit"], {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "candidate_only": True, "approved_for_production": False,
        "candidate_file": str(paths["qm_minus_mm_fit"]),
        "candidate_sha256": _sha256(paths["qm_minus_mm_fit"]),
        "scanned_point_count": len(qm_by_id), "type_fit_group_count": len(type_fits),
        "planned_term_candidate_count": len(fits),
    })
    return {"candidate_file": str(paths["qm_minus_mm_fit"]),
            "audit": str(paths["qm_minus_mm_audit"]),
            "review_template": str(paths["scan_review"]),
            "scanned_points": len(qm_by_id), "type_groups": len(type_fits),
            "planned_term_candidates": len(fits), "approved_for_production": False}


def build_amber_candidates(args: argparse.Namespace) -> dict[str, Any]:
    """Build unapproved ff14SB/RESP/GAFF2 candidate libraries and test assembly."""
    paths = _layout(_path(args.root))
    selection = _require_pass(paths["selection"], "QM model construction")
    charges = _require_pass(paths["charges"], "RESP fit")
    _require_pass(paths["charge_audit"], "RESP charge audit")
    _mkdir(paths["amber"])
    local_atoms, local_bonds = _prepare_local_resp_mol2(paths)
    charge_file = paths["amber"] / "resp_charges.dat"
    _run_logged([
        args.antechamber, "-i", str(paths["amber_local_input"]), "-fi", "mol2",
        "-o", str(paths["amber_local_typed"]), "-fo", "mol2", "-at", "gaff2",
        "-c", "rc", "-cf", str(charge_file), "-rn", "LOC", "-dr", "no",
        "-an", "no", "-pf", "y",
    ], paths["amber"] / "antechamber.log", paths["amber"])
    typed = _parse_mol2(paths["amber_local_typed"])
    if len(typed["atoms"]) != len(local_atoms):
        raise WorkflowError("Antechamber changed local-model atom count.")
    typed_by_name = {item["name"]: item for item in typed["atoms"]}
    if len(typed_by_name) != len(typed["atoms"]) or set(typed_by_name) != {item["name"] for item in local_atoms}:
        raise WorkflowError("Antechamber changed local-model atom names; stable type mapping is unsafe.")
    typed["atoms"] = [typed_by_name[item["name"]] for item in local_atoms]

    standard_mol2_path = paths["amber"] / "ff14sb_ace_met_nhe.mol2"
    standard_script = paths["amber"] / "make_ff14sb_reference.leap"
    standard_script.write_text(
        "source leaprc.protein.ff14SB\n"
        "standard = sequence { ACE MET NHE }\n"
        f'saveMol2 standard "{standard_mol2_path}" 1\nquit\n', encoding="utf-8",
    )
    _run_logged([args.tleap, "-f", str(standard_script)],
                paths["amber"] / "ff14sb_reference_tleap.log", paths["amber"])
    standard = _parse_mol2(standard_mol2_path)
    met_heavy, met_h = _standard_residue_records(standard, "MET")
    nhe_heavy, nhe_h = _standard_residue_records(standard, "NHE")

    # parmchk2 must see the real ff14SB CB boundary type, not an artificial
    # all-GAFF2 methyl-cap boundary.
    hybrid_atoms = []
    cb_type = met_heavy["CB"]["type"]
    cb_h_type = met_h["CB"][0]["type"] if met_h.get("CB") else "HC"
    for mapping, atom in zip(selection["model_atom_map"], typed["atoms"]):
        stable_id = str(mapping["stable_id"])
        atom_type = atom["type"]
        if re.fullmatch(r"PEP:[0-9]+:CB", stable_id):
            atom_type = cb_type
        elif re.fullmatch(r"PEP:[0-9]+:CB:H[0-9]+", stable_id):
            atom_type = cb_h_type
        hybrid_atoms.append({
            "name": atom["name"], "type": atom_type, "charge": atom["charge"],
            "xyz": atom["xyz"], "stable_id": stable_id,
        })
    _write_mol2(paths["amber_hybrid_typed"], "TBMB_MET_HYBRID", hybrid_atoms,
                local_bonds, "LOC")
    _run_logged([
        args.parmchk2, "-i", str(paths["amber_hybrid_typed"]), "-f", "mol2",
        "-o", str(paths["amber_frcmod_raw"]), "-s", "gaff2",
    ], paths["amber"] / "parmchk2.log", paths["amber"])
    standard_types = {
        str(record["type"])
        for record in [*met_heavy.values(), *nhe_heavy.values()]
    }
    standard_types.update(
        str(record["type"])
        for records in [*met_h.values(), *nhe_h.values()]
        for record in records
    )
    link_filter = _filter_candidate_frcmod(
        paths["amber_frcmod_raw"], paths["amber_frcmod"], standard_types,
    )

    local_sources = _local_parameter_sources(selection, typed, charges)
    molecule = _reference_with_stable_hydrogens(paths)
    positions = tuple(int(value) for value in args.met_positions)
    internal_positions = [value for value in positions if value not in {1, len(args.sequence)}]
    if not internal_positions:
        raise WorkflowError("At least one internal crosslinked methionine is needed to build MSU.")
    templates: dict[str, dict[str, Any]] = {
        INTERNAL_MET_TEMPLATE: _template_for_met(
            molecule, internal_positions[0], INTERNAL_MET_TEMPLATE, local_sources,
            met_heavy, met_h, nhe_heavy, nhe_h, False,
        ),
        LINKER_TEMPLATE: _template_for_tmb(molecule, local_sources),
    }
    if len(args.sequence) in positions:
        templates[C_TERMINAL_MET_TEMPLATE] = _template_for_met(
            molecule, len(args.sequence), C_TERMINAL_MET_TEMPLATE, local_sources,
            met_heavy, met_h, nhe_heavy, nhe_h, True,
        )
    if 1 in positions:
        templates[N_TERMINAL_MET_TEMPLATE] = _template_for_met(
            molecule, 1, N_TERMINAL_MET_TEMPLATE, local_sources,
            met_heavy, met_h, nhe_heavy, nhe_h, False,
        )

    variant_counts = {
        INTERNAL_MET_TEMPLATE: sum(value not in {1, len(args.sequence)} for value in positions),
        N_TERMINAL_MET_TEMPLATE: sum(value == 1 for value in positions),
        C_TERMINAL_MET_TEMPLATE: sum(value == len(args.sequence) for value in positions),
        LINKER_TEMPLATE: 1,
    }
    raw_module_charge = sum(
        variant_counts.get(name, 0) * sum(float(atom["charge"]) for atom in template["atoms"])
        for name, template in templates.items()
    )
    correction = float(LOCAL_MODEL_CHARGE) - raw_module_charge
    tmb_by_name = {atom["name"]: atom for atom in templates[LINKER_TEMPLATE]["atoms"]}
    attachment_names = [name for name in ("CM1", "CM2", "CM3") if name in tmb_by_name]
    if len(attachment_names) != 3:
        raise WorkflowError("TMB template does not contain CM1/CM2/CM3 for symmetric charge reconciliation.")
    if abs(correction) > float(args.max_charge_transfer_correction):
        raise WorkflowError(
            f"Boundary charge correction {correction:+.6f} e exceeds the configured scientific gate "
            f"{args.max_charge_transfer_correction:.3f} e."
        )
    for name in attachment_names:
        tmb_by_name[name]["charge"] += correction / 3.0
        tmb_by_name[name]["charge_adjustment"] = correction / 3.0
        tmb_by_name[name]["parameter_source"] += "+boundary_charge_reconciliation"
    final_module_charge = sum(
        variant_counts.get(name, 0) * sum(float(atom["charge"]) for atom in template["atoms"])
        for name, template in templates.items()
    )
    charge_transfer = {
        "schema_version": 1, "created_at_utc": _now(), "passed": abs(final_module_charge - LOCAL_MODEL_CHARGE) < 1e-6,
        "raw_module_charge": raw_module_charge, "target_module_charge": LOCAL_MODEL_CHARGE,
        "correction": correction, "correction_policy": "equal shift over symmetry-related TMB CM1/CM2/CM3 atoms",
        "per_atom_shift": correction / 3.0, "final_module_charge": final_module_charge,
        "variant_counts": variant_counts,
        "requires_qualified_review": True,
        "warning": "The correction is a boundary reconciliation, not a new RESP fit; a qualified reviewer must approve it.",
    }
    _write_json(paths["amber_charge_transfer"], charge_transfer)

    candidate_records = []
    for name, template in templates.items():
        mol2_path = paths["amber"] / f"{name}.mol2"
        _write_mol2(mol2_path, name, template["atoms"], template["bonds"], name)
        source_counts: dict[str, int] = {}
        for atom in template["atoms"]:
            source_name = str(atom["parameter_source"])
            source_counts[source_name] = source_counts.get(source_name, 0) + 1
        candidate_records.append({"residue_name": name, "mol2": str(mol2_path),
                                  "mol2_sha256": _sha256(mol2_path),
                                  "charge": sum(float(atom["charge"]) for atom in template["atoms"]),
                                  "parameter_source_counts": source_counts,
                                  "atom_parameter_sources": [
                                      {
                                          "name": atom["name"],
                                          "stable_id": atom["stable_id"],
                                          "type": atom["type"],
                                          "charge": atom["charge"],
                                          "source": atom["parameter_source"],
                                          **(
                                              {"charge_adjustment": atom["charge_adjustment"]}
                                              if "charge_adjustment" in atom else {}
                                          ),
                                      }
                                      for atom in template["atoms"]
                                  ]})

    # The capped RESP model deliberately excludes the peptide backbone, so it
    # cannot expose the CA-CB-CG seam to parmchk2.  Run a second candidate pass
    # on the complete MSU residue, then retain only mixed ff14SB/non-standard
    # terms involving the ff14SB alpha-carbon type CX.  Raw output is retained
    # and no analogue is promoted to reviewed status automatically.
    boundary_input = paths["amber"] / f"{INTERNAL_MET_TEMPLATE}.mol2"
    _run_logged([
        args.parmchk2, "-i", str(boundary_input), "-f", "mol2",
        "-o", str(paths["amber_boundary_frcmod_raw"]), "-s", "gaff2",
    ], paths["amber"] / "parmchk2_boundary.log", paths["amber"])
    boundary_filter = _filter_candidate_frcmod(
        paths["amber_boundary_frcmod_raw"], paths["amber_boundary_frcmod"],
        standard_types, required_boundary_type="CX",
    )
    required_boundary_angle = ("CX", "2C", "c3")
    angle_terms = {
        tuple(item["atom_types"])
        for item in boundary_filter["kept"] if item["section"] == "ANGLE"
    }
    if required_boundary_angle not in angle_terms and tuple(reversed(required_boundary_angle)) not in angle_terms:
        raise WorkflowError(
            "parmchk2 did not generate the required CX-2C-c3 peptide-boundary angle candidate; "
            f"see {paths['amber'] / 'parmchk2_boundary.log'} and "
            f"{paths['amber_boundary_frcmod_raw']}."
        )
    _write_json(paths["amber_frcmod_filter_audit"], {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "policy": (
            "ff14SB-only terms are excluded from runtime FRCMODs; mixed terms remain "
            "unapproved parmchk2 analogue candidates pending bonded-term review"
        ),
        "link_model": link_filter, "peptide_boundary_model": boundary_filter,
        "required_boundary_angle": list(required_boundary_angle),
    })

    library_script = paths["amber"] / "build_libraries.leap"
    script_lines = ["source leaprc.protein.ff14SB", "source leaprc.gaff2"]
    for name in templates:
        script_lines.append(f'{name} = loadMol2 "{paths["amber"] / (name + ".mol2")}"')
        if name in {INTERNAL_MET_TEMPLATE, N_TERMINAL_MET_TEMPLATE, C_TERMINAL_MET_TEMPLATE}:
            script_lines.append(f"set {name} head {name}.1.N")
        if name in {INTERNAL_MET_TEMPLATE, N_TERMINAL_MET_TEMPLATE}:
            script_lines.append(f"set {name} tail {name}.1.C")
        script_lines.extend([f"check {name}", f'saveOff {name} "{paths["amber"] / (name + ".lib")}"'])
    script_lines.append("quit")
    library_script.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    _run_logged([args.tleap, "-f", str(library_script)],
                paths["amber"] / "library_tleap.log", paths["amber"])
    for record in candidate_records:
        lib_path = paths["amber"] / f"{record['residue_name']}.lib"
        if not lib_path.is_file() or lib_path.stat().st_size == 0:
            raise WorkflowError(f"TLEaP did not create residue library: {lib_path}")
        record.update({"library": str(lib_path), "library_sha256": _sha256(lib_path)})

    type_report = {
        "schema_version": 1, "model_atom_types": [
            {"model_atom_index": index, "stable_id": mapping["stable_id"],
             "gaff2_type": typed_atom["type"], "hybrid_type": hybrid_atoms[index]["type"]}
            for index, (mapping, typed_atom) in enumerate(zip(selection["model_atom_map"], typed["atoms"]))
        ],
    }
    _write_json(paths["amber_atom_types"], type_report)
    frcmod_paths = [paths["amber_frcmod"], paths["amber_boundary_frcmod"]]
    problems = [
        f"{path.name}: {problem}"
        for path in frcmod_paths for problem in _frcmod_problems_local(path)
    ]
    build = {
        "schema_version": 1, "created_at_utc": _now(), "passed": True,
        "candidate_only": True, "templates": candidate_records,
        "frcmod": str(paths["amber_frcmod"]), "frcmod_sha256": _sha256(paths["amber_frcmod"]),
        "frcmods": [
            {"path": str(path), "sha256": _sha256(path)} for path in frcmod_paths
        ],
        "frcmod_filter_audit": str(paths["amber_frcmod_filter_audit"]),
        "frcmod_review_problems": problems,
        "charge_transfer_audit": str(paths["amber_charge_transfer"]),
        "qualified_review_required": True,
        "approval_blockers": [
            "qualified review/sign-off of charge-transfer correction",
            "replacement or explicit approval of every parmchk2 ATTN/need-revision term",
            "qualified review of filtered ff14SB/non-standard peptide-boundary analogue terms",
            "reviewed QM-minus-MM bonded fits for crosslink terms",
            "independent reference-sequence TLEaP validation",
        ],
    }
    _write_json(paths["amber_build_audit"], build)
    validation = validate_amber_candidates(args)
    if not validation["technical_assembly_passed"]:
        validation_report = _read_json(paths["amber_validation"])
        primary_reason = (
            validation_report.get("command_failure")
            or (validation_report.get("tleap_errors") or [""])[0]
            or validation_report.get("charge_parse_error")
            or "unknown TLEaP validation failure"
        )
        raise WorkflowError(
            "AMBER candidate files were generated, but reference-sequence TLEaP validation failed; "
            f"primary reason: {primary_reason}; "
            f"see {validation['amber_candidate_validation']} and "
            f"{paths['amber'] / 'validation_tleap.log'}."
        )
    return {"amber_candidate_build": str(paths["amber_build_audit"]),
            "amber_candidate_validation": validation["amber_candidate_validation"],
            "templates": len(candidate_records), "frcmod_review_problems": len(problems),
            "technical_assembly_passed": validation["technical_assembly_passed"],
            "approved_for_production": False}


def validate_amber_candidates(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    build = _require_pass(paths["amber_build_audit"], "AMBER candidate build")
    reference_pdb = paths["amber"] / "reference_ligand_amber.pdb"
    mapping = _write_reference_amber_pdb(paths, args, reference_pdb)
    script = paths["amber"] / "validate_reference.leap"
    lines = ["source leaprc.protein.ff14SB", "source leaprc.gaff2", "set default PBRadii mbondi3"]
    for record in build["templates"]:
        lines.append(f'loadOff "{record["library"]}"')
    lines.extend([
        f'loadAmberParams "{paths["amber_frcmod"]}"',
        f'loadAmberParams "{paths["amber_boundary_frcmod"]}"',
    ])
    if paths["amber_reviewed_frcmod"].is_file():
        lines.append(f'loadAmberParams "{paths["amber_reviewed_frcmod"]}"')
    lines.append(f'ligand = loadPDB "{reference_pdb}"')
    for crosslink in mapping["crosslinks"]:
        lines.append(
            f"bond ligand.{crosslink['peptide_residue_index']}.{crosslink['methionine_atom']} "
            f"ligand.{crosslink['linker_residue_index']}.{crosslink['linker_atom']}"
        )
    prmtop = paths["amber"] / "reference_ligand.prmtop"
    inpcrd = paths["amber"] / "reference_ligand.inpcrd"
    assembled = paths["amber"] / "reference_ligand_assembled.pdb"
    lines.extend(["check ligand", f'saveAmberParm ligand "{prmtop}" "{inpcrd}"',
                  f'savePdb ligand "{assembled}"', "quit"])
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # These files are fully reproducible products of this validation run.
    # Remove stale/partial files so a failed TLEaP command cannot be mistaken
    # for a current topology or mask the actual command failure.
    for generated in (prmtop, inpcrd, assembled):
        if generated.exists():
            generated.unlink()
    command_failure = ""
    try:
        _run_logged([args.tleap, "-f", str(script)],
                    paths["amber"] / "validation_tleap.log", paths["amber"])
    except WorkflowError as exc:
        command_failure = str(exc)
    log_path = paths["amber"] / "validation_tleap.log"
    log_text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    patterns = (r"FATAL", r"Could not find", r"does not have a type", r"No torsion terms", r"Errors\s*=\s*[1-9]")
    leap_errors = [line.strip() for line in log_text.splitlines()
                   if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns)]
    log_tail = [line.rstrip() for line in log_text.splitlines() if line.strip()][-80:]
    charge = None
    charge_parse_error = ""
    if not command_failure and prmtop.is_file() and prmtop.stat().st_size > 0:
        try:
            charge = _prmtop_charge(prmtop)
        except WorkflowError as exc:
            charge_parse_error = str(exc)
    elif not command_failure:
        charge_parse_error = f"TLEaP returned success but did not create a non-empty topology: {prmtop}"
    charge_ok = charge is not None and abs(charge - float(args.formal_charge)) <= float(args.charge_tolerance)
    frcmod_problems = [
        f"{path.name}: {problem}"
        for path in (paths["amber_frcmod"], paths["amber_boundary_frcmod"])
        for problem in _frcmod_problems_local(path)
    ]
    technical = (
        not command_failure and not leap_errors and not charge_parse_error
        and prmtop.is_file() and prmtop.stat().st_size > 0
        and inpcrd.is_file() and inpcrd.stat().st_size > 0 and charge_ok
    )
    report = {
        "schema_version": 1, "created_at_utc": _now(), "passed": technical,
        "technical_assembly_passed": technical, "production_parameterization_approved": False,
        "command_failure": command_failure, "tleap_errors": leap_errors,
        "charge_parse_error": charge_parse_error,
        "tleap_log_tail": log_tail,
        "observed_charge": charge, "expected_charge": int(args.formal_charge),
        "charge_tolerance": float(args.charge_tolerance), "charge_passed": charge_ok,
        "crosslinks_requested": mapping["crosslinks"],
        "frcmod_review_problems": frcmod_problems,
        "files": {
            "reference_pdb": str(reference_pdb), "tleap_input": str(script),
            "tleap_log": str(log_path), "prmtop": str(prmtop), "inpcrd": str(inpcrd),
            "assembled_pdb": str(assembled),
        },
        "scientific_blockers": [
            "technical TLEaP assembly is not scientific approval",
            "charge-transfer boundary correction requires qualified review",
            "parmchk2 analogue warnings must be resolved from QM scans or cited analogues",
            "CX-2C-c3 peptide-boundary candidate terms require qualified analogue/QM review",
            "bonded_scan_review.json must be completed and signed",
        ],
    }
    _write_json(paths["amber_validation"], report)
    return {"amber_candidate_validation": str(paths["amber_validation"]),
            "technical_assembly_passed": technical, "observed_charge": charge,
            "frcmod_review_problems": len(frcmod_problems), "approved_for_production": False}


def draft_source_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Write an explicitly unapproved hand-off manifest; never self-approve science."""
    paths = _layout(_path(args.root))
    if paths["source_manifest"].is_file():
        try:
            import yaml
            existing = yaml.safe_load(paths["source_manifest"].read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise WorkflowError("Existing source manifest cannot be read; refusing to overwrite it.") from exc
        if existing.get("approved") is True:
            raise WorkflowError("Source manifest is approved; use a new versioned root instead of overwriting it.")
    qm = _read_json(paths["qm_collection"]) if paths["qm_collection"].is_file() else {}
    charge = _read_json(paths["charge_audit"]) if paths["charge_audit"].is_file() else {}
    bonded = _read_json(paths["scan_audit"]) if paths["scan_audit"].is_file() else {}
    amber_build = _read_json(paths["amber_build_audit"]) if paths["amber_build_audit"].is_file() else {}
    amber_validation = _read_json(paths["amber_validation"]) if paths["amber_validation"].is_file() else {}
    transfer = _read_json(paths["amber_charge_transfer"]) if paths["amber_charge_transfer"].is_file() else {}
    try:
        import yaml
    except ImportError as exc:
        raise WorkflowError("PyYAML is required to write source_manifest.yaml.") from exc
    templates = [
        f"{INTERNAL_MET_TEMPLATE}.lib",
        f"{C_TERMINAL_MET_TEMPLATE}.lib",
        f"{LINKER_TEMPLATE}.lib",
    ]
    if 1 in tuple(args.met_positions):
        templates.insert(1, f"{N_TERMINAL_MET_TEMPLATE}.lib")
    files = []
    for name in templates:
        path = paths["amber"] / name
        files.append({"residue_name": Path(name).stem, "format": "off",
                      "path": str(path.relative_to(paths["root"])),
                      "sha256": _sha256(path) if path.is_file() else "PENDING"})
    frcmod_files = [
        {"path": str(path.relative_to(paths["root"])),
         "sha256": _sha256(path) if path.is_file() else "PENDING"}
        for path in (paths["amber_frcmod"], paths["amber_boundary_frcmod"])
    ]
    if paths["amber_reviewed_frcmod"].is_file():
        frcmod_files.append({"path": str(paths["amber_reviewed_frcmod"].relative_to(paths["root"])),
                             "sha256": _sha256(paths["amber_reviewed_frcmod"])})
    manifest = {
        "schema_version": 1,
        "approved": False,
        "standard_residue_forcefield": "ff14SB",
        "reviewed_by": "PENDING_QUALIFIED_REVIEW",
        "scaffold": {
            "linker": "TBMB", "crosslink_count": 3, "methionine_atom": "SD",
            "linker_atoms": ["CM1", "CM2", "CM3"],
            "c_terminal_template_includes_cap": True, "c_terminal_cap_atom_name": "NT",
            "sulfur_formal_charge_each": 1,
        },
        "charge_derivation": {
            "method": "multiconformer_RESP", "qm_level": "HF/6-31G*",
            "conformer_count": len(qm.get("records", [])),
            "electronic_convergence_verified": qm.get("electronic_convergence_verified") is True,
            "charge_constraints_verified": charge.get("charge_constraints_verified") is True,
            "charge_audit": str(paths["charge_audit"]),
            "boundary_reconciliation": {
                "reviewed": False,
                "audit": str(paths["amber_charge_transfer"]),
                "correction": transfer.get("correction"),
                "policy": transfer.get("correction_policy"),
            },
        },
        "bonded_parameter_derivation": {
            "method": "reviewed relaxed QM scans and explicitly cited ff14SB/GAFF2 analogues",
            "reviewed": bonded.get("bonded_parameters_reviewed") is True,
            "audit": str(paths["scan_audit"]),
            "peptide_boundary_terms_reviewed": False,
            "peptide_boundary_audit": str(paths["amber_frcmod_filter_audit"]),
        },
        "files": {
            "residue_templates": files,
            "frcmod": frcmod_files,
        },
        "candidate_generation": {
            "candidate_only": True,
            "build_audit": str(paths["amber_build_audit"]),
            "technical_assembly_audit": str(paths["amber_validation"]),
            "technical_assembly_passed": amber_validation.get("technical_assembly_passed") is True,
            "frcmod_review_problems": amber_build.get("frcmod_review_problems", []),
        },
        "approval_blockers": [
            "Qualified review of charge transfer and integer template charges",
            "Completed and reviewed crosslink bonded terms",
            "Every parmchk2 ATTN/need-revision term replaced or explicitly approved",
            "Filtered ff14SB/non-standard peptide-boundary analogue terms reviewed and signed",
            "MSU/CMU/TMB library and FRCMOD candidate files promoted after review",
            "Reference-sequence TLEaP assembly, charge and missing-parameter validation",
        ],
    }
    _mkdir(paths["source_manifest"].parent)
    paths["source_manifest"].write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {"source_manifest": str(paths["source_manifest"]), "approved": False,
            "electronic_convergence_verified": manifest["charge_derivation"]["electronic_convergence_verified"],
            "charge_constraints_verified": manifest["charge_derivation"]["charge_constraints_verified"],
            "bonded_parameters_reviewed": manifest["bonded_parameter_derivation"]["reviewed"]}


def status(args: argparse.Namespace) -> dict[str, Any]:
    paths = _layout(_path(args.root))
    stages = {}
    for name, path in (("input_audit", paths["audit"]), ("qm_models", paths["selection"]),
                       ("psi4_preparation", paths["job_manifest"]),
                       ("psi4_collection", paths["qm_collection"]),
                       ("resp_fit", paths["charge_audit"]), ("bonded_scan_plan", paths["scan_plan"]),
                       ("bonded_qm_preparation", paths["scan_job_manifest"]),
                       ("bonded_qm_collection", paths["scan_collection"]),
                       ("bonded_fit_candidates", paths["scan_fit"]),
                       ("amber_candidate_build", paths["amber_build_audit"]),
                       ("mm_baseline_build", paths["mm_baseline_audit"]),
                       ("mm_baseline_evaluation", paths["mm_evaluation_audit"]),
                       ("qm_minus_mm_fit", paths["qm_minus_mm_audit"]),
                       ("bonded_scan_review", paths["scan_audit"]),
                       ("reviewed_frcmod", paths["amber_reviewed_audit"]),
                       ("amber_candidate_validation", paths["amber_validation"])):
        value = _read_json(path) if path.is_file() else {}
        stages[name] = {"path": str(path), "exists": path.is_file(), "passed": value.get("passed") is True}
    manifest = {}
    if paths["source_manifest"].is_file():
        try:
            import yaml
            manifest = yaml.safe_load(paths["source_manifest"].read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - status must remain readable after a malformed draft
            manifest = {}
    report = {
        "schema_version": 1, "created_at_utc": _now(), "stages": stages,
        "source_manifest": str(paths["source_manifest"]),
        "source_approved": manifest.get("approved") is True,
        "ready_for_existing_parameterize_tool": manifest.get("approved") is True
            and stages["resp_fit"]["passed"] and stages["mm_baseline_evaluation"]["passed"]
            and stages["qm_minus_mm_fit"]["passed"] and stages["bonded_scan_review"]["passed"]
            and stages["amber_candidate_validation"]["passed"],
        "next_stage": next((name for name, item in stages.items() if not item["passed"]),
                           "qualified review and TLEaP validation"),
    }
    _write_json(paths["status"], report)
    return report


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, help="Parameter source root directory.")
    parser.add_argument("--sdf", help="Authoritative multi-record ensemble SDF.")
    parser.add_argument("--metadata", help="Authoritative chemical metadata JSON.")
    parser.add_argument("--reference-pdb", help="PDB matching the first SDF conformer.")
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--met-positions", nargs=3, type=int, default=list(DEFAULT_MET_POSITIONS))
    parser.add_argument("--formal-charge", type=int, default=DEFAULT_FORMAL_CHARGE)
    parser.add_argument("--conformers", type=int, default=4)


def _require_cli_inputs(args: argparse.Namespace, names: Iterable[str]) -> None:
    missing = [name for name in names if not getattr(args, name, None)]
    if missing:
        raise WorkflowError("This stage requires: " + ", ".join("--" + name.replace("_", "-") for name in missing))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="stage", required=True)
    stages = ["audit-inputs", "build-qm-models", "prepare-psi4", "run-psi4", "collect-psi4",
              "fit-resp", "prepare-bonded-scans", "prepare-bonded-qm", "run-bonded-qm",
              "collect-bonded-qm", "fit-bonded-candidates", "collect-bonded-scans",
              "apply-reviewed-bonded",
              "build-amber-candidates", "build-mm-baseline", "evaluate-mm-baseline",
              "fit-qm-minus-mm", "validate-amber-candidates",
              "draft-source-manifest", "status", "all-pre-qm"]
    for name in stages:
        child = subparsers.add_parser(name)
        _add_common(child)
        if name in {"run-psi4", "run-bonded-qm"}:
            child.add_argument("--only", help="Run one conformer job, e.g. conf_01.")
            child.add_argument("--memory", default="8 GB")
            child.add_argument("--resume", action="store_true", help="Reuse successful hash-matching outputs.")
            child.add_argument("--keep-going", action="store_true")
        if name == "run-bonded-qm":
            child.add_argument(
                "--threads", type=int, default=1,
                help="Psi4 CPU threads for each bonded-QM optimization (default: 1).",
            )
            child.add_argument(
                "--optimizer", choices=("geometric", "optking"), default="geometric",
                help="Constrained optimizer; geomeTRIC avoids OptKing back-transform loops (default: geometric).",
            )
        if name == "prepare-bonded-qm":
            child.add_argument("--all-arms", action="store_true",
                               help="Scan all three chemically equivalent arms instead of the first arm only.")
            child.add_argument("--terms", nargs="*", help="Optional explicit term IDs from bonded_scan_plan.json.")
        if name == "collect-bonded-scans":
            child.add_argument("--review", help="Reviewed bonded-scan JSON; defaults to raw/bonded_scan_review.json.")
        if name == "collect-bonded-qm":
            child.add_argument(
                "--exclude-failed-job", action="append", default=[], metavar="JOB_ID",
                help=("Explicitly exclude one failed terminal bond/angle scan point; repeat for multiple "
                      "terms. Dihedral, missing, stale, converged, and non-terminal jobs cannot be excluded."),
            )
            child.add_argument(
                "--exclusion-reason",
                help="Scientific reason recorded for every explicitly excluded failed terminal point.",
            )
            child.add_argument(
                "--exclusion-recorded-by",
                help="Operator recording the exclusion; this is provenance, not production approval.",
            )
        if name in {"build-amber-candidates", "validate-amber-candidates"}:
            child.add_argument("--antechamber", default="antechamber")
            child.add_argument("--parmchk2", default="parmchk2")
            child.add_argument("--tleap", default="tleap")
            child.add_argument("--charge-tolerance", type=float, default=0.05)
            child.add_argument(
                "--max-charge-transfer-correction", type=float, default=0.25,
                help="Maximum absolute boundary reconciliation allowed before stopping (e).",
            )
        if name == "build-mm-baseline":
            child.add_argument("--tleap", default="tleap")
    args = parser.parse_args()
    functions = {
        "audit-inputs": audit_inputs, "build-qm-models": build_qm_models,
        "prepare-psi4": prepare_psi4, "run-psi4": run_psi4,
        "collect-psi4": collect_psi4, "fit-resp": fit_resp,
        "prepare-bonded-scans": prepare_bonded_scans,
        "prepare-bonded-qm": prepare_bonded_qm,
        "run-bonded-qm": run_bonded_qm,
        "collect-bonded-qm": collect_bonded_qm,
        "fit-bonded-candidates": fit_bonded_candidates,
        "collect-bonded-scans": collect_bonded_scans,
        "apply-reviewed-bonded": apply_reviewed_bonded,
        "build-amber-candidates": build_amber_candidates,
        "build-mm-baseline": build_mm_baseline,
        "evaluate-mm-baseline": evaluate_mm_baseline,
        "fit-qm-minus-mm": fit_qm_minus_mm,
        "validate-amber-candidates": validate_amber_candidates,
        "draft-source-manifest": draft_source_manifest, "status": status,
    }
    try:
        if args.stage == "all-pre-qm":
            _require_cli_inputs(args, ("sdf", "metadata", "reference_pdb"))
            outputs = {}
            for name in ("audit-inputs", "build-qm-models", "prepare-psi4"):
                print(f"=== {name} ===", flush=True)
                outputs[name] = functions[name](args)
            result = outputs
        else:
            if args.stage == "audit-inputs":
                _require_cli_inputs(args, ("sdf", "metadata", "reference_pdb"))
            result = functions[args.stage](args)
        print(json.dumps({"status": "ok", "stage": args.stage, "result": result},
                         ensure_ascii=False, indent=2))
    except WorkflowError as exc:
        print(json.dumps({"status": "error", "stage": args.stage, "error": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2) from exc


_SOURCE_PHASE_STAGES: dict[str, tuple[tuple[str, str], ...]] = {
    "prepare": (
        ("audit-inputs", "resp"), ("build-qm-models", "resp"),
        ("prepare-psi4", "resp"), ("prepare-bonded-scans", "resp"),
        ("prepare-bonded-qm", "resp"),
    ),
    "esp": (("run-psi4", "qm"), ("collect-psi4", "resp"), ("fit-resp", "resp")),
    "bonded-qm": (("run-bonded-qm", "qm"), ("collect-bonded-qm", "resp")),
    "fit": (
        ("build-amber-candidates", "amber"), ("fit-bonded-candidates", "amber"),
        ("build-mm-baseline", "amber"), ("evaluate-mm-baseline", "amber"),
        ("fit-qm-minus-mm", "amber"),
    ),
    "finalize": (
        ("collect-bonded-scans", "amber"), ("apply-reviewed-bonded", "amber"),
        ("validate-amber-candidates", "amber"), ("draft-source-manifest", "amber"),
    ),
}


def _source_phase_plan(phase: str, only: str | None = None) -> tuple[tuple[str, str], ...]:
    if phase not in _SOURCE_PHASE_STAGES:
        raise WorkflowError(f"Unknown parameter-source phase: {phase}")
    if only and phase not in {"esp", "bonded-qm"}:
        raise WorkflowError("--only is supported only for esp or bonded-qm phases.")
    return _SOURCE_PHASE_STAGES[phase][:1] if only else _SOURCE_PHASE_STAGES[phase]


# The CLI above runs in tbmb_resp/tbmb_qm, where BioMA itself need not be installed.
# ToolRegistry imports this module under a different name in the BioMA environment.
if __name__ != "__main__":
    from typing import Literal

    import yaml
    from pydantic import Field

    from bioma.tools.base import ToolInput, bioma_tool
    from bioma.tools.errors import ToolError

    class ParameterSourceToolInput(ToolInput):
        query_path: str = Field(description="Complete TBMB docking query YAML.")
        phase: Literal["status", "prepare", "esp", "bonded-qm", "fit", "finalize"] = Field(
            description="One resumable parameter-source phase; status is diagnostic only."
        )
        only: str | None = Field(default=None, description="Optional single QM job or term for esp/bonded-qm.")


    @bioma_tool(
        name="derive_tbmb_parameter_source",
        description="Run one audited, resumable TBMB-Met QM/RESP or bonded-parameter phase; never approve scientific results.",
        category="structure", version="1.0.0", author="BioMA",
        args_schema=ParameterSourceToolInput,
        tags=["TBMB", "RESP", "Psi4", "AmberTools", "parameterization"],
    )
    def derive_tbmb_parameter_source(query_path: str, phase: str, only: str | None = None) -> str:
        query = _path(query_path)
        if not query.is_file():
            raise ToolError(f"Query YAML not found: {query}", error_type="invalid_input", retriable=False,
                            backend="TBMB parameter source")
        config = yaml.safe_load(query.read_text(encoding="utf-8")) or {}
        source = config.get("parameter_source") or {}
        root_value = source.get("root")
        if not root_value:
            raise ToolError("parameter_source.root is required.", error_type="invalid_input", retriable=False,
                            backend="TBMB parameter source")
        root = Path(root_value).expanduser()
        root = root.resolve() if root.is_absolute() else (query.parent / root).resolve()
        manifest = root / "source_manifest.yaml"
        if phase == "status":
            result = status(argparse.Namespace(root=str(root)))
            return json.dumps({"status": "ok", "phase": phase, "result": result}, ensure_ascii=False)
        if source.get("mode") != "build":
            raise ToolError("Set parameter_source.mode: build for source derivation; reuse mode cannot run QM.",
                            error_type="invalid_input", retriable=False, backend="TBMB parameter source")
        if manifest.is_file():
            existing = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            if existing.get("approved") is True:
                raise ToolError("Source manifest is already approved; use a new versioned root.",
                                error_type="invalid_input", retriable=False, backend="TBMB parameter source")
            if phase == "finalize":
                raise ToolError("A draft source manifest already exists; do not overwrite reviewer edits.",
                                error_type="invalid_input", retriable=False, backend="TBMB parameter source")
        try:
            stages = _source_phase_plan(phase, only)
        except WorkflowError as exc:
            raise ToolError(str(exc), error_type="invalid_input", retriable=False,
                            backend="TBMB parameter source") from exc
        if not str(source.get("sequence", "")).strip():
            raise ToolError("parameter_source.sequence is required; no case sequence is assumed.",
                            error_type="invalid_input", retriable=False, backend="TBMB parameter source")
        environments = source.get("environments") or {}
        conda = str(source.get("conda_executable") or "conda")
        if Path(conda).is_absolute() and not Path(conda).is_file():
            raise ToolError(f"Conda executable not found: {conda}", error_type="invalid_input",
                            retriable=False, backend="TBMB parameter source")
        logs = []
        for stage, environment in stages:
            env_path = environments.get(environment)
            if not env_path or not Path(env_path).is_dir():
                raise ToolError(f"parameter_source.environments.{environment} must name an existing environment.",
                                error_type="invalid_input", retriable=False, backend="TBMB parameter source")
            command = [conda, "run", "--no-capture-output", "-p", str(env_path),
                       "python", str(Path(__file__).resolve()), stage, "--root", str(root),
                       "--sequence", str(source.get("sequence") or DEFAULT_SEQUENCE),
                       "--met-positions", *[str(value) for value in source.get("met_positions", DEFAULT_MET_POSITIONS)],
                       "--formal-charge", str(source.get("formal_charge", DEFAULT_FORMAL_CHARGE)),
                       "--conformers", str(source.get("conformers", 4))]
            if stage == "audit-inputs":
                for option, field in (("sdf", "ligand_ensemble_sdf"), ("metadata", "ligand_metadata"),
                                      ("reference-pdb", "ligand_reference_pdb")):
                    value = config.get(field)
                    if not value:
                        raise ToolError(f"Query is missing {field} for input audit.", error_type="invalid_input",
                                        retriable=False, backend="TBMB parameter source")
                    asset = Path(value).expanduser()
                    asset = asset.resolve() if asset.is_absolute() else (query.parent / asset).resolve()
                    if not asset.is_file():
                        raise ToolError(f"Input file not found: {asset}", error_type="invalid_input",
                                        retriable=False, backend="TBMB parameter source")
                    command.extend(["--" + option, str(asset)])
            if stage in {"run-psi4", "run-bonded-qm"}:
                command.extend(["--memory", str(source.get("memory", "16 GB"))])
                if source.get("resume", True):
                    command.append("--resume")
                if only:
                    command.extend(["--only", only])
            if stage == "run-bonded-qm":
                command.extend(["--threads", str(source.get("threads", 8)),
                                "--optimizer", str(source.get("optimizer", "geometric"))])
                if source.get("keep_going", False):
                    command.append("--keep-going")
            if stage == "collect-bonded-qm":
                for job_id in source.get("exclude_failed_jobs", []):
                    command.extend(["--exclude-failed-job", str(job_id)])
                if source.get("exclude_failed_jobs"):
                    command.extend(["--exclusion-reason", str(source.get("exclusion_reason") or ""),
                                    "--exclusion-recorded-by", str(source.get("exclusion_recorded_by") or "")])
            log = root / "raw" / "workflow_logs" / f"{phase}_{stage}.log"
            _mkdir(log.parent)
            print(f"[parameter-source] {stage}: {log}", flush=True)
            with log.open("w", encoding="utf-8") as handle:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True)
                assert process.stdout is not None
                for line in process.stdout:
                    handle.write(line)
                    handle.flush()
                    print(line, end="", flush=True)
                returncode = process.wait()
            logs.append({"stage": stage, "log": str(log), "exit_code": returncode})
            if returncode:
                raise ToolError(f"Parameter-source stage {stage} failed ({returncode}); see {log}",
                                error_type="unavailable", retriable=False, backend="TBMB parameter source")
        result = status(argparse.Namespace(root=str(root)))
        return json.dumps({"status": "ok", "phase": phase, "root": str(root),
                           "completed_stages": logs, "result": result}, ensure_ascii=False)


if __name__ == "__main__":
    main()
