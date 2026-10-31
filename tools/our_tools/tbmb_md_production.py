#!/usr/bin/env python3
"""Run restartable replicated production MD for TBMB/Nectin-4 candidates.

Stage 5 consumes the three validated Stage-4 equilibrated AMBER systems.  It
creates three independently seeded replicas per candidate, performs a short
unrestrained NPT velocity-settling run, then executes 20 ns of NPT production
in hash-bound 1 ns segments.  Segment manifests and checkpoints make the run
restartable without trusting incomplete trajectory files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .tbmb_md_equilibrate import (
        _inpcrd_natom,
        _minimum_solute_contact,
        _monitored_bond_geometry,
        _prmtop_natom,
        _relative_structure_metrics,
        _special_residue_counts,
        _write_amber_inpcrd,
    )
else:
    from tbmb_md_equilibrate import (  # type: ignore
        _inpcrd_natom,
        _minimum_solute_contact,
        _monitored_bond_geometry,
        _prmtop_natom,
        _relative_structure_metrics,
        _special_residue_counts,
        _write_amber_inpcrd,
    )


class MDProductionError(RuntimeError):
    """A deterministic input, restart, production, or validation failure."""


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise MDProductionError("PyYAML is required for the Stage-5 YAML configuration") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise MDProductionError(f"Expected one mapping in {path}")
    return data


def _resolve(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _steps_for_duration(duration_ns: float, timestep_fs: float) -> int:
    raw = duration_ns * 1_000_000.0 / timestep_fs
    rounded = round(raw)
    if rounded <= 0 or not math.isclose(raw, rounded, rel_tol=0.0, abs_tol=1e-7):
        raise MDProductionError(
            f"Duration {duration_ns} ns is not a positive integer number of {timestep_fs} fs steps"
        )
    return rounded


def _input_records(config: dict[str, Any], base: Path) -> tuple[Path, list[dict[str, Any]]]:
    input_dir = _resolve(config["input_dir"], base)
    summary_path = input_dir / "stage4_equilibration_summary.json"
    if not summary_path.is_file():
        raise MDProductionError(f"Stage-4 summary is missing: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "completed":
        raise MDProductionError("Stage-4 summary is not completed")
    candidates = summary.get("candidates") or []
    expected_count = int(config.get("candidate_count", 3))
    if len(candidates) != expected_count:
        raise MDProductionError(
            f"Stage-4 candidate count is {len(candidates)}; expected {expected_count}"
        )
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        rank = int(candidate["rank"])
        if candidate.get("status") != "passed":
            raise MDProductionError(f"Stage-4 rank {rank} is not passed")
        rank_dir = input_dir / f"rank_{rank:02d}"
        manifest_path = rank_dir / "stage4_equilibration_manifest.json"
        prmtop = rank_dir / "complex_equilibrated.prmtop"
        inpcrd = rank_dir / "complex_equilibrated.inpcrd"
        for path in (manifest_path, prmtop, inpcrd):
            if not path.is_file():
                raise MDProductionError(f"Required Stage-4 input is missing: {path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "passed":
            raise MDProductionError(f"Stage-4 rank manifest is not passed: {rank}")
        if _sha256(prmtop) != manifest.get("output_prmtop_sha256"):
            raise MDProductionError(f"Stage-4 prmtop hash mismatch for rank {rank}")
        if _sha256(inpcrd) != manifest.get("output_inpcrd_sha256"):
            raise MDProductionError(f"Stage-4 inpcrd hash mismatch for rank {rank}")
        if _prmtop_natom(prmtop) != _inpcrd_natom(inpcrd):
            raise MDProductionError(f"Stage-4 topology/coordinate atom count mismatch for rank {rank}")
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
            DCDReporter,
            HBonds,
            PDBFile,
            Simulation,
            StateDataReporter,
        )
    except ImportError as exc:
        raise MDProductionError("OpenMM, NumPy, and AMBER file support are required for Stage 5") from exc
    return {
        "openmm": openmm,
        "LangevinMiddleIntegrator": LangevinMiddleIntegrator,
        "MonteCarloBarostat": MonteCarloBarostat,
        "Platform": Platform,
        "XmlSerializer": XmlSerializer,
        "unit": unit,
        "PME": PME,
        "AmberInpcrdFile": AmberInpcrdFile,
        "AmberPrmtopFile": AmberPrmtopFile,
        "DCDReporter": DCDReporter,
        "HBonds": HBonds,
        "PDBFile": PDBFile,
        "Simulation": Simulation,
        "StateDataReporter": StateDataReporter,
    }


def _build_simulation(
    config: dict[str, Any],
    prmtop: Any,
    modules: dict[str, Any],
    integrator_seed: int,
    barostat_seed: int,
) -> tuple[Any, Any, Any, dict[str, str]]:
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
    barostat = modules["MonteCarloBarostat"](
        float(dynamics["pressure_bar"]) * unit.bar,
        float(dynamics["temperature_kelvin"]) * unit.kelvin,
        int(dynamics["barostat_frequency_steps"]),
    )
    barostat.setRandomNumberSeed(barostat_seed)
    system.addForce(barostat)
    integrator = modules["LangevinMiddleIntegrator"](
        float(dynamics["temperature_kelvin"]) * unit.kelvin,
        float(dynamics["friction_per_ps"]) / unit.picosecond,
        float(dynamics["timestep_fs"]) * unit.femtoseconds,
    )
    integrator.setConstraintTolerance(float(dynamics["constraint_tolerance"]))
    integrator.setRandomNumberSeed(integrator_seed)
    platform = modules["Platform"].getPlatformByName(
        str(config["runtime"].get("platform", "CUDA"))
    )
    properties = {
        str(key): str(value)
        for key, value in (config["runtime"].get("platform_properties") or {}).items()
    }
    simulation = modules["Simulation"](
        prmtop.topology, system, integrator, platform, properties
    )
    return simulation, system, platform, properties


def _state_reporter(
    modules: dict[str, Any],
    path: Path,
    interval: int,
) -> Any:
    return modules["StateDataReporter"](
        str(path),
        interval,
        step=True,
        time=True,
        potentialEnergy=True,
        kineticEnergy=True,
        totalEnergy=True,
        temperature=True,
        volume=True,
        density=True,
        speed=True,
        elapsedTime=True,
        separator=",",
        append=False,
    )


def _segment_paths(replica_dir: Path, segment: int) -> dict[str, Path]:
    prefix = f"segment_{segment:03d}"
    return {
        "dcd": replica_dir / f"{prefix}.dcd",
        "csv": replica_dir / f"{prefix}_thermodynamics.csv",
        "checkpoint": replica_dir / f"{prefix}.chk",
        "manifest": replica_dir / f"{prefix}.json",
    }


def _validated_completed_segments(
    replica_dir: Path, segment_count: int, segment_steps: int, settle_steps: int
) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    gap_seen = False
    for segment in range(1, segment_count + 1):
        paths = _segment_paths(replica_dir, segment)
        if not paths["manifest"].is_file():
            gap_seen = True
            continue
        if gap_seen:
            raise MDProductionError(
                f"Non-contiguous segment manifests in {replica_dir}; segment {segment} follows a gap"
            )
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        expected_end = settle_steps + segment * segment_steps
        if manifest.get("status") != "completed" or int(manifest.get("end_step", -1)) != expected_end:
            raise MDProductionError(f"Invalid segment manifest: {paths['manifest']}")
        for key in ("dcd", "csv", "checkpoint"):
            path = paths[key]
            if not path.is_file() or _sha256(path) != manifest.get(f"{key}_sha256"):
                raise MDProductionError(f"Segment {segment} {key} hash mismatch: {path}")
        completed.append(manifest)
    return completed


def _column(fieldnames: list[str], token: str) -> str:
    matches = [name for name in fieldnames if token.lower() in name.lower()]
    if len(matches) != 1:
        raise MDProductionError(f"Expected one '{token}' column, found {matches}")
    return matches[0]


def _production_tail_statistics(csv_paths: list[Path]) -> dict[str, float]:
    rows: list[dict[str, str]] = []
    fieldnames: list[str] | None = None
    for path in csv_paths:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise MDProductionError(f"Thermodynamics CSV has no header: {path}")
            if fieldnames is None:
                fieldnames = reader.fieldnames
            rows.extend(reader)
    if not rows or fieldnames is None:
        raise MDProductionError("No production thermodynamic samples were recorded")
    temperature_key = _column(fieldnames, "Temperature")
    density_key = _column(fieldnames, "Density")
    volume_key = _column(fieldnames, "Box Volume")
    potential_key = _column(fieldnames, "Potential Energy")

    def stats(key: str) -> tuple[float, float]:
        values = [float(row[key]) for row in rows]
        if not all(math.isfinite(value) for value in values):
            raise MDProductionError(f"Non-finite values in thermodynamics column {key}")
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return mean, math.sqrt(variance)

    temperature_mean, temperature_sd = stats(temperature_key)
    density_mean, density_sd = stats(density_key)
    volume_mean, volume_sd = stats(volume_key)
    potential_mean, potential_sd = stats(potential_key)
    return {
        "sample_count": len(rows),
        "temperature_mean_kelvin": temperature_mean,
        "temperature_sd_kelvin": temperature_sd,
        "density_mean_g_ml": density_mean,
        "density_sd_g_ml": density_sd,
        "volume_mean_nm3": volume_mean,
        "volume_sd_nm3": volume_sd,
        "volume_cv": volume_sd / volume_mean,
        "potential_energy_mean_kj_mol": potential_mean,
        "potential_energy_sd_kj_mol": potential_sd,
    }


def _clear_manifests_for_overwrite(replica_dir: Path, segment_count: int) -> None:
    targets = [
        replica_dir / "initialization.json",
        replica_dir / "progress.json",
        replica_dir / "stage5_replica_manifest.json",
    ]
    targets.extend(
        _segment_paths(replica_dir, segment)["manifest"]
        for segment in range(1, segment_count + 1)
    )
    for path in targets:
        if path.is_file():
            path.unlink()


def _initialize_replica(
    simulation: Any,
    inpcrd: Any,
    config: dict[str, Any],
    modules: dict[str, Any],
    replica_dir: Path,
    velocity_seed: int,
    settle_steps: int,
) -> dict[str, Any]:
    unit = modules["unit"]
    dynamics = config["dynamics"]
    simulation.context.setPeriodicBoxVectors(*inpcrd.boxVectors)
    simulation.context.setPositions(inpcrd.positions)
    simulation.context.applyConstraints(float(dynamics["constraint_tolerance"]))
    simulation.context.setVelocitiesToTemperature(
        float(dynamics["temperature_kelvin"]) * unit.kelvin, velocity_seed
    )
    simulation.context.applyVelocityConstraints(float(dynamics["constraint_tolerance"]))
    initialization_csv = replica_dir / "initialization_thermodynamics.csv"
    simulation.reporters = [
        _state_reporter(
            modules,
            initialization_csv,
            int(dynamics["thermodynamics_interval_steps"]),
        )
    ]
    _progress(
        f"{replica_dir.parent.name}/{replica_dir.name}: "
        f"100 ps independent-velocity NPT initialization"
    )
    simulation.step(settle_steps)
    simulation.reporters = []
    checkpoint = replica_dir / "initialized.chk"
    checkpoint.write_bytes(simulation.context.createCheckpoint())
    result = {
        "status": "completed",
        "created_at_utc": _utc_now(),
        "end_step": int(simulation.currentStep),
        "velocity_seed": velocity_seed,
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": _sha256(checkpoint),
        "thermodynamics_csv": str(initialization_csv),
        "thermodynamics_csv_sha256": _sha256(initialization_csv),
    }
    _write_json(replica_dir / "initialization.json", result)
    return result


def _load_or_initialize(
    simulation: Any,
    inpcrd: Any,
    config: dict[str, Any],
    modules: dict[str, Any],
    replica_dir: Path,
    velocity_seed: int,
    settle_steps: int,
    resume: bool,
) -> dict[str, Any]:
    manifest_path = replica_dir / "initialization.json"
    checkpoint = replica_dir / "initialized.chk"
    if resume and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") != "completed"
            or int(manifest.get("end_step", -1)) != settle_steps
            or not checkpoint.is_file()
            or _sha256(checkpoint) != manifest.get("checkpoint_sha256")
        ):
            raise MDProductionError(f"Invalid initialization restart in {replica_dir}")
        simulation.loadCheckpoint(str(checkpoint))
        return manifest
    return _initialize_replica(
        simulation,
        inpcrd,
        config,
        modules,
        replica_dir,
        velocity_seed,
        settle_steps,
    )


def _run_segment(
    simulation: Any,
    modules: dict[str, Any],
    config: dict[str, Any],
    replica_dir: Path,
    segment: int,
    segment_count: int,
    segment_steps: int,
) -> dict[str, Any]:
    dynamics = config["dynamics"]
    paths = _segment_paths(replica_dir, segment)
    start_step = int(simulation.currentStep)
    simulation.reporters = [
        modules["DCDReporter"](
            str(paths["dcd"]),
            int(dynamics["trajectory_interval_steps"]),
            append=False,
            enforcePeriodicBox=False,
        ),
        _state_reporter(
            modules,
            paths["csv"],
            int(dynamics["thermodynamics_interval_steps"]),
        ),
    ]
    _progress(
        f"{replica_dir.parent.name}/{replica_dir.name}: production segment "
        f"{segment}/{segment_count}"
    )
    simulation.step(segment_steps)
    simulation.reporters = []
    paths["checkpoint"].write_bytes(simulation.context.createCheckpoint())
    state = simulation.context.getState(getEnergy=True)
    energy = float(
        state.getPotentialEnergy().value_in_unit(
            modules["unit"].kilojoules_per_mole
        )
    )
    if not math.isfinite(energy):
        raise MDProductionError(
            f"Non-finite potential energy after segment {segment} in {replica_dir}"
        )
    result = {
        "status": "completed",
        "created_at_utc": _utc_now(),
        "segment": segment,
        "start_step": start_step,
        "end_step": int(simulation.currentStep),
        "steps": segment_steps,
        "final_potential_energy_kj_mol": energy,
        "dcd": str(paths["dcd"]),
        "dcd_sha256": _sha256(paths["dcd"]),
        "csv": str(paths["csv"]),
        "csv_sha256": _sha256(paths["csv"]),
        "checkpoint": str(paths["checkpoint"]),
        "checkpoint_sha256": _sha256(paths["checkpoint"]),
    }
    _write_json(paths["manifest"], result)
    return result


def _run_replica(
    config: dict[str, Any],
    record: dict[str, Any],
    replica: int,
    output_dir: Path,
    modules: dict[str, Any],
    resume: bool,
    overwrite: bool,
) -> dict[str, Any]:
    rank = int(record["rank"])
    replica_dir = output_dir / f"rank_{rank:02d}" / f"replica_{replica:02d}"
    replica_dir.mkdir(parents=True, exist_ok=True)
    final_manifest_path = replica_dir / "stage5_replica_manifest.json"
    if final_manifest_path.is_file() and not overwrite:
        existing = json.loads(final_manifest_path.read_text(encoding="utf-8"))
        if existing.get("status") == "passed" and resume:
            _progress(f"rank_{rank:02d}/replica_{replica:02d}: already passed; skipping")
            return existing
        raise MDProductionError(
            f"Replica output already exists: {final_manifest_path}; use --resume or --overwrite"
        )

    timestep_fs = float(config["dynamics"]["timestep_fs"])
    production_ns = float(config["production"]["duration_ns"])
    segment_ns = float(config["production"]["segment_ns"])
    segment_count_float = production_ns / segment_ns
    segment_count = round(segment_count_float)
    if not math.isclose(segment_count_float, segment_count, rel_tol=0.0, abs_tol=1e-9):
        raise MDProductionError("Production duration must be divisible by segment duration")
    segment_steps = _steps_for_duration(segment_ns, timestep_fs)
    settle_steps = _steps_for_duration(
        float(config["production"]["replica_initialization_ns"]), timestep_fs
    )
    if overwrite:
        _clear_manifests_for_overwrite(replica_dir, segment_count)
        resume = False
    elif not resume and any(replica_dir.iterdir()):
        raise MDProductionError(
            f"Incomplete output exists in {replica_dir}; use --resume or --overwrite"
        )

    seed_base = int(config["dynamics"]["random_seed_base"])
    replica_seed = seed_base + rank * 100 + replica
    prmtop = modules["AmberPrmtopFile"](str(record["prmtop"]))
    inpcrd = modules["AmberInpcrdFile"](str(record["inpcrd"]))
    if inpcrd.boxVectors is None:
        raise MDProductionError(f"Rank {rank} input lacks periodic box vectors")
    prmtop.topology.setPeriodicBoxVectors(inpcrd.boxVectors)
    simulation, _system, platform, properties = _build_simulation(
        config,
        prmtop,
        modules,
        integrator_seed=replica_seed,
        barostat_seed=replica_seed + 100_000,
    )
    initialization = _load_or_initialize(
        simulation,
        inpcrd,
        config,
        modules,
        replica_dir,
        velocity_seed=replica_seed + 200_000,
        settle_steps=settle_steps,
        resume=resume,
    )

    completed = _validated_completed_segments(
        replica_dir, segment_count, segment_steps, settle_steps
    ) if resume else []
    if completed:
        last_checkpoint = Path(completed[-1]["checkpoint"])
        if not last_checkpoint.is_file():
            last_checkpoint = _segment_paths(replica_dir, len(completed))["checkpoint"]
        simulation.loadCheckpoint(str(last_checkpoint))
        expected_step = settle_steps + len(completed) * segment_steps
        if int(simulation.currentStep) != expected_step:
            raise MDProductionError(
                f"Checkpoint step mismatch in {replica_dir}: "
                f"{simulation.currentStep} != {expected_step}"
            )

    segment_manifests = list(completed)
    for segment in range(len(completed) + 1, segment_count + 1):
        segment_manifests.append(
            _run_segment(
                simulation,
                modules,
                config,
                replica_dir,
                segment,
                segment_count,
                segment_steps,
            )
        )
        _write_json(replica_dir / "progress.json", {
            "status": "running" if segment < segment_count else "segments_completed",
            "updated_at_utc": _utc_now(),
            "rank": rank,
            "replica": replica,
            "completed_segments": segment,
            "total_segments": segment_count,
            "production_ns_completed": segment * segment_ns,
            "production_ns_target": production_ns,
            "current_step": int(simulation.currentStep),
        })

    unit = modules["unit"]
    final_state = simulation.context.getState(
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
    tail_segment_count = int(config["validation"]["tail_segments"])
    if not 1 <= tail_segment_count <= segment_count:
        raise MDProductionError("validation.tail_segments is outside the segment count")
    tail_csvs = [
        _segment_paths(replica_dir, segment)["csv"]
        for segment in range(segment_count - tail_segment_count + 1, segment_count + 1)
    ]
    tail_stats = _production_tail_statistics(tail_csvs)
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
        failures.append("final production potential energy is non-finite")
    if not (
        float(gates["minimum_temperature_kelvin"])
        <= tail_stats["temperature_mean_kelvin"]
        <= float(gates["maximum_temperature_kelvin"])
    ):
        failures.append("production tail mean temperature is outside the configured gate")
    if not (
        float(gates["minimum_density_g_ml"])
        <= tail_stats["density_mean_g_ml"]
        <= float(gates["maximum_density_g_ml"])
    ):
        failures.append("production tail mean density is outside the configured gate")
    if tail_stats["volume_cv"] > float(gates["maximum_volume_cv"]):
        failures.append("production tail volume coefficient of variation exceeds the gate")
    if contacts["minimum_all_atom_non_direct_bonded_angstrom"] < float(
        gates["minimum_all_atom_contact_angstrom"]
    ):
        failures.append("final all-atom solute contact is below the configured gate")
    if contacts["minimum_heavy_atom_non_direct_bonded_angstrom"] < float(
        gates["minimum_heavy_atom_contact_angstrom"]
    ):
        failures.append("final heavy-atom solute contact is below the configured gate")
    if any(not item["passed"] for item in bonds):
        failures.append("one or more monitored covalent bonds failed validation")
    if special_counts != dict(sorted(expected_counts.items())):
        failures.append(f"special residue counts differ: {special_counts} != {expected_counts}")

    output_prmtop = replica_dir / "complex_production_final.prmtop"
    output_inpcrd = replica_dir / "complex_production_final.inpcrd"
    output_pdb = replica_dir / "complex_production_final.pdb"
    output_state = replica_dir / "production_final_state.xml"
    output_checkpoint = replica_dir / "production_final.chk"
    shutil.copy2(record["prmtop"], output_prmtop)
    _write_amber_inpcrd(output_inpcrd, final_positions, final_box, unit)
    with output_pdb.open("w", encoding="utf-8") as handle:
        modules["PDBFile"].writeFile(
            prmtop.topology, final_positions, handle, keepIds=True
        )
    output_state.write_text(
        modules["XmlSerializer"].serialize(final_state) + "\n", encoding="utf-8"
    )
    output_checkpoint.write_bytes(simulation.context.createCheckpoint())

    result = {
        "status": "passed" if not failures else "failed",
        "created_at_utc": _utc_now(),
        "rank": rank,
        "ranked_candidate": record.get("ranked_candidate"),
        "replica": replica,
        "replica_seed": replica_seed,
        "integrator_seed": replica_seed,
        "barostat_seed": replica_seed + 100_000,
        "velocity_seed": replica_seed + 200_000,
        "platform": platform.getName(),
        "platform_properties": properties,
        "input_prmtop": str(record["prmtop"]),
        "input_prmtop_sha256": _sha256(record["prmtop"]),
        "input_inpcrd": str(record["inpcrd"]),
        "input_inpcrd_sha256": _sha256(record["inpcrd"]),
        "temperature_kelvin": float(config["dynamics"]["temperature_kelvin"]),
        "pressure_bar": float(config["dynamics"]["pressure_bar"]),
        "timestep_fs": timestep_fs,
        "replica_initialization_ns": float(
            config["production"]["replica_initialization_ns"]
        ),
        "production_duration_ns": production_ns,
        "segment_duration_ns": segment_ns,
        "segment_count": segment_count,
        "initialization": initialization,
        "segments": segment_manifests,
        "tail_statistics": tail_stats,
        "final_potential_energy_kj_mol": final_energy,
        "monitored_bonds": bonds,
        "solute_contacts": contacts,
        "special_residue_counts": special_counts,
        "relative_structure_metrics": structure_metrics,
        "validation_failures": failures,
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
    _write_json(final_manifest_path, result)
    if failures:
        raise MDProductionError(
            f"Rank {rank} replica {replica} validation failed: " + "; ".join(failures)
        )
    _write_json(replica_dir / "progress.json", {
        "status": "completed",
        "updated_at_utc": _utc_now(),
        "rank": rank,
        "replica": replica,
        "completed_segments": segment_count,
        "total_segments": segment_count,
        "production_ns_completed": production_ns,
        "production_ns_target": production_ns,
        "current_step": int(simulation.currentStep),
    })
    _progress(
        f"rank_{rank:02d}/replica_{replica:02d}: passed 20 ns production; "
        f"tail T={tail_stats['temperature_mean_kelvin']:.2f} K, "
        f"density={tail_stats['density_mean_g_ml']:.4f} g/mL"
    )
    return result


def _refresh_summary(
    config: dict[str, Any],
    base: Path,
    source_summary: Path,
    error: str | None = None,
) -> dict[str, Any]:
    output_dir = _resolve(config["output_dir"], base)
    candidate_count = int(config["candidate_count"])
    replica_count = int(config["replicas_per_candidate"])
    results: list[dict[str, Any]] = []
    for rank in range(1, candidate_count + 1):
        for replica in range(1, replica_count + 1):
            path = (
                output_dir
                / f"rank_{rank:02d}"
                / f"replica_{replica:02d}"
                / "stage5_replica_manifest.json"
            )
            if path.is_file():
                results.append(json.loads(path.read_text(encoding="utf-8")))
    passed = [item for item in results if item.get("status") == "passed"]
    expected = candidate_count * replica_count
    status = "completed" if len(passed) == expected else ("failed" if error else "partial")
    summary = {
        "status": status,
        "created_at_utc": _utc_now(),
        "source_stage4_summary": str(source_summary),
        "source_stage4_summary_sha256": _sha256(source_summary),
        "candidate_count": candidate_count,
        "replicas_per_candidate": replica_count,
        "expected_replica_count": expected,
        "passed_replica_count": len(passed),
        "replicas": sorted(results, key=lambda item: (item["rank"], item["replica"])),
        "error": error,
        "next_stage": (
            "trajectory_analysis_and_mmgbsa"
            if status == "completed"
            else "complete_or_repair_production_replicas"
        ),
    }
    _write_json(output_dir / "stage5_production_summary.json", summary)
    return summary


def preflight(config: dict[str, Any], base: Path) -> dict[str, Any]:
    modules = _openmm_modules()
    summary_path, records = _input_records(config, base)
    platform_name = str(config["runtime"].get("platform", "CUDA"))
    try:
        platform = modules["Platform"].getPlatformByName(platform_name)
    except Exception as exc:
        raise MDProductionError(f"Requested OpenMM platform is unavailable: {platform_name}") from exc
    timestep_fs = float(config["dynamics"]["timestep_fs"])
    production_ns = float(config["production"]["duration_ns"])
    segment_ns = float(config["production"]["segment_ns"])
    initialization_ns = float(config["production"]["replica_initialization_ns"])
    production_steps = _steps_for_duration(production_ns, timestep_fs)
    segment_steps = _steps_for_duration(segment_ns, timestep_fs)
    initialization_steps = _steps_for_duration(initialization_ns, timestep_fs)
    if production_steps % segment_steps != 0:
        raise MDProductionError("Production steps are not divisible by segment steps")
    replica_count = int(config["replicas_per_candidate"])
    if replica_count <= 0:
        raise MDProductionError("replicas_per_candidate must be positive")
    output_dir = _resolve(config["output_dir"], base)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "pass",
        "created_at_utc": _utc_now(),
        "stage4_summary": str(summary_path),
        "stage4_summary_sha256": _sha256(summary_path),
        "candidate_count": len(records),
        "replicas_per_candidate": replica_count,
        "total_replica_count": len(records) * replica_count,
        "production_ns_per_replica": production_ns,
        "aggregate_production_ns": len(records) * replica_count * production_ns,
        "segments_per_replica": production_steps // segment_steps,
        "steps_per_segment": segment_steps,
        "initialization_steps_per_replica": initialization_steps,
        "openmm_version": getattr(modules["openmm"], "__version__", "unknown"),
        "platform": platform.getName(),
        "input_atom_counts": {
            str(item["rank"]): _prmtop_natom(item["prmtop"]) for item in records
        },
    }
    _write_json(output_dir / "preflight.json", result)
    return result


def run_production(
    config: dict[str, Any],
    base: Path,
    rank_filter: int | None,
    replica_filter: int | None,
    resume: bool,
    overwrite: bool,
) -> dict[str, Any]:
    if resume and overwrite:
        raise MDProductionError("--resume and --overwrite are mutually exclusive")
    modules = _openmm_modules()
    summary_path, records = _input_records(config, base)
    output_dir = _resolve(config["output_dir"], base)
    output_dir.mkdir(parents=True, exist_ok=True)
    if rank_filter is not None:
        records = [record for record in records if record["rank"] == rank_filter]
        if not records:
            raise MDProductionError(f"Rank filter is outside available candidates: {rank_filter}")
    replica_count = int(config["replicas_per_candidate"])
    replicas = list(range(1, replica_count + 1))
    if replica_filter is not None:
        if not 1 <= replica_filter <= replica_count:
            raise MDProductionError(f"Replica filter is outside 1..{replica_count}")
        replicas = [replica_filter]
    try:
        for record in records:
            for replica in replicas:
                _run_replica(
                    config,
                    record,
                    replica,
                    output_dir,
                    modules,
                    resume=resume,
                    overwrite=overwrite,
                )
    except MDProductionError as exc:
        _refresh_summary(config, base, summary_path, error=str(exc))
        raise
    return _refresh_summary(config, base, summary_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["preflight", "run", "all"])
    parser.add_argument("--config", required=True, help="YAML or JSON Stage-5 configuration")
    parser.add_argument("--rank", type=int, help="Run one candidate rank only")
    parser.add_argument("--replica", type=int, help="Run one replica index only")
    parser.add_argument("--resume", action="store_true", help="Resume from validated 1 ns segments")
    parser.add_argument("--overwrite", action="store_true", help="Restart selected replicas from the beginning")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = _load_mapping(config_path)
    try:
        if args.stage == "preflight":
            result = preflight(config, config_path.parent)
        elif args.stage == "run":
            result = run_production(
                config,
                config_path.parent,
                args.rank,
                args.replica,
                args.resume,
                args.overwrite,
            )
        else:
            preflight(config, config_path.parent)
            result = run_production(
                config,
                config_path.parent,
                args.rank,
                args.replica,
                args.resume,
                args.overwrite,
            )
    except MDProductionError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
