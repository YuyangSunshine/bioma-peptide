"""
Small molecule tools: RDKit-based property calculation, similarity, substructure, 3D conformer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError
from tools.our_tools._crosslinked_impl.ensemble_contract import (
    contract_hash,
    require_final_target,
)

# ---------------------------------------------------------------------------
# Generic RDKit conformer generation and xTB ensemble optimization
# ---------------------------------------------------------------------------

_ENSEMBLE_STABLE_ID = "_BioMAStableAtomId"
_XTB_SOLVENTS = {
    "ALPB": (
        "acetone",
        "acetonitrile",
        "benzene",
        "ch2cl2",
        "chloroform",
        "cs2",
        "dmf",
        "dmso",
        "ether",
        "ethylacetate",
        "furane",
        "hexadecane",
        "hexane",
        "methanol",
        "nitromethane",
        "octanol",
        "phenol",
        "thf",
        "toluene",
        "water",
    ),
    "GBSA": (
        "acetone",
        "acetonitrile",
        "benzene",
        "ch2cl2",
        "chloroform",
        "cs2",
        "dmf",
        "dmso",
        "ether",
        "methanol",
        "thf",
        "toluene",
        "water",
    ),
}


class RDKitConformerEnsembleInput(ToolInput):
    topology_manifest_path: str = Field(description="Manifest from the topology tool.")
    target_ensemble_size: int = Field(
        ge=1,
        le=100,
        description=(
            "Final requested ensemble size K. Pass the user's requested output count "
            "directly; this tool derives the larger raw ETKDG sampling count."
        ),
    )
    output_dir: str = Field(description="Directory for raw RDKit conformer artifacts.")
    sampling_options_json: str = Field(
        default="{}",
        description="Optional JSON overrides for controlled tests and advanced runs.",
    )


class XTBConformerEnsembleInput(ToolInput):
    input_manifest_path: str = Field(description="Manifest containing selected conformers.")
    optimization_level: Literal["normal", "tight"]
    method: str = Field(default="GFN2-xTB")
    solvent_model: Literal["ALPB", "GBSA"] = Field(default="ALPB")
    solvent: str = Field(default="water")
    output_dir: str = Field(description="Directory for xTB stage artifacts.")
    xtb_options_json: str = Field(
        default="{}",
        description="Optional JSON overrides for executable, cycles, timeouts, and retry policy.",
    )


def _ensemble_json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"{label} is not valid JSON: {exc}",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit/xTB",
        ) from exc
    if not isinstance(value, dict):
        raise ToolError(
            f"{label} must be a JSON object.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit/xTB",
        )
    return value


def _ensemble_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensemble_molecule_result_hash(molecules: list[Any]) -> str:
    """Hash molecular identities and coordinates without output-directory metadata."""
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
    return _ensemble_hash(records)


def _ensemble_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _ensemble_load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _ensemble_rdkit() -> tuple[Any, Any, Any]:
    try:
        from rdkit import Chem, rdBase
        from rdkit.Chem import AllChem
    except ImportError as exc:
        raise ToolError(
            "RDKit is required for conformer ensemble tools.",
            error_type="unavailable",
            retriable=False,
            backend="RDKit",
        ) from exc
    return Chem, AllChem, rdBase


def _ensemble_read_sdf(path: Path) -> list[Any]:
    Chem, *_ = _ensemble_rdkit()
    supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=True)
    molecules = [mol for mol in supplier if mol is not None]
    if not molecules:
        raise ValueError(f"No readable molecules were found in {path}.")
    return molecules


def _ensemble_create_atom_property_list(Chem: Any, mol: Any) -> None:
    creator = getattr(Chem, "CreateAtomStringPropertyList", None)
    if creator is not None:
        creator(mol, _ENSEMBLE_STABLE_ID)


def _ensemble_apply_atom_map(mol: Any, atom_map_path: Path) -> None:
    records = _ensemble_load_json(atom_map_path).get("atoms", [])
    for record in records:
        index = int(record["atom_index"])
        if index >= mol.GetNumAtoms():
            raise ValueError("Atom map is incompatible with molecule atom count.")
        atom = mol.GetAtomWithIdx(index)
        if atom.GetSymbol() != record["element"]:
            raise ValueError("Atom-map element order differs from the molecule.")
        atom.SetProp(_ENSEMBLE_STABLE_ID, str(record["stable_id"]))


def _ensemble_assign_hydrogen_metadata(mol: Any) -> None:
    Chem, *_ = _ensemble_rdkit()
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() != 1:
            continue
        atom.SetProp(_ENSEMBLE_STABLE_ID, f"H:{atom.GetIdx() + 1}")
        atom.SetProp("bioma_component", "hydrogen")
        neighbors = list(atom.GetNeighbors())
        parent_info = neighbors[0].GetPDBResidueInfo() if neighbors else None
        info = Chem.AtomPDBResidueInfo()
        info.SetName(f"H{atom.GetIdx() + 1:03d}"[-4:])
        if parent_info is not None:
            info.SetResidueName(parent_info.GetResidueName())
            info.SetResidueNumber(parent_info.GetResidueNumber())
            info.SetChainId(parent_info.GetChainId())
            info.SetIsHeteroAtom(parent_info.GetIsHeteroAtom())
        else:
            info.SetResidueName("MOL")
            info.SetResidueNumber(1)
            info.SetChainId("A")
            info.SetIsHeteroAtom(True)
        atom.SetMonomerInfo(info)


def _ensemble_copy_conformer(mol: Any, conformer_id: int) -> Any:
    Chem, *_ = _ensemble_rdkit()
    copied = Chem.Mol(mol)
    conformer = Chem.Conformer(mol.GetConformer(int(conformer_id)))
    conformer.SetId(0)
    copied.RemoveAllConformers()
    copied.AddConformer(conformer, assignId=True)
    return copied


def _ensemble_round_up(value: int, multiple: int) -> int:
    return int(math.ceil(value / multiple) * multiple)


def _ensemble_calculated_raw_count(
    final_ensemble_size: int,
    sequence_length: int,
) -> int:
    return _ensemble_round_up(
        max(100, 20 * int(final_ensemble_size), 10 * int(sequence_length)),
        25,
    )


@bioma_tool(
    name="generate_rdkit_conformer_ensemble",
    description=(
        "Generate a reproducible multi-seed ETKDGv3 conformer set from a validated "
        "molecular topology and write complete raw SDF and embedding records."
    ),
    category="analysis",
    version="1.2.0",
    author="BioMA",
    args_schema=RDKitConformerEnsembleInput,
    tags=["rdkit", "etkdg", "conformer", "ensemble", "3d"],
)
def generate_rdkit_conformer_ensemble(
    topology_manifest_path: str,
    target_ensemble_size: int,
    output_dir: str,
    sampling_options_json: str = "{}",
) -> str:
    contract = require_final_target(target_ensemble_size, stage="rdkit_raw")
    Chem, AllChem, rd_base = _ensemble_rdkit()
    options = _ensemble_json_object(sampling_options_json, "sampling_options_json")
    manifest_file = Path(topology_manifest_path).expanduser().resolve()
    topology_manifest = _ensemble_load_json(manifest_file)
    if topology_manifest.get("status") != "ok":
        raise ToolError(
            "The topology manifest must have status 'ok'.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    topology_path = Path(topology_manifest["artifacts"]["topology_sdf"]).resolve()
    atom_map_path = Path(topology_manifest["artifacts"]["atom_map_json"]).resolve()
    topology = _ensemble_read_sdf(topology_path)[0]
    _ensemble_apply_atom_map(topology, atom_map_path)
    sequence_length = int(topology_manifest.get("sequence_length", 0))
    calculated_raw = _ensemble_calculated_raw_count(
        target_ensemble_size,
        sequence_length,
    )
    raw_requested = int(options.get("raw_conformers", calculated_raw))
    seed_count = int(
        options.get("seed_count", min(8, max(2, math.ceil(raw_requested / 25))))
    )
    base_seed = int(options.get("base_seed", 20260908))
    rdkit_threads = int(options.get("rdkit_threads", 0))
    if raw_requested < 1 or seed_count < 1:
        raise ToolError(
            "raw_conformers and seed_count must be positive.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )

    per_seed = int(math.ceil(raw_requested / seed_count))
    molecules: list[Any] = []
    records: list[dict[str, Any]] = []
    for seed_index in range(seed_count):
        if len(molecules) >= raw_requested:
            break
        seed = int((base_seed + seed_index * 104729) & 0x7FFFFFFF)
        batch = Chem.AddHs(Chem.Mol(topology), addCoords=False)
        _ensemble_assign_hydrogen_metadata(batch)
        params = AllChem.ETKDGv3()
        params.randomSeed = seed
        params.useRandomCoords = True
        params.enforceChirality = True
        params.useMacrocycleTorsions = True
        params.useMacrocycle14config = True
        params.pruneRmsThresh = -1.0
        params.numThreads = rdkit_threads
        wanted = min(per_seed, raw_requested - len(molecules))
        conformer_ids = [
            int(value)
            for value in AllChem.EmbedMultipleConfs(batch, numConfs=wanted, params=params)
        ]
        for local_index, conformer_id in enumerate(conformer_ids, start=1):
            candidate = _ensemble_copy_conformer(batch, conformer_id)
            source_candidate_id = f"seed_{seed_index + 1:02d}_conf_{local_index:04d}"
            candidate_id = f"rdkit:{source_candidate_id}"
            candidate.SetProp("candidate_id", candidate_id)
            candidate.SetProp("source_candidate_id", source_candidate_id)
            candidate.SetProp("source_backend", "rdkit")
            candidate.SetProp("rdkit_seed", str(seed))
            candidate.SetProp("rdkit_conformer_id", str(conformer_id))
            candidate.SetProp("bioma_chemical_hash", topology_manifest["chemical_hash"])
            _ensemble_create_atom_property_list(Chem, candidate)
            positions = candidate.GetConformer().GetPositions()
            finite = all(math.isfinite(float(value)) for row in positions for value in row)
            molecules.append(candidate)
            records.append(
                {
                    "candidate_id": candidate_id,
                    "source_candidate_id": source_candidate_id,
                    "source_backend": "rdkit",
                    "seed_index": seed_index + 1,
                    "seed": seed,
                    "rdkit_conformer_id": conformer_id,
                    "atom_count": candidate.GetNumAtoms(),
                    "finite_coordinates": finite,
                    "embedded": True,
                }
            )

    if not molecules:
        raise ToolError(
            "ETKDGv3 produced no conformers.",
            error_type="execution_error",
            retriable=True,
            backend="RDKit",
        )
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sdf_path = destination / "rdkit_raw_conformers.sdf"
    records_path = destination / "rdkit_embedding_records.csv"
    manifest_path = destination / "raw_rdkit_manifest.json"
    writer = Chem.SDWriter(str(sdf_path))
    for molecule in molecules:
        writer.write(molecule)
    writer.close()
    with records_path.open("w", newline="", encoding="utf-8-sig") as handle:
        fieldnames = list(records[0])
        record_writer = __import__("csv").DictWriter(handle, fieldnames=fieldnames)
        record_writer.writeheader()
        record_writer.writerows(records)
    roundtrip = _ensemble_read_sdf(sdf_path)
    consistent_atom_count = all(
        mol.GetNumAtoms() == molecules[0].GetNumAtoms() for mol in roundtrip
    )
    status = "ok" if len(roundtrip) == len(molecules) and consistent_atom_count else "failed"
    sampling_config = {
        "raw_conformers": raw_requested,
        "calculated_raw_conformers": calculated_raw,
        "seed_count": seed_count,
        "base_seed": base_seed,
        "rdkit_threads": rdkit_threads,
        "method": "ETKDGv3",
        "scientific_filtering": "validate_cluster_select_conformers",
    }
    ensemble_hash = _ensemble_molecule_result_hash(molecules)
    result_hash = _ensemble_hash(
        {
            "status": status,
            "candidate_count": len(molecules),
            "candidate_ids": [record["candidate_id"] for record in records],
            "ensemble_hash": ensemble_hash,
            "ensemble_contract": contract_hash(contract),
        }
    )
    manifest = {
        "contract_version": "1.0",
        "tool_name": "generate_rdkit_conformer_ensemble",
        "tool_version": "1.2.0",
        "stage": "rdkit_raw",
        "status": status,
        "chemical_hash": topology_manifest["chemical_hash"],
        "input_hash": topology_manifest["config_hash"],
        "config_hash": _ensemble_hash(
            {"input": topology_manifest["config_hash"], "sampling": sampling_config, "rdkit": rd_base.rdkitVersion, "ensemble_contract": contract_hash(contract)}
        ),
        "result_hash": result_hash,
        "parent_manifest": str(manifest_file),
        "parent_manifests": [str(manifest_file)],
        "topology_manifest": str(manifest_file),
        "target_ensemble_size": target_ensemble_size,
        "ensemble_contract_hash": contract_hash(contract),
        "candidate_count": len(molecules),
        "candidate_ids": [record["candidate_id"] for record in records],
        "sampling": sampling_config,
        "source_backend": "rdkit",
        "artifacts": {
            "conformers_sdf": str(sdf_path),
            "embedding_records_csv": str(records_path),
        },
        "artifact_hashes": {"ensemble_scientific_sha256": ensemble_hash},
        "warnings": [],
        "recommended_action": None,
        "implementation": {"rdkit_version": rd_base.rdkitVersion},
    }
    _ensemble_write_json(manifest_path, manifest)
    return json.dumps(
        {
            "status": status,
            "manifest_path": str(manifest_path),
            "conformers_sdf": str(sdf_path),
            "requested_count": raw_requested,
            "generated_count": len(molecules),
            "seed_count": seed_count,
            "base_seed": base_seed,
            "config_hash": manifest["config_hash"],
            "result_hash": result_hash,
        },
        ensure_ascii=False,
        indent=2,
    )


def _xtb_detect_cpu_count() -> tuple[int, list[dict[str, Any]]]:
    detections: list[dict[str, Any]] = []
    if hasattr(os, "sched_getaffinity"):
        try:
            value = len(os.sched_getaffinity(0))
            if value > 0:
                detections.append({"source": "os.sched_getaffinity", "value": value})
        except OSError:
            pass
    for name in ("SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE"):
        raw = os.environ.get(name, "").strip()
        match = re.match(r"^(\d+)", raw)
        if match and int(match.group(1)) > 0:
            detections.append({"source": name, "value": int(match.group(1))})
    try:
        import psutil

        value = psutil.cpu_count(logical=True)
        if value:
            detections.append({"source": "psutil.cpu_count", "value": int(value)})
    except ImportError:
        pass
    value = os.cpu_count()
    if value:
        detections.append({"source": "os.cpu_count", "value": int(value)})
    if not detections:
        detections.append({"source": "fallback", "value": 1})
    constrained = [
        item["value"]
        for item in detections
        if item["source"] in {"os.sched_getaffinity", "SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE"}
    ]
    detected = min(constrained) if constrained else min(item["value"] for item in detections)
    return max(1, int(detected)), detections


def _xtb_available_memory_gb() -> float | None:
    try:
        import psutil

        return float(psutil.virtual_memory().available) / (1024.0 ** 3)
    except ImportError:
        return None


def _xtb_execution_plan(atom_count: int, candidate_count: int) -> dict[str, Any]:
    detected, detections = _xtb_detect_cpu_count()
    usable = max(1, int(math.floor(detected * 0.875)))
    if atom_count <= 150:
        threads = 2
    elif atom_count <= 400:
        threads = 4
    else:
        threads = 8
    threads = min(threads, usable)
    cpu_jobs = max(1, usable // threads)
    available_memory = _xtb_available_memory_gb()
    estimated_per_job = max(0.75, atom_count * 0.004)
    memory_jobs = candidate_count
    if available_memory is not None:
        memory_jobs = max(1, int((available_memory * 0.8) // estimated_per_job))
    max_parallel = max(1, min(candidate_count, cpu_jobs, memory_jobs))
    return {
        "detected_cpu_count": detected,
        "cpu_detections": detections,
        "usable_cpu_count": usable,
        "atom_count": atom_count,
        "candidate_count": candidate_count,
        "threads_per_xtb": threads,
        "max_parallel": max_parallel,
        "available_memory_gb": available_memory,
        "estimated_memory_per_job_gb": estimated_per_job,
        "memory_job_limit": memory_jobs,
    }


def _xtb_write_xyz(mol: Any, path: Path) -> None:
    conformer = mol.GetConformer()
    lines = [str(mol.GetNumAtoms()), "BioMA xTB conformer optimization"]
    for atom in mol.GetAtoms():
        point = conformer.GetAtomPosition(atom.GetIdx())
        lines.append(f"{atom.GetSymbol():<2} {point.x: .10f} {point.y: .10f} {point.z: .10f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _xtb_read_xyz(mol: Any, path: Path) -> None:
    from rdkit.Geometry import Point3D

    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError("xTB XYZ output is empty.")
    atom_count = int(lines[0].strip())
    if atom_count != mol.GetNumAtoms() or len(lines) < atom_count + 2:
        raise ValueError("xTB XYZ atom count differs from the input molecule.")
    conformer = mol.GetConformer()
    for index, line in enumerate(lines[2 : 2 + atom_count]):
        fields = line.split()
        if len(fields) < 4:
            raise ValueError(f"Invalid XYZ coordinate line {line!r}.")
        expected = mol.GetAtomWithIdx(index).GetSymbol().lower()
        if fields[0].lower() != expected:
            raise ValueError(f"xTB atom order changed at index {index}.")
        values = [float(value) for value in fields[1:4]]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("xTB produced a non-finite coordinate.")
        conformer.SetAtomPosition(index, Point3D(*values))


def _xtb_energy(text: str) -> float | None:
    matches = re.findall(r"TOTAL ENERGY\s+(-?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)", text)
    return float(matches[-1]) if matches else None


def _xtb_executable(value: str) -> str:
    requested = value.strip()
    if not requested or any(char.isspace() for char in requested):
        raise ToolError(
            "xtb_command must be one executable name or absolute path.",
            error_type="invalid_input",
            retriable=False,
            backend="xTB",
        )
    resolved = shutil.which(requested)
    if resolved:
        return resolved
    path = Path(requested)
    if path.is_file():
        return str(path.resolve())
    raise ToolError(
        f"xTB executable {requested!r} was not found.",
        error_type="unavailable",
        retriable=False,
        backend="xTB",
    )


def _xtb_run_one(
    Chem: Any,
    molecule: Any,
    candidate_id: str,
    work_dir: Path,
    command_prefix: list[str],
    total_charge: int,
    uhf: int,
    gfn: int,
    level: str,
    cycles: int,
    timeout: int,
    solvent_model: str,
    solvent: str,
    threads: int,
    stage_manifest_path: Path,
) -> tuple[Any, dict[str, Any]]:
    started_at = datetime.now(timezone.utc).isoformat()
    started_clock = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)
    candidate = Chem.Mol(molecule)
    input_path = work_dir / "input.xyz"
    _xtb_write_xyz(candidate, input_path)
    command = command_prefix + [
        input_path.name,
        "--gfn",
        str(gfn),
        "--opt",
        level,
        "--cycles",
        str(cycles),
        "--chrg",
        str(total_charge),
        "--uhf",
        str(uhf),
        "--alpb" if solvent_model.upper() == "ALPB" else "--gbsa",
        solvent,
    ]
    environment = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        environment[key] = str(threads)
    try:
        completed = subprocess.run(
            command,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        timed_out = False
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timed_out = True
        return_code = None
    (work_dir / "xtb.out").write_text(stdout, encoding="utf-8")
    (work_dir / "xtb.err").write_text(stderr, encoding="utf-8")
    combined = stdout + "\n" + stderr
    optimized_path = work_dir / "xtbopt.xyz"
    output_upper = combined.upper()
    optimization_failed = "FAILED TO CONVERGE GEOMETRY OPTIMIZATION" in output_upper
    marker = "GEOMETRY OPTIMIZATION CONVERGED" in output_upper and not optimization_failed
    energy = _xtb_energy(combined)
    converged = bool(return_code == 0 and marker and optimized_path.is_file() and energy is not None)
    error = (
        "xTB completed but the geometry optimizer did not converge within the "
        "configured cycle budget; inspect xtb.out and increase cycles or improve "
        "the initial geometry."
        if return_code == 0 and optimized_path.is_file() and not marker
        else None
    )
    upstream_source_backend = (
        molecule.GetProp("source_backend")
        if molecule.HasProp("source_backend")
        else ""
    )
    upstream_source_manifest = (
        molecule.GetProp("source_manifest")
        if molecule.HasProp("source_manifest")
        else ""
    )
    if converged:
        try:
            _xtb_read_xyz(candidate, optimized_path)
        except (ValueError, OSError) as exc:
            converged = False
            error = str(exc)
    if converged:
        candidate.SetProp("candidate_id", candidate_id)
        candidate.SetProp("xtb_converged", "true")
        candidate.SetProp("xtb_energy_hartree", str(energy))
        candidate.SetProp("xtb_optimization_level", level)
        candidate.SetProp("xtb_solvent_model", solvent_model.upper())
        candidate.SetProp("xtb_solvent", solvent)
        candidate.SetProp("source_backend", "xtb")
        candidate.SetProp("coordinate_source_backend", "xtb")
        candidate.SetProp("source_manifest", str(stage_manifest_path))
        if upstream_source_backend:
            candidate.SetProp("upstream_source_backend", upstream_source_backend)
        if upstream_source_manifest:
            candidate.SetProp("upstream_source_manifest", upstream_source_manifest)
        _ensemble_create_atom_property_list(Chem, candidate)
        result_sdf = work_dir / "optimized.sdf"
        writer = Chem.SDWriter(str(result_sdf))
        writer.write(candidate)
        writer.close()
    else:
        result_sdf = None
    record = {
        "candidate_id": candidate_id,
        "status": "success" if converged else "failed",
        "converged": converged,
        "return_code": return_code,
        "timed_out": timed_out,
        "total_energy_hartree": energy,
        "error": error,
        "source_backend": "xtb" if converged else upstream_source_backend,
        "coordinate_source_backend": (
            "xtb" if converged else upstream_source_backend
        ),
        "source_manifest": str(stage_manifest_path) if converged else upstream_source_manifest,
        "upstream_source_backend": upstream_source_backend,
        "upstream_source_manifest": upstream_source_manifest,
        "command": command,
        "work_directory": str(work_dir),
        "stdout_path": str(work_dir / "xtb.out"),
        "stderr_path": str(work_dir / "xtb.err"),
        "optimized_sdf": str(result_sdf) if result_sdf else None,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": time.perf_counter() - started_clock,
    }
    _ensemble_write_json(work_dir / "command.json", {"command": command, "environment_threads": threads})
    _ensemble_write_json(work_dir / "result.json", record)
    return candidate, record


@bioma_tool(
    name="optimize_conformer_ensemble_with_xtb",
    description=(
        "Run resource-aware parallel xTB optimization for a selected conformer set, "
        "preserve the validated bond graph, and write per-conformer logs and coordinates."
    ),
    category="analysis",
    version="1.1.0",
    author="BioMA",
    args_schema=XTBConformerEnsembleInput,
    tags=["xtb", "optimization", "parallel", "conformer", "checkpoint"],
)
def optimize_conformer_ensemble_with_xtb(
    input_manifest_path: str,
    optimization_level: Literal["normal", "tight"],
    method: str = "GFN2-xTB",
    solvent_model: Literal["ALPB", "GBSA"] = "ALPB",
    solvent: str = "water",
    output_dir: str = "",
    xtb_options_json: str = "{}",
) -> str:
    # xTB has no target-size argument, so verify the authoritative target that
    # travelled in the selected-manifest provenance before allocating scratch.
    contract = require_final_target(
        int(_ensemble_load_json(Path(input_manifest_path).expanduser().resolve()).get("target_ensemble_size", -1)),
        stage=f"xtb_{optimization_level}",
    )
    Chem, _, rd_base = _ensemble_rdkit()
    options = _ensemble_json_object(xtb_options_json, "xtb_options_json")
    manifest_file = Path(input_manifest_path).expanduser().resolve()
    input_manifest = _ensemble_load_json(manifest_file)
    topology_manifest_file = Path(input_manifest["topology_manifest"]).resolve()
    topology_manifest = _ensemble_load_json(topology_manifest_file)
    source_path = input_manifest.get("artifacts", {}).get("selected_sdf")
    if not source_path:
        raise ToolError(
            "The input manifest has no selected_sdf artifact.",
            error_type="invalid_input",
            retriable=False,
            backend="xTB",
        )
    molecules = _ensemble_read_sdf(Path(source_path).resolve())
    atom_map_path = Path(topology_manifest["artifacts"]["atom_map_json"]).resolve()
    for mol in molecules:
        _ensemble_apply_atom_map(mol, atom_map_path)
    method_match = re.fullmatch(r"GFN([012])-xTB", method, flags=re.IGNORECASE)
    if method_match is None:
        raise ToolError(
            "method must be GFN0-xTB, GFN1-xTB, or GFN2-xTB.",
            error_type="invalid_input",
            retriable=False,
            backend="xTB",
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", solvent):
        raise ToolError(
            "solvent must be an xTB solvent name.",
            error_type="invalid_input",
            retriable=False,
            backend="xTB",
        )
    solvent = solvent.lower()
    supported_solvents = _XTB_SOLVENTS[solvent_model.upper()]
    if solvent not in supported_solvents:
        return json.dumps(
            {
                "status": "unsupported_solvent",
                "requested_model": solvent_model.upper(),
                "requested_solvent": solvent,
                "supported_alternatives": list(supported_solvents),
                "recommended_action": "select_supported_solvent_approximation",
            },
            ensure_ascii=False,
            indent=2,
        )
    gfn = int(method_match.group(1))
    executable = _xtb_executable(str(options.get("xtb_command", "xtb")))
    cycles = int(options.get("cycles", 500 if optimization_level == "normal" else 800))
    timeout = int(options.get("timeout_seconds", 10800 if optimization_level == "normal" else 21600))
    retry_failed = bool(options.get("retry_failed", True))
    solvent_selection = options.get("solvent_selection", {})
    if not isinstance(solvent_selection, dict):
        raise ToolError(
            "xtb_options_json.solvent_selection must be an object.",
            error_type="invalid_input",
            retriable=False,
            backend="xTB",
        )
    plan = _xtb_execution_plan(molecules[0].GetNumAtoms(), len(molecules))
    total_charge = int(topology_manifest["total_charge"])
    uhf = int(topology_manifest["uhf"])
    config = {
        "input_manifest_hash": input_manifest["config_hash"],
        "ensemble_contract": contract_hash(contract),
        "chemical_hash": topology_manifest["chemical_hash"],
        "optimization_level": optimization_level,
        "method": method,
        "gfn": gfn,
        "solvent_model": solvent_model.upper(),
        "solvent": solvent,
        "total_charge": total_charge,
        "uhf": uhf,
        "cycles": cycles,
        "timeout_seconds": timeout,
        "executable": executable,
        "rdkit_version": rd_base.rdkitVersion,
        "tool_version": "1.1.0",
        "solvent_selection": solvent_selection,
    }
    config_hash = _ensemble_hash(config)
    destination = Path(output_dir).expanduser().resolve()
    run_root = destination / f"{optimization_level}_{config_hash[:12]}"
    run_root.mkdir(parents=True, exist_ok=True)
    plan_path = run_root / "execution_plan.json"
    checkpoint_path = run_root / "checkpoint.json"
    optimized_path = run_root / f"{optimization_level}_optimized.sdf"
    results_path = run_root / f"{optimization_level}_results.json"
    manifest_path = run_root / f"{optimization_level}_stage_manifest.json"
    _ensemble_write_json(plan_path, plan)
    checkpoint: dict[str, Any]
    if checkpoint_path.is_file():
        checkpoint = _ensemble_load_json(checkpoint_path)
        if checkpoint.get("config_hash") != config_hash:
            checkpoint = {"config_hash": config_hash, "config": config, "conformers": {}}
    else:
        checkpoint = {"config_hash": config_hash, "config": config, "conformers": {}}
    _ensemble_write_json(checkpoint_path, checkpoint)
    lock = threading.Lock()

    candidate_by_id: dict[str, Any] = {}
    for index, mol in enumerate(molecules, start=1):
        candidate_id = mol.GetProp("candidate_id") if mol.HasProp("candidate_id") else f"candidate_{index:04d}"
        if candidate_id in candidate_by_id:
            raise ValueError(f"Duplicate candidate_id {candidate_id!r}.")
        candidate_by_id[candidate_id] = mol

    outcomes: dict[str, tuple[Any, dict[str, Any]]] = {}
    pending: list[str] = []
    for candidate_id, molecule in candidate_by_id.items():
        saved = checkpoint["conformers"].get(candidate_id, {})
        saved_sdf = saved.get("optimized_sdf")
        if saved.get("status") == "success" and saved_sdf and Path(saved_sdf).is_file():
            loaded = _ensemble_read_sdf(Path(saved_sdf))[0]
            _ensemble_apply_atom_map(loaded, atom_map_path)
            outcomes[candidate_id] = (loaded, saved)
        elif saved.get("status") == "failed" and not retry_failed:
            outcomes[candidate_id] = (Chem.Mol(molecule), saved)
        else:
            pending.append(candidate_id)

    finished_ids = set(outcomes)
    stage_started_at = datetime.now(timezone.utc).isoformat()
    stage_clock = time.perf_counter()
    stage_status_override: str | None = None
    interruption: dict[str, str] | None = None

    def write_stage_snapshot() -> dict[str, Any]:
        result_records: list[dict[str, Any]] = []
        successful: list[Any] = []
        for candidate_id in candidate_by_id:
            if candidate_id not in finished_ids:
                continue
            record = checkpoint["conformers"].get(candidate_id)
            if not isinstance(record, dict):
                continue
            result_records.append(record)
            saved_sdf = record.get("optimized_sdf")
            if record.get("status") == "success" and saved_sdf and Path(saved_sdf).is_file():
                loaded = _ensemble_read_sdf(Path(saved_sdf))[0]
                _ensemble_apply_atom_map(loaded, atom_map_path)
                successful.append(loaded)

        temporary_sdf = optimized_path.with_name(optimized_path.stem + ".tmp.sdf")
        writer = Chem.SDWriter(str(temporary_sdf))
        for molecule in successful:
            _ensemble_create_atom_property_list(Chem, molecule)
            writer.write(molecule)
        writer.close()
        os.replace(temporary_sdf, optimized_path)
        _ensemble_write_json(results_path, {"records": result_records})

        completed_count = len(result_records)
        failed_count = sum(
            1 for record in result_records if record.get("status") != "success"
        )
        pending_count = len(candidate_by_id) - completed_count
        if stage_status_override is not None:
            status = stage_status_override
        elif pending_count:
            status = "running"
        elif len(successful) == len(candidate_by_id):
            status = "ok"
        elif successful:
            status = "partial_success"
        else:
            status = "failed"

        ensemble_hash = _ensemble_molecule_result_hash(successful)
        scientific_records = [
            {
                "candidate_id": record.get("candidate_id"),
                "status": record.get("status"),
                "converged": record.get("converged"),
                "return_code": record.get("return_code"),
                "timed_out": record.get("timed_out"),
                "total_energy_hartree": record.get("total_energy_hartree"),
                "error": record.get("error"),
            }
            for record in result_records
        ]
        result_hash = _ensemble_hash(
            {
                "status": status,
                "candidate_ids": [
                    molecule.GetProp("candidate_id") for molecule in successful
                ],
                "ensemble_hash": ensemble_hash,
                "records": scientific_records,
                "pending_candidate_count": pending_count,
            }
        )
        recommended_action = (
            "resume_interrupted_xtb"
            if status in {"running", "interrupted"}
            else None
            if status == "ok"
            else "validate_available_xtb_results"
        )
        stage_manifest = {
            "contract_version": "1.0",
            "tool_name": "optimize_conformer_ensemble_with_xtb",
            "tool_version": "1.1.0",
            "stage": f"xtb_{optimization_level}",
            "status": status,
            "chemical_hash": topology_manifest["chemical_hash"],
            "input_hash": input_manifest["config_hash"],
            "config_hash": config_hash,
            "result_hash": result_hash,
            "parent_manifest": str(manifest_file),
            "parent_manifests": [str(manifest_file)],
            "topology_manifest": str(topology_manifest_file),
            "target_ensemble_size": input_manifest.get("target_ensemble_size"),
            "ensemble_contract_hash": contract_hash(contract),
            "candidate_count": len(successful),
            "candidate_ids": [
                molecule.GetProp("candidate_id") for molecule in successful
            ],
            "attempted_candidate_count": len(candidate_by_id),
            "completed_candidate_count": completed_count,
            "failed_candidate_count": failed_count,
            "pending_candidate_count": pending_count,
            "optimization": config,
            "solvent_selection": solvent_selection,
            "execution_plan": plan,
            "source_backend": "xtb",
            "timing": {
                "started_at_utc": stage_started_at,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "finished_at_utc": (
                    datetime.now(timezone.utc).isoformat()
                    if status not in {"running"}
                    else None
                ),
                "duration_seconds": time.perf_counter() - stage_clock,
            },
            "interruption": interruption,
            "experimental_conditions": topology_manifest.get(
                "experimental_conditions", {}
            ),
            "total_charge": total_charge,
            "uhf": uhf,
            "artifacts": {
                "optimized_sdf": str(optimized_path),
                "results_json": str(results_path),
                "execution_plan_json": str(plan_path),
                "checkpoint_json": str(checkpoint_path),
            },
            "artifact_hashes": {
                "ensemble_scientific_sha256": ensemble_hash,
            },
            "warnings": (
                ["The xTB stage was interrupted; resume the same config hash."]
                if status == "interrupted"
                else []
            ),
            "recommended_action": recommended_action,
        }
        _ensemble_write_json(manifest_path, stage_manifest)
        return stage_manifest

    write_stage_snapshot()

    def run_candidate(candidate_id: str) -> tuple[str, Any, dict[str, Any]]:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id)
        molecule, record = _xtb_run_one(
            Chem,
            candidate_by_id[candidate_id],
            candidate_id,
            run_root / "conformers" / safe_id,
            [executable],
            total_charge,
            uhf,
            gfn,
            optimization_level,
            cycles,
            timeout,
            solvent_model,
            solvent,
            int(plan["threads_per_xtb"]),
            manifest_path,
        )
        with lock:
            checkpoint["conformers"][candidate_id] = record
            _ensemble_write_json(checkpoint_path, checkpoint)
            finished_ids.add(candidate_id)
            write_stage_snapshot()
        return candidate_id, molecule, record

    executor = ThreadPoolExecutor(max_workers=int(plan["max_parallel"]))
    futures = []
    try:
        futures = [executor.submit(run_candidate, candidate_id) for candidate_id in pending]
        for future in as_completed(futures):
            candidate_id, molecule, record = future.result()
            outcomes[candidate_id] = (molecule, record)
    except BaseException as exc:
        for future in futures:
            future.cancel()
        with lock:
            stage_status_override = "interrupted"
            interruption = {
                "exception_type": type(exc).__name__,
                "message": str(exc),
            }
            write_stage_snapshot()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    with lock:
        stage_manifest = write_stage_snapshot()
    return json.dumps(
        {
            "status": stage_manifest["status"],
            "manifest_path": str(manifest_path),
            "optimized_sdf": str(optimized_path),
            "attempted_count": stage_manifest["attempted_candidate_count"],
            "successful_count": stage_manifest["candidate_count"],
            "failed_count": stage_manifest["failed_candidate_count"],
            "pending_count": stage_manifest["pending_candidate_count"],
            "config_hash": config_hash,
            "result_hash": stage_manifest["result_hash"],
            "execution_plan": plan,
        },
        ensure_ascii=False,
        indent=2,
    )
