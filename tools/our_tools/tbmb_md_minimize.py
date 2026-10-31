#!/usr/bin/env python3
"""Run validated explicit-solvent minimization for TBMB/Nectin-4 MD inputs.

This is Stage 3 of the workflow.  It consumes the three solvated AMBER systems
from ``tbmb_md_build.py``, relaxes newly generated hydrogens and solvent under
progressively weaker solute-heavy-atom restraints, performs a final unrestrained
PME minimization, and writes a new AMBER coordinate/topology pair plus audit
manifests.  It does not run NVT, NPT, or production dynamics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MDMinimizeError(RuntimeError):
    """A deterministic input, topology, minimization, or validation failure."""


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
            raise MDMinimizeError("PyYAML is required for the Stage-3 YAML configuration") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise MDMinimizeError(f"Expected one mapping in {path}")
    return data


def _resolve(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _prmtop_values(path: Path, flag: str) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    marker = f"%FLAG {flag}"
    try:
        index = next(i for i, line in enumerate(lines) if line.strip() == marker)
    except StopIteration as exc:
        raise MDMinimizeError(f"{flag} is absent from AMBER topology: {path}") from exc
    values: list[str] = []
    for line in lines[index + 2:]:
        if line.startswith("%FLAG"):
            break
        values.extend(line.split())
    return values


def _prmtop_natom(path: Path) -> int:
    values = _prmtop_values(path, "POINTERS")
    if not values:
        raise MDMinimizeError(f"Empty POINTERS section: {path}")
    return int(values[0])


def _inpcrd_natom(path: Path) -> int:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        raise MDMinimizeError(f"Malformed AMBER coordinate file: {path}")
    try:
        return int(lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        raise MDMinimizeError(f"Cannot read atom count from AMBER coordinate file: {path}") from exc


def _vector_values(vector: Any, target_unit: Any) -> tuple[float, float, float]:
    values = vector.value_in_unit(target_unit) if hasattr(vector, "value_in_unit") else vector
    return float(values[0]), float(values[1]), float(values[2])


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _angle_degrees(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    denominator = _norm(first) * _norm(second)
    if denominator == 0:
        raise MDMinimizeError("Periodic box contains a zero-length vector")
    cosine = sum(a * b for a, b in zip(first, second)) / denominator
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _write_amber_inpcrd(path: Path, positions: Any, box_vectors: Any, unit: Any) -> None:
    """Write a positions-only formatted AMBER restart with periodic box data."""
    coordinates = positions.value_in_unit(unit.angstrom)
    flattened = [float(value) for xyz in coordinates for value in xyz]
    if box_vectors is None:
        raise MDMinimizeError("Cannot write a periodic restart without box vectors")
    vectors = [_vector_values(vector, unit.angstrom) for vector in box_vectors]
    lengths = [_norm(vector) for vector in vectors]
    alpha = _angle_degrees(vectors[1], vectors[2])
    beta = _angle_degrees(vectors[0], vectors[2])
    gamma = _angle_degrees(vectors[0], vectors[1])
    lines = ["TBMB/Nectin-4 Stage-3 minimized coordinates", f"{len(coordinates):6d}"]
    for start in range(0, len(flattened), 6):
        lines.append("".join(f"{value:12.7f}" for value in flattened[start:start + 6]))
    lines.append("".join(f"{value:12.7f}" for value in [*lengths, alpha, beta, gamma]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _atom_label(atom: Any) -> str:
    return f"{atom.residue.name}:{atom.residue.index + 1}:{atom.name}:{atom.index + 1}"


def _distance_angstrom(first: Any, second: Any, unit: Any) -> float:
    left = _vector_values(first, unit.angstrom)
    right = _vector_values(second, unit.angstrom)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def _minimum_solute_contact(
    topology: Any, positions: Any, solute_residue_count: int, unit: Any,
) -> dict[str, Any]:
    """Find closest non-directly-bonded solute pair, including 1-3 geometry."""
    atoms = list(topology.atoms())
    solute = [atom for atom in atoms if atom.residue.index < solute_residue_count]
    directly_bonded = {
        tuple(sorted((first.index, second.index))) for first, second in topology.bonds()
    }
    values = positions.value_in_unit(unit.angstrom)
    best_all: tuple[float, Any, Any] | None = None
    best_heavy: tuple[float, Any, Any] | None = None
    for left_index, left in enumerate(solute):
        left_xyz = values[left.index]
        left_hydrogen = left.element is not None and left.element.symbol == "H"
        for right in solute[left_index + 1:]:
            if tuple(sorted((left.index, right.index))) in directly_bonded:
                continue
            right_xyz = values[right.index]
            distance = math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left_xyz, right_xyz)))
            if best_all is None or distance < best_all[0]:
                best_all = (distance, left, right)
            right_hydrogen = right.element is not None and right.element.symbol == "H"
            if (
                not left_hydrogen
                and not right_hydrogen
                and (best_heavy is None or distance < best_heavy[0])
            ):
                best_heavy = (distance, left, right)
    if best_all is None or best_heavy is None:
        raise MDMinimizeError("Unable to calculate solute contact distances")
    return {
        "minimum_all_atom_non_direct_bonded_angstrom": best_all[0],
        "minimum_all_atom_pair": [_atom_label(best_all[1]), _atom_label(best_all[2])],
        "minimum_heavy_atom_non_direct_bonded_angstrom": best_heavy[0],
        "minimum_heavy_atom_pair": [_atom_label(best_heavy[1]), _atom_label(best_heavy[2])],
        "definition": "solute pairs excluding only directly bonded 1-2 pairs; 1-3 geometry retained",
    }


def _force_statistics(forces: Any, unit: Any) -> dict[str, float]:
    values = forces.value_in_unit(unit.kilojoules_per_mole / unit.nanometer)
    magnitudes = [math.sqrt(sum(float(component) ** 2 for component in force)) for force in values]
    component_sum = sum(
        float(component) ** 2 for force in values for component in force
    )
    return {
        "rms_force_component_kj_mol_nm": math.sqrt(component_sum / (3 * len(values))),
        "maximum_atom_force_kj_mol_nm": max(magnitudes),
    }


def _monitored_bond_geometry(
    topology: Any, positions: Any, records: Iterable[dict[str, Any]], unit: Any,
) -> list[dict[str, Any]]:
    residues = list(topology.residues())
    atom_lookup = {
        (residue.index + 1, atom.name): atom for residue in residues for atom in residue.atoms()
    }
    topology_bonds = {
        tuple(sorted((first.index, second.index))) for first, second in topology.bonds()
    }
    results: list[dict[str, Any]] = []
    for record in records:
        first_key = (int(record["first_residue"]), str(record["first_atom"]))
        second_key = (int(record["second_residue"]), str(record["second_atom"]))
        if first_key not in atom_lookup or second_key not in atom_lookup:
            raise MDMinimizeError(f"Monitored bond atom is missing: {first_key}--{second_key}")
        first = atom_lookup[first_key]
        second = atom_lookup[second_key]
        distance = _distance_angstrom(positions[first.index], positions[second.index], unit)
        minimum = float(record["minimum_angstrom"])
        maximum = float(record["maximum_angstrom"])
        bonded = tuple(sorted((first.index, second.index))) in topology_bonds
        results.append({
            "name": str(record["name"]),
            "first": _atom_label(first), "second": _atom_label(second),
            "is_bonded_in_topology": bonded,
            "distance_angstrom": distance,
            "minimum_angstrom": minimum, "maximum_angstrom": maximum,
            "passed": bonded and minimum <= distance <= maximum,
        })
    return results


def _special_residue_counts(prmtop_path: Path, names: Iterable[str]) -> dict[str, int]:
    """Count authoritative AMBER residue labels without OpenMM normalization.

    OpenMM intentionally maps CYX/HID/HIE to generic CYS/HIS names in its
    application-layer Topology.  The AMBER RESIDUE_LABEL flag retains the
    parameter-defining identities and is therefore the correct audit source.
    """
    requested = {str(name) for name in names}
    counts = {name: 0 for name in requested}
    for residue_name in _prmtop_values(prmtop_path, "RESIDUE_LABEL"):
        if residue_name in counts:
            counts[residue_name] += 1
    return dict(sorted(counts.items()))


def _input_records(config: dict[str, Any], base: Path) -> tuple[Path, list[dict[str, Any]]]:
    input_dir = _resolve(config["input_dir"], base)
    summary_path = input_dir / "stage2_solvation_summary.json"
    if not summary_path.is_file():
        raise MDMinimizeError(f"Stage-2 summary is missing: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "completed":
        raise MDMinimizeError("Stage-2 summary is not completed")
    candidates = summary.get("candidates") or []
    expected_count = int(config.get("candidate_count", 3))
    if len(candidates) != expected_count:
        raise MDMinimizeError(
            f"Stage-2 candidate count is {len(candidates)}; expected {expected_count}"
        )
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        rank = int(candidate["rank"])
        job = input_dir / f"rank_{rank:02d}"
        manifest_path = job / "stage2_solvation_manifest.json"
        if not manifest_path.is_file():
            raise MDMinimizeError(f"Stage-2 rank manifest is missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prmtop = job / "complex_solvated.prmtop"
        inpcrd = job / "complex_solvated.inpcrd"
        for path in (prmtop, inpcrd):
            if not path.is_file():
                raise MDMinimizeError(f"Required Stage-2 input is missing: {path}")
        if _sha256(prmtop) != manifest.get("solvated_prmtop_sha256"):
            raise MDMinimizeError(f"Stage-2 prmtop hash mismatch for rank {rank}")
        if _sha256(inpcrd) != manifest.get("solvated_inpcrd_sha256"):
            raise MDMinimizeError(f"Stage-2 inpcrd hash mismatch for rank {rank}")
        if _prmtop_natom(prmtop) != _inpcrd_natom(inpcrd):
            raise MDMinimizeError(f"Stage-2 topology/coordinate atom count mismatch for rank {rank}")
        records.append({
            "rank": rank, "ranked_candidate": candidate.get("ranked_candidate"),
            "prmtop": prmtop, "inpcrd": inpcrd,
            "manifest": manifest_path, "stage2": candidate,
        })
    return summary_path, records


def _openmm_modules() -> dict[str, Any]:
    try:
        import openmm
        from openmm import (
            CustomExternalForce,
            LangevinMiddleIntegrator,
            LocalEnergyMinimizer,
            Platform,
            XmlSerializer,
            unit,
        )
        from openmm.app import (
            PME,
            AmberInpcrdFile,
            AmberPrmtopFile,
            PDBFile,
            Simulation,
        )
    except ImportError as exc:
        raise MDMinimizeError("OpenMM with AMBER file support is required for Stage 3") from exc
    return {
        "openmm": openmm, "CustomExternalForce": CustomExternalForce,
        "LangevinMiddleIntegrator": LangevinMiddleIntegrator,
        "LocalEnergyMinimizer": LocalEnergyMinimizer, "Platform": Platform,
        "XmlSerializer": XmlSerializer, "unit": unit,
        "AmberInpcrdFile": AmberInpcrdFile, "AmberPrmtopFile": AmberPrmtopFile,
        "PME": PME, "PDBFile": PDBFile, "Simulation": Simulation,
    }


def preflight(config: dict[str, Any], base: Path) -> dict[str, Any]:
    modules = _openmm_modules()
    summary_path, records = _input_records(config, base)
    platform_name = str(config["runtime"].get("platform", "CUDA"))
    try:
        platform = modules["Platform"].getPlatformByName(platform_name)
    except Exception as exc:
        raise MDMinimizeError(f"Requested OpenMM platform is unavailable: {platform_name}") from exc
    output_dir = _resolve(config["output_dir"], base)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "pass", "created_at_utc": _utc_now(),
        "stage2_summary": str(summary_path), "stage2_summary_sha256": _sha256(summary_path),
        "candidate_count": len(records),
        "openmm_version": getattr(modules["openmm"], "__version__", "unknown"),
        "platform": platform.getName(),
        "input_atom_counts": {str(item["rank"]): _prmtop_natom(item["prmtop"]) for item in records},
    }
    _write_json(output_dir / "preflight.json", result)
    return result


def _minimize_one(
    config: dict[str, Any], record: dict[str, Any], output_dir: Path, modules: dict[str, Any],
) -> dict[str, Any]:
    unit = modules["unit"]
    prmtop = modules["AmberPrmtopFile"](str(record["prmtop"]))
    inpcrd = modules["AmberInpcrdFile"](str(record["inpcrd"]))
    if inpcrd.boxVectors is None:
        raise MDMinimizeError(f"Rank {record['rank']} coordinate file lacks periodic box vectors")
    prmtop.topology.setPeriodicBoxVectors(inpcrd.boxVectors)
    section = config["minimization"]
    constraint_mode = str(section.get("constraints", "none")).lower()
    if constraint_mode != "none":
        raise MDMinimizeError(
            "Stage-3 force validation requires minimization.constraints=none; "
            "constraints are introduced during NVT"
        )
    rigid_water = bool(section.get("rigid_water", False))
    if rigid_water:
        raise MDMinimizeError(
            "Stage-3 force validation requires minimization.rigid_water=false"
        )
    system = prmtop.createSystem(
        nonbondedMethod=modules["PME"],
        nonbondedCutoff=float(section.get("nonbonded_cutoff_nm", 1.0)) * unit.nanometer,
        constraints=None, rigidWater=False, removeCMMotion=True,
        ewaldErrorTolerance=float(section.get("ewald_error_tolerance", 0.0005)),
    )
    if not system.usesPeriodicBoundaryConditions():
        raise MDMinimizeError(f"Rank {record['rank']} OpenMM system is not periodic")

    restraint = modules["CustomExternalForce"](
        "0.5*k_pos*periodicdistance(x,y,z,x0,y0,z0)^2"
    )
    restraint.addGlobalParameter("k_pos", 0.0)
    for name in ("x0", "y0", "z0"):
        restraint.addPerParticleParameter(name)
    solute_residue_count = int(config["system"]["solute_residue_count"])
    restrained_atoms = 0
    reference_nm = inpcrd.positions.value_in_unit(unit.nanometer)
    for atom in prmtop.topology.atoms():
        is_hydrogen = atom.element is not None and atom.element.symbol == "H"
        if atom.residue.index < solute_residue_count and not is_hydrogen:
            restraint.addParticle(atom.index, [float(value) for value in reference_nm[atom.index]])
            restrained_atoms += 1
    system.addForce(restraint)

    integrator = modules["LangevinMiddleIntegrator"](
        float(section.get("temperature_kelvin", 298.15)) * unit.kelvin,
        1.0 / unit.picosecond, 0.001 * unit.picoseconds,
    )
    integrator.setConstraintTolerance(float(section.get("constraint_tolerance", 1e-6)))
    platform = modules["Platform"].getPlatformByName(str(config["runtime"].get("platform", "CUDA")))
    properties = {str(k): str(v) for k, v in (config["runtime"].get("platform_properties") or {}).items()}
    simulation = modules["Simulation"](
        prmtop.topology, system, integrator, platform, properties,
    )
    simulation.context.setPeriodicBoxVectors(*inpcrd.boxVectors)
    simulation.context.setPositions(inpcrd.positions)

    simulation.context.setParameter("k_pos", 0.0)
    initial_state = simulation.context.getState(getEnergy=True)
    initial_physical_energy = float(
        initial_state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)
    )
    if not math.isfinite(initial_physical_energy):
        raise MDMinimizeError(f"Rank {record['rank']} initial energy is not finite")

    stages: list[dict[str, Any]] = []
    conversion = 4.184 / 0.01  # kcal mol^-1 A^-2 -> kJ mol^-1 nm^-2
    for index, stage in enumerate(section.get("stages") or [], 1):
        force_constant = float(stage["restraint_kcal_mol_a2"])
        tolerance = float(stage.get("tolerance_kj_mol_nm", 10.0))
        max_iterations = int(stage.get("max_iterations", 10000))
        simulation.context.setParameter("k_pos", force_constant * conversion)
        modules["LocalEnergyMinimizer"].minimize(
            simulation.context, tolerance=tolerance, maxIterations=max_iterations,
        )
        state = simulation.context.getState(getEnergy=True, getForces=True)
        energy = float(state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole))
        force_stats = _force_statistics(state.getForces(), unit)
        if not math.isfinite(energy):
            raise MDMinimizeError(f"Rank {record['rank']} Stage-3 energy became non-finite")
        stages.append({
            "stage": index, "restraint_kcal_mol_a2": force_constant,
            "tolerance_kj_mol_nm": tolerance, "max_iterations": max_iterations,
            "potential_energy_kj_mol": energy, **force_stats,
        })
    if not stages or stages[-1]["restraint_kcal_mol_a2"] != 0.0:
        raise MDMinimizeError("The final minimization stage must be unrestrained (0.0)")

    final_state = simulation.context.getState(
        getEnergy=True, getForces=True, getPositions=True, enforcePeriodicBox=False,
    )
    final_positions = final_state.getPositions()
    final_energy = float(final_state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole))
    final_forces = _force_statistics(final_state.getForces(), unit)
    contacts = _minimum_solute_contact(
        prmtop.topology, final_positions, solute_residue_count, unit,
    )
    bonds = _monitored_bond_geometry(
        prmtop.topology, final_positions, config["system"].get("monitored_bonds") or [], unit,
    )
    special_counts = _special_residue_counts(
        record["prmtop"], (config["system"].get("expected_special_residue_counts") or {}).keys(),
    )
    expected_counts = {
        str(k): int(v) for k, v in (config["system"].get("expected_special_residue_counts") or {}).items()
    }
    gates = config["validation"]
    failures: list[str] = []
    if not math.isfinite(final_energy):
        failures.append("final energy is non-finite")
    if final_energy >= initial_physical_energy:
        failures.append("final unrestrained energy did not decrease")
    rms_gate = float(gates["maximum_rms_force_kj_mol_nm"])
    maximum_force_gate = float(gates["maximum_atom_force_kj_mol_nm"])
    if final_forces["rms_force_component_kj_mol_nm"] > rms_gate:
        failures.append(
            "final RMS force exceeds gate: "
            f'{final_forces["rms_force_component_kj_mol_nm"]:.6g} > {rms_gate:.6g} kJ mol^-1 nm^-1'
        )
    if final_forces["maximum_atom_force_kj_mol_nm"] > maximum_force_gate:
        failures.append(
            "final maximum atom force exceeds gate: "
            f'{final_forces["maximum_atom_force_kj_mol_nm"]:.6g} > '
            f"{maximum_force_gate:.6g} kJ mol^-1 nm^-1"
        )
    if contacts["minimum_all_atom_non_direct_bonded_angstrom"] < float(gates["minimum_all_atom_contact_angstrom"]):
        failures.append("final all-atom solute contact is below the configured gate")
    if contacts["minimum_heavy_atom_non_direct_bonded_angstrom"] < float(gates["minimum_heavy_atom_contact_angstrom"]):
        failures.append("final heavy-atom solute contact is below the configured gate")
    if any(not item["passed"] for item in bonds):
        failures.append("one or more monitored covalent bonds failed geometry/topology validation")
    if special_counts != dict(sorted(expected_counts.items())):
        failures.append(f"special residue counts differ: {special_counts} != {expected_counts}")

    rank_dir = output_dir / f"rank_{record['rank']:02d}"
    rank_dir.mkdir(parents=True, exist_ok=True)
    output_prmtop = rank_dir / "complex_minimized.prmtop"
    output_inpcrd = rank_dir / "complex_minimized.inpcrd"
    output_pdb = rank_dir / "complex_minimized.pdb"
    output_state = rank_dir / "minimized_state.xml"
    output_checkpoint = rank_dir / "minimized.chk"
    shutil.copy2(record["prmtop"], output_prmtop)
    _write_amber_inpcrd(output_inpcrd, final_positions, inpcrd.boxVectors, unit)
    with output_pdb.open("w", encoding="utf-8") as handle:
        modules["PDBFile"].writeFile(prmtop.topology, final_positions, handle, keepIds=True)
    output_state.write_text(modules["XmlSerializer"].serialize(final_state) + "\n", encoding="utf-8")
    output_checkpoint.write_bytes(simulation.context.createCheckpoint())
    if _prmtop_natom(output_prmtop) != _inpcrd_natom(output_inpcrd):
        failures.append("output topology/coordinate atom count mismatch")

    result = {
        "status": "passed" if not failures else "failed",
        "created_at_utc": _utc_now(), "rank": record["rank"],
        "ranked_candidate": record.get("ranked_candidate"),
        "input_prmtop": str(record["prmtop"]), "input_prmtop_sha256": _sha256(record["prmtop"]),
        "input_inpcrd": str(record["inpcrd"]), "input_inpcrd_sha256": _sha256(record["inpcrd"]),
        "platform": platform.getName(), "platform_properties": properties,
        "periodic_method": "PME", "constraints": constraint_mode,
        "rigid_water": rigid_water,
        "restrained_solute_heavy_atoms": restrained_atoms,
        "initial_unrestrained_potential_energy_kj_mol": initial_physical_energy,
        "final_unrestrained_potential_energy_kj_mol": final_energy,
        "energy_decreased": final_energy < initial_physical_energy,
        "stages": stages, "final_force_statistics": final_forces,
        "solute_contacts": contacts, "monitored_bonds": bonds,
        "special_residue_counts": special_counts, "validation_failures": failures,
        "output_prmtop": str(output_prmtop), "output_prmtop_sha256": _sha256(output_prmtop),
        "output_inpcrd": str(output_inpcrd), "output_inpcrd_sha256": _sha256(output_inpcrd),
        "output_pdb": str(output_pdb), "output_pdb_sha256": _sha256(output_pdb),
        "output_state_xml": str(output_state), "output_state_xml_sha256": _sha256(output_state),
        "output_checkpoint": str(output_checkpoint), "output_checkpoint_sha256": _sha256(output_checkpoint),
    }
    _write_json(rank_dir / "stage3_minimization_manifest.json", result)
    if failures:
        raise MDMinimizeError(
            f"Rank {record['rank']} Stage-3 validation failed: " + "; ".join(failures)
        )
    return result


def minimize(config: dict[str, Any], base: Path, overwrite: bool = False) -> dict[str, Any]:
    modules = _openmm_modules()
    summary_path, records = _input_records(config, base)
    output_dir = _resolve(config["output_dir"], base)
    summary_output = output_dir / "stage3_minimization_summary.json"
    if summary_output.exists() and not overwrite:
        existing = json.loads(summary_output.read_text(encoding="utf-8"))
        if existing.get("status") == "completed":
            raise MDMinimizeError(
                f"Completed Stage-3 summary already exists: {summary_output}; "
                "use --overwrite to rerun"
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for record in records:
        try:
            results.append(_minimize_one(config, record, output_dir, modules))
        except MDMinimizeError as exc:
            failed_manifest = (
                output_dir / f"rank_{record['rank']:02d}" / "stage3_minimization_manifest.json"
            )
            if failed_manifest.is_file():
                results.append(json.loads(failed_manifest.read_text(encoding="utf-8")))
            failed_summary = {
                "status": "failed", "created_at_utc": _utc_now(),
                "source_stage2_summary": str(summary_path),
                "source_stage2_summary_sha256": _sha256(summary_path),
                "candidate_count_completed_or_attempted": len(results),
                "failed_rank": record["rank"], "error": str(exc),
                "candidates": results,
                "next_stage": "blocked_until_stage3_validation_passes",
            }
            _write_json(summary_output, failed_summary)
            raise
    summary = {
        "status": "completed", "created_at_utc": _utc_now(),
        "source_stage2_summary": str(summary_path),
        "source_stage2_summary_sha256": _sha256(summary_path),
        "candidate_count": len(results), "candidates": results,
        "next_stage": "restrained_NVT_heating_then_NPT_density_equilibration",
    }
    _write_json(summary_output, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["preflight", "minimize", "all"])
    parser.add_argument("--config", required=True, help="YAML or JSON Stage-3 configuration")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacement of an existing Stage-3 summary")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = _load_mapping(config_path)
    try:
        if args.stage == "preflight":
            result = preflight(config, config_path.parent)
        elif args.stage == "minimize":
            result = minimize(config, config_path.parent, overwrite=args.overwrite)
        else:
            preflight(config, config_path.parent)
            result = minimize(config, config_path.parent, overwrite=args.overwrite)
    except MDMinimizeError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
