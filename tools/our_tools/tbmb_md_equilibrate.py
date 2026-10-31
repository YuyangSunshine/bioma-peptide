#!/usr/bin/env python3
"""Run restrained NVT heating and staged NPT equilibration for TBMB/Nectin-4.

Stage 4 consumes the three hash-bound, minimized AMBER systems from Stage 3.
It heats each system to the target temperature under solute-heavy-atom
restraints, switches to NPT, releases the restraints in stages, and writes a
validated AMBER topology/coordinate pair for production-MD preparation.
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MDEquilibrateError(RuntimeError):
    """A deterministic input, dynamics, or validation failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _progress(message: str) -> None:
    print(f"[{_utc_now()}] {message}", flush=True)


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
            raise MDEquilibrateError("PyYAML is required for the Stage-4 YAML configuration") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise MDEquilibrateError(f"Expected one mapping in {path}")
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
        raise MDEquilibrateError(f"{flag} is absent from AMBER topology: {path}") from exc
    values: list[str] = []
    for line in lines[index + 2:]:
        if line.startswith("%FLAG"):
            break
        values.extend(line.split())
    return values


def _prmtop_natom(path: Path) -> int:
    values = _prmtop_values(path, "POINTERS")
    if not values:
        raise MDEquilibrateError(f"Empty POINTERS section: {path}")
    return int(values[0])


def _inpcrd_natom(path: Path) -> int:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        raise MDEquilibrateError(f"Malformed AMBER coordinate file: {path}")
    try:
        return int(lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        raise MDEquilibrateError(f"Cannot read atom count from AMBER coordinate file: {path}") from exc


def _special_residue_counts(prmtop_path: Path, names: Iterable[str]) -> dict[str, int]:
    requested = {str(name) for name in names}
    counts = {name: 0 for name in requested}
    for residue_name in _prmtop_values(prmtop_path, "RESIDUE_LABEL"):
        if residue_name in counts:
            counts[residue_name] += 1
    return dict(sorted(counts.items()))


def _vector_values(vector: Any, target_unit: Any) -> tuple[float, float, float]:
    values = vector.value_in_unit(target_unit) if hasattr(vector, "value_in_unit") else vector
    return float(values[0]), float(values[1]), float(values[2])


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _angle_degrees(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    denominator = _norm(first) * _norm(second)
    if denominator == 0:
        raise MDEquilibrateError("Periodic box contains a zero-length vector")
    cosine = sum(a * b for a, b in zip(first, second)) / denominator
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _write_amber_inpcrd(path: Path, positions: Any, box_vectors: Any, unit: Any) -> None:
    coordinates = positions.value_in_unit(unit.angstrom)
    flattened = [float(value) for xyz in coordinates for value in xyz]
    if box_vectors is None:
        raise MDEquilibrateError("Cannot write a periodic restart without box vectors")
    vectors = [_vector_values(vector, unit.angstrom) for vector in box_vectors]
    lengths = [_norm(vector) for vector in vectors]
    alpha = _angle_degrees(vectors[1], vectors[2])
    beta = _angle_degrees(vectors[0], vectors[2])
    gamma = _angle_degrees(vectors[0], vectors[1])
    lines = ["TBMB/Nectin-4 Stage-4 equilibrated coordinates", f"{len(coordinates):6d}"]
    for start in range(0, len(flattened), 6):
        lines.append("".join(f"{value:12.7f}" for value in flattened[start:start + 6]))
    lines.append("".join(f"{value:12.7f}" for value in [*lengths, alpha, beta, gamma]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _input_records(config: dict[str, Any], base: Path) -> tuple[Path, list[dict[str, Any]]]:
    input_dir = _resolve(config["input_dir"], base)
    summary_path = input_dir / "stage3_minimization_summary.json"
    if not summary_path.is_file():
        raise MDEquilibrateError(f"Stage-3 summary is missing: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "completed":
        raise MDEquilibrateError("Stage-3 summary is not completed")
    candidates = summary.get("candidates") or []
    expected_count = int(config.get("candidate_count", 3))
    if len(candidates) != expected_count:
        raise MDEquilibrateError(
            f"Stage-3 candidate count is {len(candidates)}; expected {expected_count}"
        )
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        rank = int(candidate["rank"])
        if candidate.get("status") != "passed":
            raise MDEquilibrateError(f"Stage-3 rank {rank} is not passed")
        rank_dir = input_dir / f"rank_{rank:02d}"
        manifest_path = rank_dir / "stage3_minimization_manifest.json"
        prmtop = rank_dir / "complex_minimized.prmtop"
        inpcrd = rank_dir / "complex_minimized.inpcrd"
        for path in (manifest_path, prmtop, inpcrd):
            if not path.is_file():
                raise MDEquilibrateError(f"Required Stage-3 input is missing: {path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "passed":
            raise MDEquilibrateError(f"Stage-3 rank manifest is not passed: {rank}")
        if _sha256(prmtop) != manifest.get("output_prmtop_sha256"):
            raise MDEquilibrateError(f"Stage-3 prmtop hash mismatch for rank {rank}")
        if _sha256(inpcrd) != manifest.get("output_inpcrd_sha256"):
            raise MDEquilibrateError(f"Stage-3 inpcrd hash mismatch for rank {rank}")
        if _prmtop_natom(prmtop) != _inpcrd_natom(inpcrd):
            raise MDEquilibrateError(f"Stage-3 topology/coordinate atom count mismatch for rank {rank}")
        records.append({
            "rank": rank,
            "ranked_candidate": candidate.get("ranked_candidate"),
            "prmtop": prmtop,
            "inpcrd": inpcrd,
            "manifest": manifest_path,
        })
    return summary_path, records


def _openmm_modules() -> dict[str, Any]:
    try:
        import numpy  # noqa: F401
        import openmm
        from openmm import (
            CMMotionRemover,
            CustomExternalForce,
            LangevinMiddleIntegrator,
            MonteCarloBarostat,
            Platform,
            XmlSerializer,
            unit,
        )
        from openmm.app import (
            PME,
            AmberInpcrdFile,
            AmberPrmtopFile,
            CheckpointReporter,
            DCDReporter,
            HBonds,
            PDBFile,
            Simulation,
        )
    except ImportError as exc:
        raise MDEquilibrateError("OpenMM with AMBER file support is required for Stage 4") from exc
    return {
        "openmm": openmm,
        "CMMotionRemover": CMMotionRemover,
        "CustomExternalForce": CustomExternalForce,
        "LangevinMiddleIntegrator": LangevinMiddleIntegrator,
        "MonteCarloBarostat": MonteCarloBarostat,
        "Platform": Platform,
        "XmlSerializer": XmlSerializer,
        "unit": unit,
        "DCDReporter": DCDReporter,
        "CheckpointReporter": CheckpointReporter,
        "HBonds": HBonds,
        "PME": PME,
        "AmberInpcrdFile": AmberInpcrdFile,
        "AmberPrmtopFile": AmberPrmtopFile,
        "PDBFile": PDBFile,
        "Simulation": Simulation,
    }


def _steps_for_duration(duration_ps: float, timestep_fs: float) -> int:
    raw = duration_ps * 1000.0 / timestep_fs
    rounded = round(raw)
    if rounded <= 0 or not math.isclose(raw, rounded, rel_tol=0.0, abs_tol=1e-7):
        raise MDEquilibrateError(
            f"Duration {duration_ps} ps is not a positive integer number of {timestep_fs} fs steps"
        )
    return rounded


def _box_volume_nm3(box_vectors: Any, unit: Any) -> float:
    import numpy as np

    matrix = np.asarray(
        [_vector_values(vector, unit.nanometer) for vector in box_vectors], dtype=float
    )
    return abs(float(np.linalg.det(matrix)))


def _degrees_of_freedom(system: Any, modules: dict[str, Any]) -> int:
    dof = 0
    for index in range(system.getNumParticles()):
        mass = system.getParticleMass(index).value_in_unit(modules["unit"].dalton)
        if float(mass) > 0:
            dof += 3
    dof -= system.getNumConstraints()
    if any(
        isinstance(system.getForce(index), modules["CMMotionRemover"])
        for index in range(system.getNumForces())
    ):
        dof -= 3
    if dof <= 0:
        raise MDEquilibrateError("Calculated non-positive dynamics degrees of freedom")
    return dof


def _total_mass_dalton(system: Any, unit: Any) -> float:
    return sum(
        float(system.getParticleMass(index).value_in_unit(unit.dalton))
        for index in range(system.getNumParticles())
    )


def _sample_state(
    simulation: Any,
    modules: dict[str, Any],
    phase: str,
    target_temperature: float,
    restraint: float,
    degrees_of_freedom: int,
    total_mass_dalton: float,
) -> dict[str, Any]:
    unit = modules["unit"]
    state = simulation.context.getState(getEnergy=True)
    potential = float(state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole))
    kinetic = float(state.getKineticEnergy().value_in_unit(unit.kilojoules_per_mole))
    box_vectors = state.getPeriodicBoxVectors()
    volume = _box_volume_nm3(box_vectors, unit)
    gas_constant = 0.00831446261815324  # kJ mol^-1 K^-1
    measured_temperature = 2.0 * kinetic / (degrees_of_freedom * gas_constant)
    density = total_mass_dalton * 0.00166053906660 / volume
    values = [potential, kinetic, volume, measured_temperature, density]
    if not all(math.isfinite(value) for value in values):
        raise MDEquilibrateError(f"Non-finite thermodynamic value during {phase}")
    return {
        "step": int(simulation.currentStep),
        "time_ps": float(state.getTime().value_in_unit(unit.picosecond)),
        "phase": phase,
        "target_temperature_kelvin": target_temperature,
        "restraint_kcal_mol_a2": restraint,
        "potential_energy_kj_mol": potential,
        "kinetic_energy_kj_mol": kinetic,
        "total_energy_kj_mol": potential + kinetic,
        "temperature_kelvin": measured_temperature,
        "volume_nm3": volume,
        "density_g_ml": density,
    }


def _run_sampled(
    simulation: Any,
    system: Any,
    modules: dict[str, Any],
    steps: int,
    report_interval: int,
    phase: str,
    target_temperature: float,
    restraint: float,
    writer: Any,
    records: list[dict[str, Any]],
) -> None:
    degrees_of_freedom = _degrees_of_freedom(system, modules)
    total_mass_dalton = _total_mass_dalton(system, modules["unit"])
    remaining = steps
    while remaining > 0:
        block = min(remaining, report_interval)
        simulation.step(block)
        record = _sample_state(
            simulation,
            modules,
            phase,
            target_temperature,
            restraint,
            degrees_of_freedom,
            total_mass_dalton,
        )
        writer.writerow(record)
        records.append(record)
        remaining -= block


def _tail_statistics(records: list[dict[str, Any]], fraction: float = 0.5) -> dict[str, float]:
    if not records:
        raise MDEquilibrateError("No thermodynamic samples were recorded")
    if not 0 < fraction <= 1:
        raise MDEquilibrateError("Tail fraction must be in (0, 1]")
    start = max(0, len(records) - max(1, math.ceil(len(records) * fraction)))
    tail = records[start:]

    def stats(key: str) -> tuple[float, float]:
        values = [float(record[key]) for record in tail]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return mean, math.sqrt(variance)

    temperature_mean, temperature_sd = stats("temperature_kelvin")
    density_mean, density_sd = stats("density_g_ml")
    volume_mean, volume_sd = stats("volume_nm3")
    return {
        "sample_count": len(tail),
        "temperature_mean_kelvin": temperature_mean,
        "temperature_sd_kelvin": temperature_sd,
        "density_mean_g_ml": density_mean,
        "density_sd_g_ml": density_sd,
        "volume_mean_nm3": volume_mean,
        "volume_sd_nm3": volume_sd,
        "volume_cv": volume_sd / volume_mean,
    }


def _add_restraint(
    system: Any,
    topology: Any,
    reference_positions: Any,
    solute_residue_count: int,
    modules: dict[str, Any],
) -> int:
    unit = modules["unit"]
    force = modules["CustomExternalForce"](
        "0.5*k_pos*periodicdistance(x,y,z,x0,y0,z0)^2"
    )
    force.addGlobalParameter("k_pos", 0.0)
    for name in ("x0", "y0", "z0"):
        force.addPerParticleParameter(name)
    reference_nm = reference_positions.value_in_unit(unit.nanometer)
    count = 0
    for atom in topology.atoms():
        is_hydrogen = atom.element is not None and atom.element.symbol == "H"
        if atom.residue.index < solute_residue_count and not is_hydrogen:
            force.addParticle(atom.index, [float(value) for value in reference_nm[atom.index]])
            count += 1
    system.addForce(force)
    return count


def _build_system(
    prmtop: Any,
    reference_positions: Any,
    config: dict[str, Any],
    modules: dict[str, Any],
    with_barostat: bool,
    seed: int,
) -> tuple[Any, int]:
    unit = modules["unit"]
    dynamics = config["dynamics"]
    system = prmtop.createSystem(
        nonbondedMethod=modules["PME"],
        nonbondedCutoff=float(dynamics["nonbonded_cutoff_nm"]) * unit.nanometer,
        constraints=modules["HBonds"],
        rigidWater=True,
        removeCMMotion=True,
        ewaldErrorTolerance=float(dynamics["ewald_error_tolerance"]),
    )
    restrained_atoms = _add_restraint(
        system,
        prmtop.topology,
        reference_positions,
        int(config["system"]["solute_residue_count"]),
        modules,
    )
    if with_barostat:
        npt = dynamics["npt"]
        barostat = modules["MonteCarloBarostat"](
            float(npt["pressure_bar"]) * unit.bar,
            float(dynamics["target_temperature_kelvin"]) * unit.kelvin,
            int(npt["barostat_frequency_steps"]),
        )
        barostat.setRandomNumberSeed(seed)
        system.addForce(barostat)
    return system, restrained_atoms


def _new_simulation(
    topology: Any,
    system: Any,
    temperature: float,
    config: dict[str, Any],
    modules: dict[str, Any],
    seed: int,
) -> tuple[Any, Any, Any, dict[str, str]]:
    unit = modules["unit"]
    dynamics = config["dynamics"]
    integrator = modules["LangevinMiddleIntegrator"](
        temperature * unit.kelvin,
        float(dynamics["friction_per_ps"]) / unit.picosecond,
        float(dynamics["timestep_fs"]) * unit.femtoseconds,
    )
    integrator.setConstraintTolerance(float(dynamics["constraint_tolerance"]))
    integrator.setRandomNumberSeed(seed)
    platform = modules["Platform"].getPlatformByName(
        str(config["runtime"].get("platform", "CUDA"))
    )
    properties = {
        str(key): str(value)
        for key, value in (config["runtime"].get("platform_properties") or {}).items()
    }
    simulation = modules["Simulation"](
        topology, system, integrator, platform, properties
    )
    return simulation, integrator, platform, properties


def _atom_label(atom: Any) -> str:
    return f"{atom.residue.name}:{atom.residue.index + 1}:{atom.name}:{atom.index + 1}"


def _distance_angstrom(first: Any, second: Any, unit: Any) -> float:
    left = _vector_values(first, unit.angstrom)
    right = _vector_values(second, unit.angstrom)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def _monitored_bond_geometry(
    topology: Any,
    positions: Any,
    records: Iterable[dict[str, Any]],
    unit: Any,
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
            raise MDEquilibrateError(f"Monitored bond atom is missing: {first_key}--{second_key}")
        first = atom_lookup[first_key]
        second = atom_lookup[second_key]
        distance = _distance_angstrom(positions[first.index], positions[second.index], unit)
        minimum = float(record["minimum_angstrom"])
        maximum = float(record["maximum_angstrom"])
        bonded = tuple(sorted((first.index, second.index))) in topology_bonds
        results.append({
            "name": str(record["name"]),
            "first": _atom_label(first),
            "second": _atom_label(second),
            "is_bonded_in_topology": bonded,
            "distance_angstrom": distance,
            "minimum_angstrom": minimum,
            "maximum_angstrom": maximum,
            "passed": bonded and minimum <= distance <= maximum,
        })
    return results


def _minimum_solute_contact(
    topology: Any,
    positions: Any,
    solute_residue_count: int,
    unit: Any,
) -> dict[str, Any]:
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
            distance = math.sqrt(
                sum((float(a) - float(b)) ** 2 for a, b in zip(left_xyz, right_xyz))
            )
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
        raise MDEquilibrateError("Unable to calculate solute contact distances")
    return {
        "minimum_all_atom_non_direct_bonded_angstrom": best_all[0],
        "minimum_all_atom_pair": [_atom_label(best_all[1]), _atom_label(best_all[2])],
        "minimum_heavy_atom_non_direct_bonded_angstrom": best_heavy[0],
        "minimum_heavy_atom_pair": [_atom_label(best_heavy[1]), _atom_label(best_heavy[2])],
        "definition": "solute pairs excluding only directly bonded 1-2 pairs; 1-3 geometry retained",
    }


def _relative_structure_metrics(
    topology: Any,
    reference_positions: Any,
    final_positions: Any,
    receptor_residue_count: int,
    solute_residue_count: int,
    unit: Any,
) -> dict[str, Any]:
    import numpy as np

    atoms = list(topology.atoms())
    backbone = [
        atom.index
        for atom in atoms
        if atom.residue.index < receptor_residue_count and atom.name in {"N", "CA", "C"}
    ]
    receptor_heavy = [
        atom.index
        for atom in atoms
        if atom.residue.index < receptor_residue_count
        and atom.element is not None
        and atom.element.symbol != "H"
    ]
    ligand_heavy = [
        atom.index
        for atom in atoms
        if receptor_residue_count <= atom.residue.index < solute_residue_count
        and atom.element is not None
        and atom.element.symbol != "H"
    ]
    if len(backbone) < 3 or not receptor_heavy or not ligand_heavy:
        raise MDEquilibrateError("Insufficient receptor/ligand atoms for structural metrics")
    reference = np.asarray(reference_positions.value_in_unit(unit.angstrom), dtype=float)
    final = np.asarray(final_positions.value_in_unit(unit.angstrom), dtype=float)
    moving = final[backbone]
    target = reference[backbone]
    moving_center = moving.mean(axis=0)
    target_center = target.mean(axis=0)
    covariance = (moving - moving_center).T @ (target - target_center)
    left, _singular, right_t = np.linalg.svd(covariance)
    rotation = left @ right_t
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right_t
    aligned = (final - moving_center) @ rotation + target_center

    def rmsd(indices: list[int]) -> float:
        delta = aligned[indices] - reference[indices]
        return float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))

    def interface_contacts(coordinates: Any, cutoff: float = 4.5) -> int:
        receptor = coordinates[receptor_heavy]
        ligand = coordinates[ligand_heavy]
        squared = np.sum((receptor[:, None, :] - ligand[None, :, :]) ** 2, axis=2)
        return int(np.count_nonzero(squared <= cutoff * cutoff))

    initial_contacts = interface_contacts(reference)
    final_contacts = interface_contacts(aligned)
    return {
        "receptor_backbone_rmsd_angstrom": rmsd(backbone),
        "ligand_heavy_atom_rmsd_after_receptor_alignment_angstrom": rmsd(ligand_heavy),
        "interface_heavy_atom_contacts_initial_4p5a": initial_contacts,
        "interface_heavy_atom_contacts_final_4p5a": final_contacts,
        "interface_contact_retention_fraction": (
            final_contacts / initial_contacts if initial_contacts else None
        ),
        "note": "RMSD/contact metrics are diagnostics, not affinity scores or hard pass gates",
    }


def preflight(config: dict[str, Any], base: Path) -> dict[str, Any]:
    modules = _openmm_modules()
    summary_path, records = _input_records(config, base)
    platform_name = str(config["runtime"].get("platform", "CUDA"))
    try:
        platform = modules["Platform"].getPlatformByName(platform_name)
    except Exception as exc:
        raise MDEquilibrateError(f"Requested OpenMM platform is unavailable: {platform_name}") from exc
    timestep = float(config["dynamics"]["timestep_fs"])
    _steps_for_duration(
        float(config["dynamics"]["nvt"]["heating_duration_ps"]), timestep
    )
    _steps_for_duration(
        float(config["dynamics"]["nvt"]["hold_duration_ps"]), timestep
    )
    for stage in config["dynamics"]["npt"].get("stages") or []:
        _steps_for_duration(float(stage["duration_ps"]), timestep)
    output_dir = _resolve(config["output_dir"], base)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "pass",
        "created_at_utc": _utc_now(),
        "stage3_summary": str(summary_path),
        "stage3_summary_sha256": _sha256(summary_path),
        "candidate_count": len(records),
        "openmm_version": getattr(modules["openmm"], "__version__", "unknown"),
        "platform": platform.getName(),
        "input_atom_counts": {
            str(item["rank"]): _prmtop_natom(item["prmtop"]) for item in records
        },
    }
    _write_json(output_dir / "preflight.json", result)
    return result


def _equilibrate_one(
    config: dict[str, Any],
    record: dict[str, Any],
    output_dir: Path,
    modules: dict[str, Any],
) -> dict[str, Any]:
    unit = modules["unit"]
    rank = int(record["rank"])
    rank_dir = output_dir / f"rank_{rank:02d}"
    rank_dir.mkdir(parents=True, exist_ok=True)
    prmtop = modules["AmberPrmtopFile"](str(record["prmtop"]))
    inpcrd = modules["AmberInpcrdFile"](str(record["inpcrd"]))
    if inpcrd.boxVectors is None:
        raise MDEquilibrateError(f"Rank {rank} coordinate file lacks periodic box vectors")
    prmtop.topology.setPeriodicBoxVectors(inpcrd.boxVectors)
    dynamics = config["dynamics"]
    target_temperature = float(dynamics["target_temperature_kelvin"])
    timestep_fs = float(dynamics["timestep_fs"])
    report_interval = int(dynamics["report_interval_steps"])
    trajectory_interval = int(dynamics["trajectory_interval_steps"])
    checkpoint_interval = int(dynamics["checkpoint_interval_steps"])
    seed = int(dynamics["random_seed_base"]) + rank
    conversion = 4.184 / 0.01
    _progress(f"Rank {rank}: starting Stage-4 equilibration")

    nvt_system, restrained_atoms = _build_system(
        prmtop, inpcrd.positions, config, modules, with_barostat=False, seed=seed + 1000
    )
    nvt = dynamics["nvt"]
    initial_temperature = float(nvt["initial_temperature_kelvin"])
    nvt_sim, nvt_integrator, platform, properties = _new_simulation(
        prmtop.topology,
        nvt_system,
        initial_temperature,
        config,
        modules,
        seed,
    )
    nvt_sim.context.setPeriodicBoxVectors(*inpcrd.boxVectors)
    nvt_sim.context.setPositions(inpcrd.positions)
    nvt_sim.context.applyConstraints(float(dynamics["constraint_tolerance"]))
    nvt_sim.context.setVelocitiesToTemperature(initial_temperature * unit.kelvin, seed)
    nvt_sim.context.applyVelocityConstraints(float(dynamics["constraint_tolerance"]))
    nvt_restraint = float(nvt["restraint_kcal_mol_a2"])
    nvt_sim.context.setParameter("k_pos", nvt_restraint * conversion)
    nvt_sim.reporters.append(
        modules["DCDReporter"](
            str(rank_dir / "nvt_trajectory.dcd"), trajectory_interval, enforcePeriodicBox=False
        )
    )
    nvt_sim.reporters.append(
        modules["CheckpointReporter"](str(rank_dir / "nvt_restart.chk"), checkpoint_interval)
    )

    fieldnames = [
        "step", "time_ps", "phase", "target_temperature_kelvin",
        "restraint_kcal_mol_a2", "potential_energy_kj_mol",
        "kinetic_energy_kj_mol", "total_energy_kj_mol", "temperature_kelvin",
        "volume_nm3", "density_g_ml",
    ]
    nvt_records: list[dict[str, Any]] = []
    nvt_log = rank_dir / "nvt_thermodynamics.csv"
    heating_steps = _steps_for_duration(float(nvt["heating_duration_ps"]), timestep_fs)
    hold_steps = _steps_for_duration(float(nvt["hold_duration_ps"]), timestep_fs)
    segments = int(nvt["temperature_segments"])
    if segments <= 0 or heating_steps % segments != 0:
        raise MDEquilibrateError("NVT heating steps must divide evenly across temperature segments")
    nvt_hold_records: list[dict[str, Any]] = []
    with nvt_log.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for segment in range(1, segments + 1):
            fraction = segment / segments
            temperature = initial_temperature + fraction * (
                target_temperature - initial_temperature
            )
            nvt_integrator.setTemperature(temperature * unit.kelvin)
            _progress(
                f"Rank {rank}: NVT heating segment {segment}/{segments}, "
                f"target {temperature:.2f} K"
            )
            _run_sampled(
                nvt_sim,
                nvt_system,
                modules,
                heating_steps // segments,
                report_interval,
                f"nvt_heat_{segment:02d}",
                temperature,
                nvt_restraint,
                writer,
                nvt_records,
            )
        nvt_integrator.setTemperature(target_temperature * unit.kelvin)
        _progress(f"Rank {rank}: NVT target-temperature hold")
        _run_sampled(
            nvt_sim,
            nvt_system,
            modules,
            hold_steps,
            report_interval,
            "nvt_hold",
            target_temperature,
            nvt_restraint,
            writer,
            nvt_hold_records,
        )
        nvt_records.extend(nvt_hold_records)
    nvt_state = nvt_sim.context.getState(
        getEnergy=True, getPositions=True, getVelocities=True, enforcePeriodicBox=False
    )
    nvt_positions = nvt_state.getPositions()
    nvt_velocities = nvt_state.getVelocities()
    nvt_box = nvt_state.getPeriodicBoxVectors()
    (rank_dir / "nvt_final_state.xml").write_text(
        modules["XmlSerializer"].serialize(nvt_state) + "\n", encoding="utf-8"
    )
    (rank_dir / "nvt_final.chk").write_bytes(nvt_sim.context.createCheckpoint())
    nvt_tail = _tail_statistics(nvt_hold_records, fraction=0.5)
    _progress(
        f"Rank {rank}: NVT completed; tail mean temperature "
        f"{nvt_tail['temperature_mean_kelvin']:.2f} K"
    )

    # Release the NVT CUDA Context before allocating the NPT Context.  Keeping
    # both alive needlessly doubles device memory use for the largest rank.
    del nvt_state, nvt_sim, nvt_integrator, nvt_system
    gc.collect()

    npt_system, npt_restrained_atoms = _build_system(
        prmtop, inpcrd.positions, config, modules, with_barostat=True, seed=seed + 2000
    )
    if npt_restrained_atoms != restrained_atoms:
        raise MDEquilibrateError("NVT/NPT restrained atom counts differ")
    npt_sim, _npt_integrator, _platform, _properties = _new_simulation(
        prmtop.topology,
        npt_system,
        target_temperature,
        config,
        modules,
        seed + 3000,
    )
    npt_sim.context.setPeriodicBoxVectors(*nvt_box)
    npt_sim.context.setPositions(nvt_positions)
    npt_sim.context.applyConstraints(float(dynamics["constraint_tolerance"]))
    npt_sim.context.setVelocities(nvt_velocities)
    npt_sim.context.applyVelocityConstraints(float(dynamics["constraint_tolerance"]))
    npt_sim.reporters.append(
        modules["DCDReporter"](
            str(rank_dir / "npt_trajectory.dcd"), trajectory_interval, enforcePeriodicBox=False
        )
    )
    npt_sim.reporters.append(
        modules["CheckpointReporter"](str(rank_dir / "npt_restart.chk"), checkpoint_interval)
    )

    npt_records: list[dict[str, Any]] = []
    npt_stage_records: list[dict[str, Any]] = []
    npt_log = rank_dir / "npt_thermodynamics.csv"
    npt_stages = dynamics["npt"].get("stages") or []
    if not npt_stages or float(npt_stages[-1]["restraint_kcal_mol_a2"]) != 0.0:
        raise MDEquilibrateError("The final NPT stage must be unrestrained (0.0)")
    with npt_log.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for stage_index, stage in enumerate(npt_stages, 1):
            restraint = float(stage["restraint_kcal_mol_a2"])
            npt_sim.context.setParameter("k_pos", restraint * conversion)
            steps = _steps_for_duration(float(stage["duration_ps"]), timestep_fs)
            _progress(
                f"Rank {rank}: NPT stage {stage_index}/{len(npt_stages)}, "
                f"restraint {restraint:g} kcal mol^-1 A^-2, "
                f"duration {float(stage['duration_ps']):g} ps"
            )
            before = len(npt_records)
            _run_sampled(
                npt_sim,
                npt_system,
                modules,
                steps,
                report_interval,
                f"npt_{stage_index:02d}",
                target_temperature,
                restraint,
                writer,
                npt_records,
            )
            stage_samples = npt_records[before:]
            stage_stats = _tail_statistics(stage_samples, fraction=0.5)
            npt_stage_records.append({
                "stage": stage_index,
                "duration_ps": float(stage["duration_ps"]),
                "restraint_kcal_mol_a2": restraint,
                "steps": steps,
                "tail_statistics": stage_stats,
            })

    final_state = npt_sim.context.getState(
        getEnergy=True,
        getPositions=True,
        getVelocities=True,
        enforcePeriodicBox=False,
    )
    final_positions = final_state.getPositions()
    final_box = final_state.getPeriodicBoxVectors()
    final_energy = float(
        final_state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)
    )
    final_stage_name = f"npt_{len(npt_stages):02d}"
    final_stage_samples = [record for record in npt_records if record["phase"] == final_stage_name]
    npt_tail = _tail_statistics(final_stage_samples, fraction=0.5)

    bonds = _monitored_bond_geometry(
        prmtop.topology,
        final_positions,
        config["system"].get("monitored_bonds") or [],
        unit,
    )
    contacts = _minimum_solute_contact(
        prmtop.topology,
        final_positions,
        int(config["system"]["solute_residue_count"]),
        unit,
    )
    structure_metrics = _relative_structure_metrics(
        prmtop.topology,
        inpcrd.positions,
        final_positions,
        int(config["system"]["receptor_residue_count"]),
        int(config["system"]["solute_residue_count"]),
        unit,
    )
    expected_counts = {
        str(key): int(value)
        for key, value in (
            config["system"].get("expected_special_residue_counts") or {}
        ).items()
    }
    special_counts = _special_residue_counts(record["prmtop"], expected_counts)
    gates = config["validation"]
    failures: list[str] = []
    if not math.isfinite(final_energy):
        failures.append("final NPT potential energy is non-finite")
    if not (
        float(gates["minimum_temperature_kelvin"])
        <= nvt_tail["temperature_mean_kelvin"]
        <= float(gates["maximum_temperature_kelvin"])
    ):
        failures.append("NVT tail mean temperature is outside the configured gate")
    if not (
        float(gates["minimum_temperature_kelvin"])
        <= npt_tail["temperature_mean_kelvin"]
        <= float(gates["maximum_temperature_kelvin"])
    ):
        failures.append("NPT tail mean temperature is outside the configured gate")
    if not (
        float(gates["minimum_density_g_ml"])
        <= npt_tail["density_mean_g_ml"]
        <= float(gates["maximum_density_g_ml"])
    ):
        failures.append("NPT tail mean density is outside the configured gate")
    if npt_tail["volume_cv"] > float(gates["maximum_volume_cv"]):
        failures.append("NPT tail volume coefficient of variation exceeds the configured gate")
    if contacts["minimum_all_atom_non_direct_bonded_angstrom"] < float(
        gates["minimum_all_atom_contact_angstrom"]
    ):
        failures.append("final all-atom solute contact is below the configured gate")
    if contacts["minimum_heavy_atom_non_direct_bonded_angstrom"] < float(
        gates["minimum_heavy_atom_contact_angstrom"]
    ):
        failures.append("final heavy-atom solute contact is below the configured gate")
    if any(not item["passed"] for item in bonds):
        failures.append("one or more monitored covalent bonds failed geometry/topology validation")
    if special_counts != dict(sorted(expected_counts.items())):
        failures.append(f"special residue counts differ: {special_counts} != {expected_counts}")

    output_prmtop = rank_dir / "complex_equilibrated.prmtop"
    output_inpcrd = rank_dir / "complex_equilibrated.inpcrd"
    output_pdb = rank_dir / "complex_equilibrated.pdb"
    output_state = rank_dir / "equilibrated_state.xml"
    output_checkpoint = rank_dir / "equilibrated.chk"
    shutil.copy2(record["prmtop"], output_prmtop)
    _write_amber_inpcrd(output_inpcrd, final_positions, final_box, unit)
    with output_pdb.open("w", encoding="utf-8") as handle:
        modules["PDBFile"].writeFile(
            prmtop.topology, final_positions, handle, keepIds=True
        )
    output_state.write_text(
        modules["XmlSerializer"].serialize(final_state) + "\n", encoding="utf-8"
    )
    output_checkpoint.write_bytes(npt_sim.context.createCheckpoint())
    if _prmtop_natom(output_prmtop) != _inpcrd_natom(output_inpcrd):
        failures.append("output topology/coordinate atom count mismatch")

    result = {
        "status": "passed" if not failures else "failed",
        "created_at_utc": _utc_now(),
        "rank": rank,
        "ranked_candidate": record.get("ranked_candidate"),
        "input_prmtop": str(record["prmtop"]),
        "input_prmtop_sha256": _sha256(record["prmtop"]),
        "input_inpcrd": str(record["inpcrd"]),
        "input_inpcrd_sha256": _sha256(record["inpcrd"]),
        "platform": platform.getName(),
        "platform_properties": properties,
        "random_seed": seed,
        "periodic_method": "PME",
        "constraints": "HBonds",
        "rigid_water": True,
        "timestep_fs": timestep_fs,
        "target_temperature_kelvin": target_temperature,
        "target_pressure_bar": float(dynamics["npt"]["pressure_bar"]),
        "restrained_solute_heavy_atoms": restrained_atoms,
        "nvt_heating_duration_ps": float(nvt["heating_duration_ps"]),
        "nvt_hold_duration_ps": float(nvt["hold_duration_ps"]),
        "nvt_total_duration_ps": (
            float(nvt["heating_duration_ps"]) + float(nvt["hold_duration_ps"])
        ),
        "nvt_tail_statistics": nvt_tail,
        "npt_total_duration_ps": sum(float(stage["duration_ps"]) for stage in npt_stages),
        "npt_stages": npt_stage_records,
        "npt_final_unrestrained_tail_statistics": npt_tail,
        "final_potential_energy_kj_mol": final_energy,
        "final_box_volume_nm3": _box_volume_nm3(final_box, unit),
        "solute_contacts": contacts,
        "monitored_bonds": bonds,
        "special_residue_counts": special_counts,
        "relative_structure_metrics": structure_metrics,
        "validation_failures": failures,
        "nvt_thermodynamics_csv": str(nvt_log),
        "nvt_thermodynamics_csv_sha256": _sha256(nvt_log),
        "npt_thermodynamics_csv": str(npt_log),
        "npt_thermodynamics_csv_sha256": _sha256(npt_log),
        "nvt_trajectory_dcd": str(rank_dir / "nvt_trajectory.dcd"),
        "npt_trajectory_dcd": str(rank_dir / "npt_trajectory.dcd"),
        "output_prmtop": str(output_prmtop),
        "output_prmtop_sha256": _sha256(output_prmtop),
        "output_inpcrd": str(output_inpcrd),
        "output_inpcrd_sha256": _sha256(output_inpcrd),
        "output_pdb": str(output_pdb),
        "output_pdb_sha256": _sha256(output_pdb),
        "output_state_xml": str(output_state),
        "output_state_xml_sha256": _sha256(output_state),
        "output_checkpoint": str(output_checkpoint),
        "output_checkpoint_sha256": _sha256(output_checkpoint),
    }
    _write_json(rank_dir / "stage4_equilibration_manifest.json", result)
    if failures:
        raise MDEquilibrateError(
            f"Rank {rank} Stage-4 validation failed: " + "; ".join(failures)
        )
    _progress(
        f"Rank {rank}: passed; NPT tail density {npt_tail['density_mean_g_ml']:.4f} g/mL, "
        f"temperature {npt_tail['temperature_mean_kelvin']:.2f} K"
    )
    return result


def equilibrate(
    config: dict[str, Any], base: Path, overwrite: bool = False
) -> dict[str, Any]:
    modules = _openmm_modules()
    summary_path, records = _input_records(config, base)
    output_dir = _resolve(config["output_dir"], base)
    summary_output = output_dir / "stage4_equilibration_summary.json"
    if summary_output.exists() and not overwrite:
        existing = json.loads(summary_output.read_text(encoding="utf-8"))
        if existing.get("status") == "completed":
            raise MDEquilibrateError(
                f"Completed Stage-4 summary already exists: {summary_output}; use --overwrite to rerun"
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for record in records:
        try:
            results.append(_equilibrate_one(config, record, output_dir, modules))
        except MDEquilibrateError as exc:
            failed_manifest = (
                output_dir
                / f"rank_{record['rank']:02d}"
                / "stage4_equilibration_manifest.json"
            )
            if failed_manifest.is_file():
                results.append(json.loads(failed_manifest.read_text(encoding="utf-8")))
            failed_summary = {
                "status": "failed",
                "created_at_utc": _utc_now(),
                "source_stage3_summary": str(summary_path),
                "source_stage3_summary_sha256": _sha256(summary_path),
                "candidate_count_completed_or_attempted": len(results),
                "failed_rank": record["rank"],
                "error": str(exc),
                "candidates": results,
                "next_stage": "blocked_until_stage4_validation_passes",
            }
            _write_json(summary_output, failed_summary)
            raise
    summary = {
        "status": "completed",
        "created_at_utc": _utc_now(),
        "source_stage3_summary": str(summary_path),
        "source_stage3_summary_sha256": _sha256(summary_path),
        "candidate_count": len(results),
        "candidates": results,
        "next_stage": "replicated_production_md",
    }
    _write_json(summary_output, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["preflight", "equilibrate", "all"])
    parser.add_argument("--config", required=True, help="YAML or JSON Stage-4 configuration")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of an existing Stage-4 summary",
    )
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = _load_mapping(config_path)
    try:
        if args.stage == "preflight":
            result = preflight(config, config_path.parent)
        elif args.stage == "equilibrate":
            result = equilibrate(config, config_path.parent, overwrite=args.overwrite)
        else:
            preflight(config, config_path.parent)
            result = equilibrate(config, config_path.parent, overwrite=args.overwrite)
    except MDEquilibrateError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
