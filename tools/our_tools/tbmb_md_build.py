#!/usr/bin/env python3
"""Build complete-receptor and explicit-solvent AMBER inputs for TBMB MD.

This tool consumes the final ranked docking records but does not trust their
incomplete receptor topology.  For each selected pose it:

1. aligns an approved complete receptor model to the relaxed receptor;
2. transfers the relaxed TBMB-peptide coordinates without rebuilding its graph;
3. rebuilds and locally minimizes a dry ff14SB/custom-parameter complex; and
4. creates a TIP3P truncated-octahedral box with neutralizing ions and NaCl.

The output is input preparation only.  It does not run NVT/NPT or production MD.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


AA3_TO_1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "CYX": "C", "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H",
    "HID": "H", "HIE": "H", "HIP": "H", "ILE": "I", "LEU": "L",
    "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P", "SER": "S",
    "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}
WATER_RESIDUES = {"WAT", "HOH", "TIP3"}


class MDBuildError(RuntimeError):
    """A deterministic input, chemistry, topology, or runtime failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise MDBuildError("PyYAML is required for YAML configuration/manifest files") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise MDBuildError(f"Expected one mapping in {path}")
    return data


def _resolve(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _resolve_artifact(value: str | Path, base: Path, aliases: dict[str, str]) -> Path:
    text = str(value)
    direct = Path(text).expanduser()
    if direct.is_file():
        return direct.resolve()
    for remote, local in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        prefix = remote.rstrip("/")
        if text == prefix or text.startswith(prefix + "/"):
            suffix = text[len(prefix):].lstrip("/")
            candidate = _resolve(local, base) / suffix
            if candidate.is_file():
                return candidate.resolve()
    candidate = _resolve(text, base)
    if candidate.is_file():
        return candidate
    raise MDBuildError(f"Cannot resolve artifact path: {value}")


@dataclass(frozen=True)
class PDBAtom:
    record: str
    serial: int
    atom_name: str
    altloc: str
    residue_name: str
    chain: str
    residue_number: int
    insertion_code: str
    xyz: tuple[float, float, float]
    occupancy: float
    bfactor: float
    element: str


def _read_pdb_atoms(path: Path, include_hydrogen: bool = True) -> list[PDBAtom]:
    atoms: list[PDBAtom] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        atom_name = line[12:16].strip()
        element = line[76:78].strip().upper()
        if not element:
            element = re.sub(r"[^A-Za-z]", "", atom_name)[:1].upper()
        if not include_hydrogen and element == "H":
            continue
        try:
            atoms.append(PDBAtom(
                record=line[:6].strip() or "ATOM",
                serial=int(line[6:11]), atom_name=atom_name,
                altloc=line[16:17].strip(), residue_name=line[17:20].strip().upper(),
                chain=line[21:22].strip(), residue_number=int(line[22:26]),
                insertion_code=line[26:27].strip(),
                xyz=(float(line[30:38]), float(line[38:46]), float(line[46:54])),
                occupancy=float(line[54:60] or 1.0), bfactor=float(line[60:66] or 0.0),
                element=element,
            ))
        except ValueError as exc:
            raise MDBuildError(f"Malformed PDB atom line in {path}: {line!r}") from exc
    if not atoms:
        raise MDBuildError(f"No atoms found in PDB: {path}")
    return atoms


def _format_pdb_atom(atom: PDBAtom, serial: int) -> str:
    record = "HETATM" if atom.record.upper().startswith("HET") else "ATOM  "
    altloc = atom.altloc[:1] if atom.altloc else " "
    chain = atom.chain[:1] if atom.chain else " "
    icode = atom.insertion_code[:1] if atom.insertion_code else " "
    return (
        f"{record}{serial:5d} {atom.atom_name:>4s}{altloc}{atom.residue_name:>3s} "
        f"{chain}{atom.residue_number:4d}{icode}   "
        f"{atom.xyz[0]:8.3f}{atom.xyz[1]:8.3f}{atom.xyz[2]:8.3f}"
        f"{atom.occupancy:6.2f}{atom.bfactor:6.2f}          {atom.element:>2s}"
    )


def _write_pdb(path: Path, atom_groups: Iterable[list[PDBAtom]]) -> None:
    lines: list[str] = []
    serial = 1
    for group in atom_groups:
        for atom in group:
            lines.append(_format_pdb_atom(atom, serial))
            serial += 1
        if group:
            last = group[-1]
            lines.append(
                f"TER   {serial:5d}      {last.residue_name:>3s} "
                f"{(last.chain or ' ')[:1]}{last.residue_number:4d}"
            )
            serial += 1
    lines.append("END")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _chain_groups(atoms: list[PDBAtom]) -> list[list[PDBAtom]]:
    """Split atoms at chain boundaries so PDB TER records are preserved."""
    groups: list[list[PDBAtom]] = []
    for atom in atoms:
        if not groups or atom.chain != groups[-1][-1].chain:
            groups.append([])
        groups[-1].append(atom)
    return groups


def _residue_order(atoms: Iterable[PDBAtom]) -> list[tuple[tuple[str, int, str], str]]:
    result: list[tuple[tuple[str, int, str], str]] = []
    seen: set[tuple[str, int, str]] = set()
    for atom in atoms:
        key = (atom.chain, atom.residue_number, atom.insertion_code)
        if key not in seen:
            result.append((key, atom.residue_name))
            seen.add(key)
    return result


def _protein_sequence(atoms: Iterable[PDBAtom]) -> tuple[str, list[tuple[str, int, str]]]:
    sequence: list[str] = []
    keys: list[tuple[str, int, str]] = []
    for key, residue_name in _residue_order(atoms):
        letter = AA3_TO_1.get(residue_name)
        if letter is None:
            raise MDBuildError(f"Unsupported protein residue {residue_name} at {key}")
        sequence.append(letter)
        keys.append(key)
    return "".join(sequence), keys


def _lcs_pairs(first: str, second: str) -> list[tuple[int, int]]:
    table = [[0] * (len(second) + 1) for _ in range(len(first) + 1)]
    for i, left in enumerate(first, 1):
        for j, right in enumerate(second, 1):
            table[i][j] = table[i - 1][j - 1] + 1 if left == right else max(table[i - 1][j], table[i][j - 1])
    pairs: list[tuple[int, int]] = []
    i, j = len(first), len(second)
    while i and j:
        if first[i - 1] == second[j - 1]:
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif table[i - 1][j] >= table[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return list(reversed(pairs))


def _ca_by_key(atoms: Iterable[PDBAtom]) -> dict[tuple[str, int, str], tuple[float, float, float]]:
    return {
        (atom.chain, atom.residue_number, atom.insertion_code): atom.xyz
        for atom in atoms if atom.atom_name == "CA"
    }


def _kabsch_transform(
    source: list[tuple[float, float, float]], target: list[tuple[float, float, float]],
) -> tuple[Any, Any, float]:
    if len(source) != len(target) or len(source) < 3:
        raise MDBuildError("At least three paired coordinates are required for receptor alignment")
    try:
        import numpy as np
    except ImportError as exc:
        raise MDBuildError("NumPy is required for complete-receptor coordinate alignment") from exc
    moving = np.asarray(source, dtype=float); fixed = np.asarray(target, dtype=float)
    moving_center = moving.mean(axis=0); fixed_center = fixed.mean(axis=0)
    covariance = (moving - moving_center).T @ (fixed - fixed_center)
    left, _, right_t = np.linalg.svd(covariance)
    rotation = right_t.T @ left.T
    if np.linalg.det(rotation) < 0:
        right_t[-1, :] *= -1
        rotation = right_t.T @ left.T
    translation = fixed_center - moving_center @ rotation.T
    fitted = moving @ rotation.T + translation
    rmsd = float(np.sqrt(np.mean(np.sum((fitted - fixed) ** 2, axis=1))))
    return rotation, translation, rmsd


def _transform_xyz(xyz: tuple[float, float, float], rotation: Any, translation: Any) -> tuple[float, float, float]:
    try:
        import numpy as np
    except ImportError as exc:
        raise MDBuildError("NumPy is required for complete-receptor coordinate alignment") from exc
    result = np.asarray(xyz, dtype=float) @ rotation.T + translation
    return tuple(float(value) for value in result)


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def _prmtop_values(path: Path, flag: str) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    marker = f"%FLAG {flag}"
    try:
        index = next(i for i, line in enumerate(lines) if line.strip() == marker)
    except StopIteration as exc:
        raise MDBuildError(f"{flag} is absent from AMBER topology: {path}") from exc
    values: list[str] = []
    for line in lines[index + 2:]:
        if line.startswith("%FLAG"):
            break
        values.extend(line.split())
    return values


def _prmtop_natom(path: Path) -> int:
    values = _prmtop_values(path, "POINTERS")
    if not values:
        raise MDBuildError(f"Empty POINTERS section: {path}")
    return int(values[0])


def _prmtop_charge(path: Path) -> float:
    return sum(float(value.replace("D", "E")) for value in _prmtop_values(path, "CHARGE")) / 18.2223


def _has_periodic_box(path: Path) -> bool:
    return "%FLAG BOX_DIMENSIONS" in path.read_text(encoding="utf-8", errors="replace")


def _tleap_failures(log_text: str) -> list[str]:
    failures: list[str] = []
    patterns = [
        r"FATAL", r"Could not find bond parameter", r"Could not find angle parameter",
        r"No torsion terms", r"Unknown residue", r"not found in database",
        r"Argument #\d+ is of type", r"Errors\s*=\s*[1-9]",
        r"There is a bond of\s+([3-9]|[1-9][0-9])(?:\.\d+)?\s+angstroms",
    ]
    for line in log_text.splitlines():
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
            failures.append(line.strip())
    return failures


def _run_tleap(executable: str, input_path: Path, log_path: Path) -> None:
    command = [executable, "-f", str(input_path)]
    completed = subprocess.run(command, cwd=input_path.parent, text=True, capture_output=True, check=False)
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    failures = _tleap_failures(log_path.read_text(encoding="utf-8", errors="replace"))
    if completed.returncode:
        detail = "; ".join(failures[:6]) if failures else "no recognized diagnostic line"
        raise MDBuildError(
            f"TLEaP failed with exit code {completed.returncode}: {detail}; inspect {log_path}"
        )
    if failures:
        raise MDBuildError(f"TLEaP topology audit failed: {'; '.join(failures[:6])}; inspect {log_path}")


def _parameter_bundle(config: dict[str, Any], base: Path, output: Path) -> dict[str, Any]:
    section = config["parameters"]
    source_path = _resolve(section["source_manifest"], base)
    status_path = _resolve(section["validation_status"], base)
    source = _load_mapping(source_path)
    status = _load_mapping(status_path)
    attestation = section.get("approval_attestation") or {}
    failures: list[str] = []
    if source.get("approved") is not True:
        failures.append("source manifest is not approved")
    if source.get("standard_residue_forcefield") != "ff14SB":
        failures.append("source manifest must declare standard_residue_forcefield=ff14SB")
    if status.get("source_approved") is not True:
        failures.append("validation status does not record source_approved=true")
    failed_stages = [name for name, record in (status.get("stages") or {}).items() if record.get("passed") is not True]
    if failed_stages:
        failures.append("failed validation stages: " + ", ".join(failed_stages))
    if (source.get("charge_derivation", {}).get("boundary_reconciliation", {}).get("reviewed") is not True):
        failures.append("charge boundary reconciliation is not reviewed")
    if source.get("bonded_parameter_derivation", {}).get("reviewed") is not True:
        failures.append("bonded parameters are not reviewed")
    if source.get("candidate_generation", {}).get("technical_assembly_passed") is not True:
        failures.append("reference TLEaP assembly has not passed")
    if attestation.get("production_md_confirmed") is not True or not attestation.get("reviewed_by"):
        failures.append("configuration lacks an explicit production_md approval attestation")
    if failures:
        raise MDBuildError("Parameter production gate failed: " + "; ".join(failures))

    templates: list[dict[str, Any]] = []
    frcmods: list[dict[str, Any]] = []
    for record in source.get("files", {}).get("residue_templates", []):
        path = _resolve(record["path"], source_path.parent)
        observed = _sha256(path)
        if observed != record["sha256"]:
            raise MDBuildError(f"Parameter template hash mismatch: {path}")
        templates.append({**record, "path": str(path), "observed_sha256": observed})
    for record in source.get("files", {}).get("frcmod", []):
        path = _resolve(record["path"], source_path.parent)
        observed = _sha256(path)
        if observed != record["sha256"]:
            raise MDBuildError(f"FRCMOD hash mismatch: {path}")
        frcmods.append({**record, "path": str(path), "observed_sha256": observed})
    if {item["residue_name"] for item in templates} != {"MSU", "CMU", "TMB"}:
        raise MDBuildError("Parameter bundle must provide exactly MSU, CMU, and TMB templates")

    runtime = {
        "schema_version": 1, "created_at_utc": _utc_now(),
        "source_manifest": str(source_path), "source_manifest_sha256": _sha256(source_path),
        "validation_status": str(status_path), "validation_status_sha256": _sha256(status_path),
        "approved": True, "reviewed_by": attestation["reviewed_by"],
        "allowed_uses": ["restrained_minimization", "equilibration", "production_md", "mm_gbsa"],
        "standard_residue_forcefield": source.get("standard_residue_forcefield"),
        "templates": templates, "frcmods": frcmods, "scaffold": source.get("scaffold"),
        "notice": "Runtime promotion records an explicit user attestation; it does not create new QM evidence.",
    }
    runtime_path = output / "parameter_runtime_manifest.json"
    _write_json(runtime_path, runtime)
    runtime["runtime_manifest"] = str(runtime_path)
    runtime["runtime_manifest_sha256"] = _sha256(runtime_path)
    return runtime


def _tleap_preamble(bundle: dict[str, Any], include_water: bool = False) -> list[str]:
    lines = ["source leaprc.protein.ff14SB", "source leaprc.gaff2"]
    if include_water:
        lines.append("source leaprc.water.tip3p")
    lines.append("set default PBRadii mbondi3")
    lines.extend(f'loadOff "{item["path"]}"' for item in bundle["templates"])
    lines.extend(f'loadAmberParams "{item["path"]}"' for item in bundle["frcmods"])
    return lines


def _crosslink_records(config: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    records = config["ligand"].get("crosslinks") or []
    if not records:
        records = (bundle.get("scaffold") or {}).get("crosslinks") or []
    normalized: list[dict[str, Any]] = []
    for record in records:
        normalized.append({
            "peptide_residue_index": int(record["peptide_residue_index"]),
            "methionine_atom": str(record.get("methionine_atom", "SD")),
            "linker_residue_index": int(record["linker_residue_index"]),
            "linker_atom": str(record["linker_atom"]),
        })
    if len(normalized) != 3:
        raise MDBuildError("Exactly three TBMB-Met crosslinks are required")
    return normalized


def _receptor_for_pose(
    config: dict[str, Any], base: Path, relaxed_receptor_atoms: list[PDBAtom],
) -> tuple[list[PDBAtom], dict[str, Any]]:
    section = config["receptor"]
    model_path = _resolve(section["selected_model"], base)
    model_atoms = _read_pdb_atoms(model_path, include_hydrogen=False)
    selected_chain = str(section.get("chain_id", "A"))
    model_atoms = [atom for atom in model_atoms if atom.chain == selected_chain]
    if not model_atoms:
        raise MDBuildError(f"Selected receptor chain {selected_chain} is absent from {model_path}")
    model_sequence, model_keys = _protein_sequence(model_atoms)
    relaxed_sequence, relaxed_keys = _protein_sequence(relaxed_receptor_atoms)
    start = int(section["construct_start"]); end = int(section["construct_end"])
    if len(model_sequence) != end - start + 1:
        raise MDBuildError(
            f"Complete receptor has {len(model_sequence)} residues; expected {end - start + 1} for {start}-{end}"
        )
    pairs = _lcs_pairs(model_sequence, relaxed_sequence)
    minimum_matches = int(section.get("minimum_common_residues", 100))
    if len(pairs) < minimum_matches:
        raise MDBuildError(f"Only {len(pairs)} receptor residues map to relaxed coordinates")
    model_ca = _ca_by_key(model_atoms); relaxed_ca = _ca_by_key(relaxed_receptor_atoms)
    source = [model_ca[model_keys[left]] for left, _ in pairs]
    target = [relaxed_ca[relaxed_keys[right]] for _, right in pairs]
    rotation, translation, rmsd = _kabsch_transform(source, target)
    if rmsd > float(section.get("maximum_alignment_rmsd_angstrom", 1.0)):
        raise MDBuildError(f"Complete-to-relaxed receptor alignment RMSD is too high: {rmsd:.3f} A")

    histidines = {int(item["residue"]): str(item["state"]).upper() for item in section.get("histidines", [])}
    if any(state not in {"HID", "HIE", "HIP"} for state in histidines.values()):
        raise MDBuildError("Histidine states must be HID, HIE, or HIP")
    disulfide_numbers = {
        int(value) for pair in section.get("disulfides", []) for value in (pair["first"], pair["second"])
    }
    model_to_relaxed = {model_keys[left]: relaxed_keys[right] for left, right in pairs}
    relaxed_atom_map = {
        ((atom.chain, atom.residue_number, atom.insertion_code), atom.atom_name): atom.xyz
        for atom in relaxed_receptor_atoms if atom.element != "H"
    }
    preservation_ranges = [
        (int(item[0]), int(item[1])) for item in section.get("complete_model_preservation_ranges", [])
    ]
    transfer_common_residues = bool(section.get("transfer_common_residue_coordinates_from_relaxed_receptor", True))
    transferred_common_atoms = 0
    transformed: list[PDBAtom] = []
    for atom in model_atoms:
        residue_name = atom.residue_name
        if atom.residue_number in histidines:
            residue_name = histidines[atom.residue_number]
        if atom.residue_number in disulfide_numbers:
            if AA3_TO_1.get(residue_name) != "C":
                raise MDBuildError(f"Configured disulfide residue is not Cys: {atom.residue_number}")
            residue_name = "CYX"
        transformed_xyz = _transform_xyz(atom.xyz, rotation, translation)
        in_preservation_range = any(first <= atom.residue_number <= last for first, last in preservation_ranges)
        relaxed_key = model_to_relaxed.get((atom.chain, atom.residue_number, atom.insertion_code))
        relaxed_xyz = relaxed_atom_map.get((relaxed_key, atom.atom_name)) if relaxed_key else None
        if (
            transfer_common_residues and not in_preservation_range and relaxed_xyz is not None
        ):
            transformed_xyz = relaxed_xyz
            transferred_common_atoms += 1
        transformed.append(replace(
            atom, record="ATOM", residue_name=residue_name, chain="A",
            xyz=transformed_xyz, occupancy=1.0, bfactor=0.0,
        ))
    return transformed, {
        "selected_model": str(model_path), "selected_model_sha256": _sha256(model_path),
        "complete_sequence": model_sequence, "complete_residue_count": len(model_sequence),
        "relaxed_sequence": relaxed_sequence, "relaxed_residue_count": len(relaxed_sequence),
        "mapped_residue_count": len(pairs), "alignment_ca_rmsd_angstrom": rmsd,
        "transferred_common_heavy_atoms": transferred_common_atoms,
        "complete_model_preservation_ranges": preservation_ranges,
        "histidines": histidines, "disulfides": section.get("disulfides", []),
    }


def _ligand_from_relaxed_complex(
    relaxed_atoms: list[PDBAtom], ligand_template: Path, ligand_natom: int,
) -> tuple[list[PDBAtom], int]:
    if len(relaxed_atoms) <= ligand_natom:
        raise MDBuildError("Relaxed complex contains too few atoms to split receptor and ligand")
    final_ligand = relaxed_atoms[-ligand_natom:]
    final_heavy = [atom for atom in final_ligand if atom.element != "H"]
    template = _read_pdb_atoms(ligand_template, include_hydrogen=False)
    if len(final_heavy) != len(template):
        raise MDBuildError(
            f"Relaxed/template ligand heavy-atom counts differ: {len(final_heavy)} vs {len(template)}"
        )
    template_residues = _residue_order(template)
    final_residues = _residue_order(final_heavy)
    if len(template_residues) != len(final_residues):
        raise MDBuildError(
            f"Relaxed/template ligand residue counts differ: {len(final_residues)} vs {len(template_residues)}"
        )
    final_residue_index = {key: index for index, (key, _) in enumerate(final_residues)}
    template_residue_index = {key: index for index, (key, _) in enumerate(template_residues)}
    final_coordinates: dict[tuple[int, str], tuple[float, float, float]] = {}
    for atom in final_heavy:
        key = (atom.chain, atom.residue_number, atom.insertion_code)
        lookup = (final_residue_index[key], atom.atom_name)
        if lookup in final_coordinates:
            raise MDBuildError(f"Duplicate ligand atom name within one relaxed residue: {lookup}")
        final_coordinates[lookup] = atom.xyz
    for index, ((_, template_name), (_, final_name)) in enumerate(zip(template_residues, final_residues), 1):
        if template_name != final_name:
            raise MDBuildError(
                f"Ligand residue-order mismatch at residue {index}: {template_name} vs {final_name}"
            )
    output: list[PDBAtom] = []
    for reference in template:
        key = (reference.chain, reference.residue_number, reference.insertion_code)
        lookup = (template_residue_index[key], reference.atom_name)
        if lookup not in final_coordinates:
            raise MDBuildError(
                f"Relaxed ligand is missing {reference.residue_name}:{reference.atom_name} at relative residue {lookup[0] + 1}"
            )
        output.append(replace(reference, xyz=final_coordinates[lookup], occupancy=1.0, bfactor=0.0))
    return output, len(relaxed_atoms) - ligand_natom


def _minimum_distance(first: Iterable[PDBAtom], second: Iterable[PDBAtom]) -> float:
    left = [atom for atom in first if atom.element != "H"]
    right = [atom for atom in second if atom.element != "H"]
    return min(_distance(a.xyz, b.xyz) for a in left for b in right)


def _receptor_covalent_geometry(
    atoms: list[PDBAtom], start: int, end: int, disulfides: list[dict[str, Any]],
) -> dict[str, Any]:
    coordinates: dict[int, dict[str, tuple[float, float, float]]] = {}
    for atom in atoms:
        coordinates.setdefault(atom.residue_number, {})[atom.atom_name] = atom.xyz
    peptide_bonds: list[dict[str, Any]] = []
    for residue in range(start, end):
        try:
            value = _distance(coordinates[residue]["C"], coordinates[residue + 1]["N"])
        except KeyError as exc:
            raise MDBuildError(f"Missing backbone atom around receptor residues {residue}-{residue + 1}") from exc
        peptide_bonds.append({"first": residue, "second": residue + 1, "distance_angstrom": value})
    abnormal = [
        item for item in peptide_bonds
        if item["distance_angstrom"] < 1.0 or item["distance_angstrom"] > 2.0
    ]
    disulfide_geometry: list[dict[str, Any]] = []
    for pair in disulfides:
        first, second = int(pair["first"]), int(pair["second"])
        try:
            value = _distance(coordinates[first]["SG"], coordinates[second]["SG"])
        except KeyError as exc:
            raise MDBuildError(f"Missing SG atom for disulfide {first}-{second}") from exc
        disulfide_geometry.append({"first": first, "second": second, "distance_angstrom": value})
        if not 1.7 <= value <= 2.3:
            raise MDBuildError(f"Disulfide {first}-{second} has invalid pre-TLEaP distance {value:.3f} A")
    if abnormal:
        raise MDBuildError(
            "Complete-receptor transfer created abnormal peptide bonds: "
            + ", ".join(
                f'{item["first"]}-{item["second"]}={item["distance_angstrom"]:.3f}A'
                for item in abnormal[:8]
            )
        )
    return {
        "peptide_bond_count": len(peptide_bonds),
        "minimum_peptide_bond_angstrom": min(item["distance_angstrom"] for item in peptide_bonds),
        "maximum_peptide_bond_angstrom": max(item["distance_angstrom"] for item in peptide_bonds),
        "abnormal_peptide_bonds": abnormal, "disulfides": disulfide_geometry,
    }


def _residue_index_by_number(atoms: list[PDBAtom]) -> dict[int, int]:
    result: dict[int, int] = {}
    index = 0
    previous: tuple[str, int, str] | None = None
    for atom in atoms:
        key = (atom.chain, atom.residue_number, atom.insertion_code)
        if key != previous:
            index += 1
            result[atom.residue_number] = index
            previous = key
    return result


def _write_tleap_sequential_pdb(source: Path, output: Path) -> dict[str, Any]:
    """Write a heavy-atom TLEaP PDB with unique sequential residue numbers.

    The human-review PDB keeps biological receptor numbering (32--145) and
    ligand numbering (1--18).  Loading that combined file into TLEaP makes
    atom selection ambiguous because the residue-number ranges overlap.  The
    OpenMM output also contains hydrogens whose names can conflict with LEaP's
    terminal, histidine, and custom-residue templates.  This copy therefore
    retains the relaxed heavy-atom coordinates, removes hydrogens, and changes
    residue numbers only.  LEaP then rebuilds hydrogens consistently from the
    already selected residue templates.
    """
    atoms = _read_pdb_atoms(source, include_hydrogen=False)
    residues = _residue_order(atoms)
    residue_numbers = {key: index for index, (key, _) in enumerate(residues, 1)}
    renumbered = [
        replace(
            atom,
            residue_number=residue_numbers[(atom.chain, atom.residue_number, atom.insertion_code)],
            insertion_code="",
        )
        for atom in atoms
    ]
    _write_pdb(output, _chain_groups(renumbered))
    mapping = [
        {
            "tleap_residue_number": index,
            "source_chain": key[0],
            "source_residue_number": key[1],
            "source_insertion_code": key[2],
            "residue_name": residue_name,
        }
        for index, (key, residue_name) in enumerate(residues, 1)
    ]
    return {
        "source_pdb": str(source),
        "source_pdb_sha256": _sha256(source),
        "tleap_pdb": str(output),
        "tleap_pdb_sha256": _sha256(output),
        "residue_count": len(residues),
        "hydrogen_policy": "strip_and_rebuild_with_tleap_templates",
        "residue_mapping": mapping,
    }


def _dry_tleap_text(
    config: dict[str, Any], bundle: dict[str, Any], receptor_pdb: Path, ligand_pdb: Path,
    prmtop: Path, inpcrd: Path, output_pdb: Path, receptor_atoms: list[PDBAtom],
) -> str:
    lines = _tleap_preamble(bundle)
    lines.extend([f'receptor = loadPDB "{receptor_pdb}"'])
    receptor_residue_numbers = {atom.residue_number for atom in receptor_atoms}
    for pair in config["receptor"].get("disulfides", []):
        first = int(pair["first"]); second = int(pair["second"])
        if first not in receptor_residue_numbers or second not in receptor_residue_numbers:
            raise MDBuildError(
                f"Configured disulfide {first}-{second} is absent from the receptor PDB numbering"
            )
        lines.append(f"bond receptor.{first}.SG receptor.{second}.SG")
    lines.append(f'ligand = loadPDB "{ligand_pdb}"')
    for record in _crosslink_records(config, bundle):
        lines.append(
            f'bond ligand.{record["peptide_residue_index"]}.{record["methionine_atom"]} '
            f'ligand.{record["linker_residue_index"]}.{record["linker_atom"]}'
        )
    lines.extend([
        "check receptor", "check ligand", "complex = combine { receptor ligand }", "check complex",
        f'saveAmberParm complex "{prmtop}" "{inpcrd}"', f'savePdb complex "{output_pdb}"', "quit",
    ])
    return "\n".join(lines) + "\n"


def _local_relax(
    config: dict[str, Any], prmtop_path: Path, inpcrd_path: Path, output_pdb: Path,
    receptor_residue_count: int,
) -> dict[str, Any]:
    try:
        from openmm import CustomExternalForce, LangevinMiddleIntegrator, Platform, unit
        from openmm.app import AmberInpcrdFile, AmberPrmtopFile, GBn2, HBonds, NoCutoff, PDBFile, Simulation
    except ImportError as exc:
        raise MDBuildError("OpenMM with AMBER file support is required for local relaxation") from exc
    section = config["local_relaxation"]
    prmtop = AmberPrmtopFile(str(prmtop_path)); inpcrd = AmberInpcrdFile(str(inpcrd_path))
    system = prmtop.createSystem(
        nonbondedMethod=NoCutoff, constraints=HBonds, implicitSolvent=GBn2,
        implicitSolventSaltConc=float(section.get("salt_molar", 0.15)) * unit.moles / unit.liter,
    )
    force = CustomExternalForce("0.5*k*((x-x0)^2+(y-y0)^2+(z-z0)^2)")
    force.addGlobalParameter("k", 0.0)
    for name in ("x0", "y0", "z0"):
        force.addPerParticleParameter(name)
    backbone_names = {"N", "CA", "C", "O"}
    restrained = 0
    for atom, position in zip(prmtop.topology.atoms(), inpcrd.positions):
        if atom.residue.index < receptor_residue_count and atom.name in backbone_names:
            xyz = position.value_in_unit(unit.nanometer)
            force.addParticle(atom.index, [xyz.x, xyz.y, xyz.z]); restrained += 1
    system.addForce(force)
    integrator = LangevinMiddleIntegrator(
        float(section.get("temperature_kelvin", 298.15)) * unit.kelvin,
        1.0 / unit.picosecond, 0.002 * unit.picoseconds,
    )
    platform_name = str(section.get("platform", "")).strip()
    properties = {str(k): str(v) for k, v in (section.get("platform_properties") or {}).items()}
    if platform_name:
        simulation = Simulation(prmtop.topology, system, integrator, Platform.getPlatformByName(platform_name), properties)
    else:
        simulation = Simulation(prmtop.topology, system, integrator)
    simulation.context.setPositions(inpcrd.positions)
    initial = simulation.context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
    stages = section.get("stages") or [
        {"restraint_kcal_mol_a2": 10.0, "max_iterations": 5000},
        {"restraint_kcal_mol_a2": 2.0, "max_iterations": 5000},
        {"restraint_kcal_mol_a2": 0.5, "max_iterations": 5000},
    ]
    records: list[dict[str, Any]] = []
    tolerance = float(section.get("force_tolerance_kj_mol_nm", 10.0))
    for stage in stages:
        restraint = float(stage["restraint_kcal_mol_a2"])
        simulation.context.setParameter("k", restraint * 418.4)
        simulation.minimizeEnergy(
            tolerance=tolerance * unit.kilojoule_per_mole / unit.nanometer,
            maxIterations=int(stage.get("max_iterations", 5000)),
        )
        energy = simulation.context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        records.append({"restraint_kcal_mol_a2": restraint, "potential_energy_kj_mol": energy})
    final_state = simulation.context.getState(getEnergy=True, getPositions=True)
    final_energy = final_state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
    with output_pdb.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(prmtop.topology, final_state.getPositions(), handle, keepIds=True)
    if not math.isfinite(initial) or not math.isfinite(final_energy):
        raise MDBuildError("Local relaxation produced non-finite energy")
    relaxed_atoms = _read_pdb_atoms(output_pdb, include_hydrogen=False)
    residue_keys = [key for key, _ in _residue_order(relaxed_atoms)]
    receptor_keys = set(residue_keys[:receptor_residue_count])
    relaxed_receptor = [
        atom for atom in relaxed_atoms
        if (atom.chain, atom.residue_number, atom.insertion_code) in receptor_keys
    ]
    relaxed_ligand = [
        atom for atom in relaxed_atoms
        if (atom.chain, atom.residue_number, atom.insertion_code) not in receptor_keys
    ]
    minimum_interface_distance = _minimum_distance(relaxed_receptor, relaxed_ligand)
    required_distance = float(section.get("post_relax_minimum_heavy_atom_distance_angstrom", 1.5))
    if minimum_interface_distance < required_distance:
        raise MDBuildError(
            f"Local relaxation left a receptor-ligand heavy-atom clash: "
            f"{minimum_interface_distance:.3f} A < {required_distance:.3f} A"
        )
    return {
        "platform": simulation.context.getPlatform().getName(), "restrained_backbone_atoms": restrained,
        "initial_energy_kj_mol": initial, "final_energy_kj_mol": final_energy,
        "energy_decreased": final_energy < initial, "stages": records,
        "minimum_receptor_ligand_distance_angstrom": minimum_interface_distance,
        "relaxed_pdb": str(output_pdb), "relaxed_pdb_sha256": _sha256(output_pdb),
    }


def _combined_bond_lines(
    config: dict[str, Any], bundle: dict[str, Any], receptor_residue_count: int,
    receptor_atoms: list[PDBAtom], unit: str = "system",
) -> list[str]:
    indices = _residue_index_by_number(receptor_atoms)
    lines: list[str] = []
    for pair in config["receptor"].get("disulfides", []):
        lines.append(f'bond {unit}.{indices[int(pair["first"])]}.SG {unit}.{indices[int(pair["second"])]}.SG')
    for record in _crosslink_records(config, bundle):
        peptide = receptor_residue_count + record["peptide_residue_index"]
        linker = receptor_residue_count + record["linker_residue_index"]
        lines.append(
            f'bond {unit}.{peptide}.{record["methionine_atom"]} '
            f'{unit}.{linker}.{record["linker_atom"]}'
        )
    return lines


def _normalize_relaxed_complex_pdb(
    raw_pdb: Path, output_pdb: Path, receptor_start: int,
    receptor_residue_count: int, ligand_residue_count: int,
    receptor_residue_names: list[str], ligand_residue_names: list[str],
) -> dict[str, Any]:
    """Restore reviewed residue identities and chains after OpenMM output.

    AMBER/OpenMM PDB output can normalize CYX/HID/HIE or custom-residue labels.
    Reusing those labels in a new LEaP load would change protonation and atom
    types.  Residue names are therefore restored from the reviewed inputs while
    coordinates are taken from the relaxed structure.
    """
    atoms = _read_pdb_atoms(raw_pdb, include_hydrogen=True)
    residues = _residue_order(atoms)
    expected = receptor_residue_count + ligand_residue_count
    if len(residues) != expected:
        raise MDBuildError(
            f"Relaxed complex contains {len(residues)} residues; expected {expected}"
        )
    if len(receptor_residue_names) != receptor_residue_count:
        raise MDBuildError(
            "Reviewed receptor residue-name count does not match the relaxed receptor"
        )
    if len(ligand_residue_names) != ligand_residue_count:
        raise MDBuildError(
            "Reviewed ligand residue-name count does not match the relaxed ligand"
        )
    residue_index = {key: index for index, (key, _) in enumerate(residues)}
    receptor_atoms: list[PDBAtom] = []
    peptide_atoms: list[PDBAtom] = []
    linker_atoms: list[PDBAtom] = []
    for atom in atoms:
        old_key = (atom.chain, atom.residue_number, atom.insertion_code)
        index = residue_index[old_key]
        if index < receptor_residue_count:
            receptor_atoms.append(replace(
                atom, residue_name=receptor_residue_names[index],
                chain="A", residue_number=receptor_start + index, insertion_code="",
            ))
        else:
            ligand_index = index - receptor_residue_count + 1
            is_linker = ligand_index == ligand_residue_count
            target = linker_atoms if is_linker else peptide_atoms
            target.append(replace(
                atom, residue_name=ligand_residue_names[ligand_index - 1],
                chain="C" if is_linker else "B",
                residue_number=ligand_index, insertion_code="",
            ))
    _write_pdb(output_pdb, [receptor_atoms, peptide_atoms, linker_atoms])
    return {
        "receptor_residue_count": receptor_residue_count,
        "ligand_residue_count": ligand_residue_count,
        "receptor_chain": "A", "peptide_chain": "B", "linker_chain": "C",
        "residue_name_policy": "restore_from_reviewed_receptor_and_ligand_inputs",
        "normalized_pdb": str(output_pdb), "normalized_pdb_sha256": _sha256(output_pdb),
    }


def _combined_dry_tleap_text(
    config: dict[str, Any], bundle: dict[str, Any], relaxed_pdb: Path,
    receptor_atoms: list[PDBAtom], prmtop: Path, inpcrd: Path, output_pdb: Path,
) -> str:
    receptor_residue_count = len(_residue_order(receptor_atoms))
    lines = _tleap_preamble(bundle)
    lines.append(f'system = loadPDB "{relaxed_pdb}"')
    lines.extend(_combined_bond_lines(config, bundle, receptor_residue_count, receptor_atoms))
    lines.extend([
        "check system", f'saveAmberParm system "{prmtop}" "{inpcrd}"',
        f'savePdb system "{output_pdb}"', "quit",
    ])
    return "\n".join(lines) + "\n"


def _solvate_tleap_text(
    config: dict[str, Any], bundle: dict[str, Any], relaxed_pdb: Path,
    receptor_atoms: list[PDBAtom], output_pdb: Path, na_count: int | None = None,
    cl_count: int | None = None, prmtop: Path | None = None, inpcrd: Path | None = None,
) -> str:
    receptor_residue_count = len(_residue_order(receptor_atoms))
    lines = _tleap_preamble(bundle, include_water=True)
    lines.append(f'system = loadPDB "{relaxed_pdb}"')
    lines.extend(_combined_bond_lines(config, bundle, receptor_residue_count, receptor_atoms))
    lines.append("check system")
    padding = float(config["solvation"].get("padding_angstrom", 12.0))
    lines.append(f"solvateOct system TIP3PBOX {padding:.3f}")
    if na_count is not None and cl_count is not None:
        if na_count or cl_count:
            lines.append(f"addIonsRand system Na+ {na_count} Cl- {cl_count}")
        lines.append("check system")
    if prmtop is not None and inpcrd is not None:
        lines.append(f'saveAmberParm system "{prmtop}" "{inpcrd}"')
    lines.extend([f'savePdb system "{output_pdb}"', "quit"])
    return "\n".join(lines) + "\n"


def _count_residues(path: Path) -> dict[str, int]:
    atoms = _read_pdb_atoms(path)
    counts: dict[str, int] = {}
    ordinary_seen: set[tuple[str, int, str]] = set()
    for atom in atoms:
        if atom.residue_name in WATER_RESIDUES:
            if atom.atom_name in {"O", "OW"}:
                counts[atom.residue_name] = counts.get(atom.residue_name, 0) + 1
            continue
        if atom.residue_name.upper() in {"NA+", "NA", "CL-", "CL"}:
            counts[atom.residue_name] = counts.get(atom.residue_name, 0) + 1
            continue
        key = (atom.chain, atom.residue_number, atom.insertion_code)
        if key not in ordinary_seen:
            counts[atom.residue_name] = counts.get(atom.residue_name, 0) + 1
            ordinary_seen.add(key)
    return counts


def _preflight(config: dict[str, Any], base: Path) -> dict[str, Any]:
    output = _resolve(config["output_dir"], base)
    output.mkdir(parents=True, exist_ok=True)
    bundle = _parameter_bundle(config, base, output)
    receptor = config["receptor"]
    if (receptor.get("approval_attestation") or {}).get("human_review_confirmed") is not True:
        raise MDBuildError("Selected receptor lacks human_review_confirmed=true")
    model = _resolve(receptor["selected_model"], base)
    ranked = _resolve(config["candidates"]["ranked_json"], base)
    if int(config["ligand"].get("formal_charge", 0)) != 5:
        raise MDBuildError("The authoritative TBMB peptide charge must be +5")
    solvation = config["solvation"]
    if str(solvation.get("water_model", "")).upper() != "TIP3P":
        raise MDBuildError("This validated build requires TIP3P water")
    if str(solvation.get("box_shape", "")).lower() != "truncated_octahedron":
        raise MDBuildError("This validated build requires a truncated-octahedral box")
    if float(solvation.get("padding_angstrom", 0.0)) < 12.0:
        raise MDBuildError("Explicit solvent padding must be at least 12.0 A")
    if str(solvation.get("salt", "")).upper() != "NACL":
        raise MDBuildError("This validated PBS proxy requires NaCl")
    if abs(float(solvation.get("salt_molar", 0.0)) - 0.15) > 1e-9:
        raise MDBuildError("This validated PBS proxy requires 0.15 M NaCl")
    tleap = str((config.get("commands") or {}).get("tleap", "tleap"))
    resolved_tleap = shutil.which(tleap) or (tleap if Path(tleap).is_file() else None)
    if not resolved_tleap:
        raise MDBuildError(f"TLEaP executable not found: {tleap}")
    try:
        import openmm
    except ImportError as exc:
        raise MDBuildError("OpenMM is unavailable in the selected Python environment") from exc
    result = {
        "status": "pass", "created_at_utc": _utc_now(), "output_dir": str(output),
        "selected_model": str(model), "selected_model_sha256": _sha256(model),
        "ranked_json": str(ranked), "ranked_json_sha256": _sha256(ranked),
        "parameter_runtime_manifest": bundle["runtime_manifest"],
        "tleap": resolved_tleap, "openmm_version": getattr(openmm, "__version__", "unknown"),
    }
    _write_json(output / "preflight.json", result)
    return result


def _ranked_records(config: dict[str, Any], base: Path) -> list[dict[str, Any]]:
    ranked_path = _resolve(config["candidates"]["ranked_json"], base)
    data = json.loads(ranked_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise MDBuildError("ranked_json must contain a list")
    count = int(config["candidates"].get("top_n", 3))
    if len(data) < count:
        raise MDBuildError(f"Only {len(data)} ranked candidates are available; expected {count}")
    return data[:count]


def reconstruct(config: dict[str, Any], base: Path) -> dict[str, Any]:
    output = _resolve(config["output_dir"], base); output.mkdir(parents=True, exist_ok=True)
    bundle = _parameter_bundle(config, base, output)
    aliases = {str(k): str(v) for k, v in (config["candidates"].get("path_aliases") or {}).items()}
    tleap = str((config.get("commands") or {}).get("tleap", "tleap"))
    tleap_executable = shutil.which(tleap) or (tleap if Path(tleap).is_file() else None)
    if not tleap_executable:
        raise MDBuildError(f"TLEaP executable not found: {tleap}")
    results: list[dict[str, Any]] = []
    for rank, record in enumerate(_ranked_records(config, base), 1):
        job = output / f"rank_{rank:02d}"; job.mkdir(parents=True, exist_ok=True)
        relaxed_path = _resolve_artifact(record["recommended_complex_pose"], base, aliases)
        assembly = record.get("sequence_specific_amber_assembly") or {}
        ligand_template = _resolve_artifact(assembly["ligand_pdb"], base, aliases)
        ligand_prmtop = _resolve_artifact(assembly["ligand_prmtop"], base, aliases)
        relaxed_atoms = _read_pdb_atoms(relaxed_path, include_hydrogen=True)
        ligand_natom = _prmtop_natom(ligand_prmtop)
        ligand_atoms, receptor_natom = _ligand_from_relaxed_complex(relaxed_atoms, ligand_template, ligand_natom)
        relaxed_receptor_atoms = relaxed_atoms[:receptor_natom]
        complete_receptor, receptor_audit = _receptor_for_pose(config, base, relaxed_receptor_atoms)
        receptor_audit["covalent_geometry"] = _receptor_covalent_geometry(
            complete_receptor, int(config["receptor"]["construct_start"]),
            int(config["receptor"]["construct_end"]), config["receptor"].get("disulfides", []),
        )
        missing_numbers = {int(value) for value in config["receptor"].get("modeled_residue_numbers", [])}
        modeled_atoms = [atom for atom in complete_receptor if atom.residue_number in missing_numbers]
        modeled_to_ligand = _minimum_distance(modeled_atoms, ligand_atoms) if modeled_atoms else None
        receptor_to_ligand = _minimum_distance(complete_receptor, ligand_atoms)
        if receptor_to_ligand < float(config["receptor"].get("minimum_receptor_ligand_distance_angstrom", 1.5)):
            raise MDBuildError(f"Rank {rank} has a severe receptor-ligand clash after receptor replacement: {receptor_to_ligand:.3f} A")
        receptor_pdb = job / "receptor_complete_aligned.pdb"
        ligand_pdb = job / "ligand_relaxed_pose.pdb"
        _write_pdb(receptor_pdb, [complete_receptor]); _write_pdb(ligand_pdb, _chain_groups(ligand_atoms))
        dry_prmtop = job / "complex_dry.prmtop"; dry_inpcrd = job / "complex_dry.inpcrd"
        dry_pdb = job / "complex_dry.pdb"; leap_input = job / "tleap_dry.in"; leap_log = job / "tleap_dry.log"
        leap_input.write_text(
            _dry_tleap_text(config, bundle, receptor_pdb, ligand_pdb, dry_prmtop, dry_inpcrd, dry_pdb, complete_receptor),
            encoding="utf-8",
        )
        _run_tleap(str(tleap_executable), leap_input, leap_log)
        for required in (dry_prmtop, dry_inpcrd, dry_pdb):
            if not required.is_file():
                raise MDBuildError(f"TLEaP did not create required dry-complex output: {required}")
        if _has_periodic_box(dry_prmtop):
            raise MDBuildError(f"Dry topology unexpectedly contains periodic box information: {dry_prmtop}")
        expected_ligand_charge = float(config["ligand"].get("formal_charge", 5))
        if abs(_prmtop_charge(ligand_prmtop) - expected_ligand_charge) > 1e-3:
            raise MDBuildError(f"Rank {rank} ligand topology is not charge {expected_ligand_charge:+g}")
        relaxed_dry_raw_pdb = job / "complex_relaxed_dry_raw.pdb"
        local_relaxation = _local_relax(
            config, dry_prmtop, dry_inpcrd, relaxed_dry_raw_pdb,
            receptor_residue_count=len(_residue_order(complete_receptor)),
        )
        receptor_residue_names = [name for _, name in _residue_order(complete_receptor)]
        ligand_residue_names = [name for _, name in _residue_order(ligand_atoms)]
        relaxed_dry_pdb = job / "complex_relaxed_dry.pdb"
        normalized_relaxed = _normalize_relaxed_complex_pdb(
            relaxed_dry_raw_pdb, relaxed_dry_pdb,
            receptor_start=int(config["receptor"]["construct_start"]),
            receptor_residue_count=len(_residue_order(complete_receptor)),
            ligand_residue_count=len(_residue_order(ligand_atoms)),
            receptor_residue_names=receptor_residue_names,
            ligand_residue_names=ligand_residue_names,
        )
        local_relaxation["raw_relaxed_pdb"] = local_relaxation.pop("relaxed_pdb")
        local_relaxation["raw_relaxed_pdb_sha256"] = local_relaxation.pop("relaxed_pdb_sha256")
        local_relaxation["relaxed_pdb"] = str(relaxed_dry_pdb)
        local_relaxation["relaxed_pdb_sha256"] = _sha256(relaxed_dry_pdb)
        local_relaxation["pdb_normalization"] = normalized_relaxed
        relaxed_dry_tleap_input_pdb = job / "complex_relaxed_dry_tleap_input.pdb"
        tleap_residue_numbering = _write_tleap_sequential_pdb(
            relaxed_dry_pdb, relaxed_dry_tleap_input_pdb,
        )
        local_relaxation["tleap_residue_numbering"] = tleap_residue_numbering
        relaxed_dry_prmtop = job / "complex_relaxed_dry.prmtop"
        relaxed_dry_inpcrd = job / "complex_relaxed_dry.inpcrd"
        relaxed_dry_normalized_pdb = job / "complex_relaxed_dry_tleap.pdb"
        relaxed_leap_input = job / "tleap_relaxed_dry.in"
        relaxed_leap_log = job / "tleap_relaxed_dry.log"
        relaxed_leap_input.write_text(
            _combined_dry_tleap_text(
                config, bundle, relaxed_dry_tleap_input_pdb, complete_receptor,
                relaxed_dry_prmtop, relaxed_dry_inpcrd, relaxed_dry_normalized_pdb,
            ),
            encoding="utf-8",
        )
        _run_tleap(str(tleap_executable), relaxed_leap_input, relaxed_leap_log)
        for required in (relaxed_dry_prmtop, relaxed_dry_inpcrd, relaxed_dry_normalized_pdb):
            if not required.is_file():
                raise MDBuildError(f"TLEaP did not create relaxed dry-complex output: {required}")
        if _has_periodic_box(relaxed_dry_prmtop):
            raise MDBuildError(f"Relaxed dry topology unexpectedly contains a periodic box: {relaxed_dry_prmtop}")
        result = {
            "rank": rank, "ranked_candidate": record.get("candidate"),
            "vina_score_only_kcal_per_mol": record.get("vina_score_only_kcal_per_mol"),
            "source_relaxed_complex": str(relaxed_path), "source_relaxed_complex_sha256": _sha256(relaxed_path),
            "receptor": receptor_audit,
            "minimum_receptor_ligand_distance_angstrom_before_rebuild": receptor_to_ligand,
            "minimum_modeled_residue_ligand_distance_angstrom": modeled_to_ligand,
            "receptor_pdb": str(receptor_pdb), "ligand_pdb": str(ligand_pdb),
            "dry_prmtop": str(dry_prmtop), "dry_inpcrd": str(dry_inpcrd),
            "dry_complex_charge": _prmtop_charge(dry_prmtop),
            "dry_prmtop_sha256": _sha256(dry_prmtop), "dry_inpcrd_sha256": _sha256(dry_inpcrd),
            "tleap_input": str(leap_input), "tleap_log": str(leap_log),
            "local_relaxation": local_relaxation,
            "relaxed_dry_prmtop": str(relaxed_dry_prmtop),
            "relaxed_dry_inpcrd": str(relaxed_dry_inpcrd),
            "relaxed_dry_complex_charge": _prmtop_charge(relaxed_dry_prmtop),
            "relaxed_dry_prmtop_sha256": _sha256(relaxed_dry_prmtop),
            "relaxed_dry_inpcrd_sha256": _sha256(relaxed_dry_inpcrd),
            "relaxed_dry_tleap_log": str(relaxed_leap_log),
        }
        _write_json(job / "stage1_manifest.json", result); results.append(result)
    summary = {"status": "completed", "created_at_utc": _utc_now(), "candidate_count": len(results), "candidates": results}
    _write_json(output / "stage1_reconstruction_summary.json", summary)
    return summary


def solvate(config: dict[str, Any], base: Path) -> dict[str, Any]:
    output = _resolve(config["output_dir"], base); bundle = _parameter_bundle(config, base, output)
    tleap = str((config.get("commands") or {}).get("tleap", "tleap"))
    tleap_executable = shutil.which(tleap) or (tleap if Path(tleap).is_file() else None)
    if not tleap_executable:
        raise MDBuildError(f"TLEaP executable not found: {tleap}")
    stage1_path = output / "stage1_reconstruction_summary.json"
    if not stage1_path.is_file():
        raise MDBuildError("Stage-1 summary is missing; run reconstruct first")
    stage1 = json.loads(stage1_path.read_text(encoding="utf-8"))
    receptor_model = _resolve(config["receptor"]["selected_model"], base)
    receptor_atoms = [atom for atom in _read_pdb_atoms(receptor_model, include_hydrogen=False) if atom.chain == str(config["receptor"].get("chain_id", "A"))]
    results: list[dict[str, Any]] = []
    salt_molar = float(config["solvation"].get("salt_molar", 0.15))
    for candidate in stage1["candidates"]:
        rank = int(candidate["rank"]); job = output / f"rank_{rank:02d}"
        relaxed_pdb = Path(candidate["local_relaxation"]["relaxed_pdb"])
        tleap_input_pdb = job / "complex_relaxed_solvate_tleap_input.pdb"
        tleap_residue_numbering = _write_tleap_sequential_pdb(relaxed_pdb, tleap_input_pdb)
        presolvated_pdb = job / "complex_presolvated_no_ions.pdb"
        pass1_input = job / "tleap_solvate_pass1.in"; pass1_log = job / "tleap_solvate_pass1.log"
        pass1_input.write_text(
            _solvate_tleap_text(config, bundle, tleap_input_pdb, receptor_atoms, presolvated_pdb), encoding="utf-8"
        )
        _run_tleap(str(tleap_executable), pass1_input, pass1_log)
        counts = _count_residues(presolvated_pdb)
        water_count = sum(counts.get(name, 0) for name in WATER_RESIDUES)
        if water_count <= 0:
            raise MDBuildError(f"No waters were created during solvation for rank {rank}")
        dry_charge = float(candidate["dry_complex_charge"])
        rounded_charge = int(round(dry_charge))
        if abs(dry_charge - rounded_charge) > 1e-3:
            raise MDBuildError(f"Dry complex charge is not integral for rank {rank}: {dry_charge}")
        salt_pairs = int(round(water_count * salt_molar / 55.5))
        na_count = salt_pairs + max(0, -rounded_charge)
        cl_count = salt_pairs + max(0, rounded_charge)
        solvated_prmtop = job / "complex_solvated.prmtop"; solvated_inpcrd = job / "complex_solvated.inpcrd"
        solvated_pdb = job / "complex_solvated.pdb"
        pass2_input = job / "tleap_solvate_pass2.in"; pass2_log = job / "tleap_solvate_pass2.log"
        pass2_input.write_text(
            _solvate_tleap_text(
                config, bundle, tleap_input_pdb, receptor_atoms, solvated_pdb,
                na_count=na_count, cl_count=cl_count, prmtop=solvated_prmtop, inpcrd=solvated_inpcrd,
            ),
            encoding="utf-8",
        )
        _run_tleap(str(tleap_executable), pass2_input, pass2_log)
        for required in (solvated_prmtop, solvated_inpcrd, solvated_pdb):
            if not required.is_file():
                raise MDBuildError(f"TLEaP did not create required solvated output: {required}")
        if not _has_periodic_box(solvated_prmtop):
            raise MDBuildError(f"Solvated topology lacks periodic box information: {solvated_prmtop}")
        final_charge = _prmtop_charge(solvated_prmtop)
        if abs(final_charge) > 1e-3:
            raise MDBuildError(f"Solvated system is not neutral for rank {rank}: {final_charge}")
        final_counts = _count_residues(solvated_pdb)
        result = {
            "rank": rank, "ranked_candidate": candidate.get("ranked_candidate"),
            "water_model": "TIP3P", "box_shape": "truncated_octahedron",
            "padding_angstrom": float(config["solvation"].get("padding_angstrom", 12.0)),
            "target_salt_molar": salt_molar, "initial_water_count": water_count,
            "salt_pairs": salt_pairs, "na_count": na_count, "cl_count": cl_count,
            "tleap_residue_numbering": tleap_residue_numbering,
            "final_residue_counts": final_counts, "final_charge": final_charge,
            "solvated_prmtop": str(solvated_prmtop), "solvated_inpcrd": str(solvated_inpcrd),
            "solvated_pdb": str(solvated_pdb), "solvated_atom_count": _prmtop_natom(solvated_prmtop),
            "solvated_prmtop_sha256": _sha256(solvated_prmtop),
            "solvated_inpcrd_sha256": _sha256(solvated_inpcrd),
            "tleap_pass1_log": str(pass1_log), "tleap_pass2_log": str(pass2_log),
        }
        _write_json(job / "stage2_solvation_manifest.json", result); results.append(result)
    summary = {"status": "completed", "created_at_utc": _utc_now(), "candidate_count": len(results), "candidates": results}
    _write_json(output / "stage2_solvation_summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["preflight", "reconstruct", "solvate", "all"])
    parser.add_argument("--config", required=True, help="YAML or JSON MD-build configuration")
    parser.add_argument(
        "--confirm-reviewed-inputs", action="store_true",
        help=(
            "Record that the selected complete receptor and the approved parameter source "
            "have been reviewed for this production-MD input build"
        ),
    )
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = _load_mapping(config_path); base = config_path.parent
    if args.confirm_reviewed_inputs:
        config["receptor"].setdefault("approval_attestation", {})["human_review_confirmed"] = True
        config["parameters"].setdefault("approval_attestation", {})["production_md_confirmed"] = True
    try:
        if args.stage == "preflight":
            result = _preflight(config, base)
        elif args.stage == "reconstruct":
            _preflight(config, base); result = reconstruct(config, base)
        elif args.stage == "solvate":
            _preflight(config, base); result = solvate(config, base)
        else:
            _preflight(config, base); reconstruct(config, base); result = solvate(config, base)
    except MDBuildError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
