"""Boltz coordinate proposals for validated crosslinked-peptide topologies.

The topology created by ``build_crosslinked_peptide_topology`` is the chemical
master.  Boltz is used only to propose heavy-atom coordinates; every accepted
prediction is projected back onto that RDKit graph before it enters the common
validation and xTB workflow.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError
from tools.our_tools._crosslinked_impl.ensemble_contract import (
    contract_hash,
    require_final_target,
)


_STABLE_ID = "_BioMAStableAtomId"
_BOLTZ2_MOLECULES_URL = "https://huggingface.co/boltz-community/boltz-2/resolve/main/mols.tar"


class BoltzConformerEnsembleInput(ToolInput):
    topology_manifest_path: str = Field(description="Manifest from the validated topology tool.")
    target_ensemble_size: int = Field(
        ge=1,
        le=100,
        description="Requested final ensemble size; Boltz defaults to a 4 x 4 raw proposal budget.",
    )
    output_dir: str = Field(description="Directory for Boltz inputs, logs, projections, and manifest.")
    boltz_options_json: str = Field(
        default="{}",
        description="Optional JSON overrides for Boltz execution, sampling, cache, and projection settings.",
    )


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"{label} is not valid JSON: {exc}", error_type="invalid_input", retriable=False, backend="Boltz") from exc
    if not isinstance(value, dict):
        raise ToolError(f"{label} must be a JSON object.", error_type="invalid_input", retriable=False, backend="Boltz")
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _rdkit() -> tuple[Any, Any, Any, Any]:
    try:
        import numpy as np
        from rdkit import Chem, rdBase
        from rdkit.Chem import AllChem
    except ImportError as exc:
        raise ToolError("RDKit and NumPy are required for Boltz conformer projection.", error_type="unavailable", retriable=False, backend="Boltz/RDKit") from exc
    return Chem, AllChem, rdBase, np


def _read_sdf(Chem: Any, path: Path) -> list[Any]:
    supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=True)
    molecules = [mol for mol in supplier if mol is not None]
    if not molecules:
        raise ValueError(f"No readable molecules were found in {path}.")
    return molecules


def _apply_atom_map(mol: Any, atom_map_path: Path) -> list[int]:
    records = _load_json(atom_map_path).get("atoms", [])
    core: list[int] = []
    for record in records:
        index = int(record["atom_index"])
        if index >= mol.GetNumAtoms() or mol.GetAtomWithIdx(index).GetSymbol() != record["element"]:
            raise ValueError("Atom map is incompatible with the topology SDF.")
        mol.GetAtomWithIdx(index).SetProp(_STABLE_ID, str(record["stable_id"]))
        if record.get("core_heavy_atom"):
            core.append(index)
    return core


def _create_atom_property_list(Chem: Any, mol: Any) -> None:
    creator = getattr(Chem, "CreateAtomStringPropertyList", None)
    if creator is not None:
        creator(mol, _STABLE_ID)


def _copy_atom_properties(source: Any, target: Any) -> None:
    for name in source.GetPropNames(includePrivate=True, includeComputed=False):
        target.SetProp(name, source.GetProp(name))


def _component_fragment(Chem: Any, AllChem: Any, topology: Any, component_ids: set[str], seed: int) -> tuple[Any, dict[str, str]]:
    prefix = tuple(f"COMP:{component_id}:" for component_id in component_ids)
    selected = [atom.GetIdx() for atom in topology.GetAtoms() if atom.HasProp(_STABLE_ID) and atom.GetProp(_STABLE_ID).startswith(prefix)]
    if not selected:
        raise ValueError("No linker atoms matching the covalent component were found in the topology.")
    selected_set = set(selected)
    editable = Chem.RWMol()
    old_to_new: dict[int, int] = {}
    for old_index in selected:
        source = topology.GetAtomWithIdx(old_index)
        new_index = editable.AddAtom(Chem.Atom(source))
        _copy_atom_properties(source, editable.GetAtomWithIdx(new_index))
        old_to_new[old_index] = new_index
    for bond in topology.GetBonds():
        first, second = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if first in selected_set and second in selected_set:
            editable.AddBond(old_to_new[first], old_to_new[second], bond.GetBondType())
    fragment = editable.GetMol()
    fragment.UpdatePropertyCache(strict=False)
    Chem.SanitizeMol(fragment)
    stable_to_name: dict[str, str] = {}
    counts: dict[str, int] = {}
    for atom in fragment.GetAtoms():
        element = atom.GetSymbol().upper()
        counts[element] = counts.get(element, 0) + 1
        atom_name = f"{element[:2]}{counts[element]:02d}"[-4:]
        atom.SetProp("name", atom_name)
        atom.SetBoolProp("leaving_atom", False)
        stable_to_name[atom.GetProp(_STABLE_ID)] = atom_name
    embedded = Chem.AddHs(Chem.Mol(fragment))
    params = AllChem.ETKDGv3()
    params.randomSeed = int(seed & 0x7FFFFFFF)
    params.useRandomCoords = True
    if int(AllChem.EmbedMolecule(embedded, params)) != 0:
        raise RuntimeError("RDKit could not create a custom-CCD reference conformer.")
    return Chem.RemoveHs(embedded), stable_to_name


def _write_component_pickle(Chem: Any, fragment: Any, output_path: Path, python_command: str | None) -> str:
    if not python_command:
        previous = Chem.GetDefaultPickleProperties()
        try:
            Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)
            with output_path.open("wb") as handle:
                pickle.dump(fragment, handle)
        finally:
            Chem.SetDefaultPickleProperties(previous)
        return "current_python"
    mol_path = output_path.with_suffix(".mol")
    atoms_path = output_path.with_suffix(".atoms.json")
    Chem.MolToMolFile(fragment, str(mol_path))
    _write_json(atoms_path, {"atoms": [{"name": atom.GetProp("name"), "leaving_atom": bool(atom.GetBoolProp("leaving_atom"))} for atom in fragment.GetAtoms()]})
    converter = (
        "import json,pickle,sys; from rdkit import Chem; "
        "m=Chem.MolFromMolFile(sys.argv[1],removeHs=False,sanitize=True); "
        "d=json.load(open(sys.argv[2],encoding='utf-8'))['atoms']; "
        "[(a.SetProp('name',x['name']),a.SetBoolProp('leaving_atom',bool(x['leaving_atom']))) for a,x in zip(m.GetAtoms(),d)]; "
        "Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps); pickle.dump(m,open(sys.argv[3],'wb'))"
    )
    completed = subprocess.run([*shlex.split(python_command), "-c", converter, str(mol_path), str(atoms_path), str(output_path)], text=True, capture_output=True, check=False, timeout=120)
    if completed.returncode != 0 or not output_path.is_file():
        raise RuntimeError(completed.stderr.strip() or "Boltz-environment CCD serialization failed.")
    return "boltz_python"


def _infer_boltz_python_command(boltz_command: str, explicit: Any) -> str | None:
    """Prefer the Python adjacent to Boltz so a separate Boltz env is supported."""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    parts = shlex.split(boltz_command)
    if len(parts) == 1:
        executable = shutil.which(parts[0])
        if executable:
            sibling = Path(executable).resolve().with_name("python")
            if sibling.is_file():
                return str(sibling)
    return None


def _ensure_molecule_cache(cache_dir: Path) -> None:
    canonical = cache_dir / "mols" / "ALA.pkl"
    if canonical.is_file():
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / "mols.tar"
    if not archive.is_file():
        urllib.request.urlretrieve(_BOLTZ2_MOLECULES_URL, archive)
    with tarfile.open(archive, "r") as handle:
        try:
            handle.extractall(cache_dir, filter="data")
        except TypeError:
            handle.extractall(cache_dir)
    if not canonical.is_file():
        raise RuntimeError("Boltz CCD cache is incomplete: mols/ALA.pkl is missing.")


def _prepare_boltz_input(Chem: Any, AllChem: Any, topology: Any, topology_manifest: dict[str, Any], destination: Path, cache_dir: Path, seed: int, boltz_python_command: str | None = None) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    connections = topology_manifest.get("connections", [])
    component_ids: set[str] = set()
    for connection in connections:
        for value in (str(connection.get("atom1", "")), str(connection.get("atom2", ""))):
            fields = value.split(":")
            if len(fields) >= 3 and fields[0] == "COMP":
                component_ids.add(fields[1])
    if len(component_ids) != 1:
        raise ValueError("Boltz custom-CCD generation currently requires exactly one covalently linked component.")
    fragment, stable_to_name = _component_fragment(Chem, AllChem, topology, component_ids, seed)
    # Boltz 2 stores component identifiers in a fixed-width, five-character
    # field.  Longer custom codes are silently truncated during preprocessing,
    # so the YAML identifier and the cache filename would no longer agree.
    ccd_code = f"B{_hash({'chemical_hash': topology_manifest['chemical_hash'], 'components': sorted(component_ids)})[:4].upper()}"
    custom_dir = destination / "custom_components"
    custom_dir.mkdir(parents=True, exist_ok=True)
    local_pickle = custom_dir / f"{ccd_code}.pkl"
    runtime = _write_component_pickle(Chem, fragment, local_pickle, boltz_python_command)
    (cache_dir / "mols").mkdir(parents=True, exist_ok=True)
    cache_target = cache_dir / "mols" / local_pickle.name
    shutil.copyfile(local_pickle, cache_target)
    stable_to_boltz = {stable_id: {"chain_id": "B", "residue_index": 1, "atom_name": atom_name} for stable_id, atom_name in stable_to_name.items()}
    constraints: list[dict[str, Any]] = []
    for connection in connections:
        first, second = str(connection["atom1"]), str(connection["atom2"])
        peptide_id, linker_id = (first, second) if first.startswith("PEP:") else (second, first)
        fields = peptide_id.split(":")
        if len(fields) != 3 or linker_id not in stable_to_boltz:
            raise ValueError(f"Connection {connection.get('connection_id', '')!r} cannot be represented as a peptide-linker Boltz constraint.")
        linker = stable_to_boltz[linker_id]
        constraints.append({"bond": {"atom1": ["A", int(fields[1]), fields[2]], "atom2": ["B", 1, linker["atom_name"]]}})
    payload = {"version": 1, "sequences": [{"protein": {"id": "A", "sequence": topology_manifest["sequence"], "msa": "empty"}}, {"ligand": {"id": "B", "ccd": ccd_code}}], "constraints": constraints}
    input_path = destination / "boltz_input.yaml"
    input_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    mapping = {"protein_chain": "A", "linker_chain": "B", "ccd_code": ccd_code, "stable_atom_to_boltz": stable_to_boltz, "pickle_runtime": runtime, "cache_pickle": str(cache_target.resolve())}
    _write_json(destination / "boltz_atom_mapping.json", mapping)
    # Keep the public helper contract used by tests and manual integrations:
    # input path, YAML payload, then stable-ID-to-Boltz atom mapping.
    return input_path, payload, mapping


def _parse_pdb(path: Path) -> dict[tuple[str, int, str], tuple[float, float, float]]:
    coordinates: dict[tuple[str, int, str], tuple[float, float, float]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(("ATOM  ", "HETATM")):
            try:
                coordinates[(line[21:22].strip(), int(line[22:26]), line[12:16].strip())] = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError:
                continue
    return coordinates


def _mapped_targets(topology: Any, mapping: dict[str, Any], coordinates: dict[tuple[str, int, str], tuple[float, float, float]]) -> dict[int, tuple[float, float, float]]:
    targets: dict[int, tuple[float, float, float]] = {}
    for atom in topology.GetAtoms():
        if atom.GetAtomicNum() <= 1 or not atom.HasProp(_STABLE_ID):
            continue
        stable_id = atom.GetProp(_STABLE_ID)
        if stable_id.startswith("PEP:"):
            _, residue, atom_name = stable_id.split(":")
            key = ("A", int(residue), atom_name)
        elif stable_id in mapping["stable_atom_to_boltz"]:
            entry = mapping["stable_atom_to_boltz"][stable_id]
            key = (entry["chain_id"], int(entry["residue_index"]), entry["atom_name"])
        else:
            continue
        if key in coordinates:
            targets[atom.GetIdx()] = coordinates[key]
    return targets


def _completion_template(Chem: Any, AllChem: Any, topology: Any, seed: int) -> Any:
    template = Chem.Mol(topology)
    # The topology SDF already carries coordinates.  They are sufficient as a
    # completion template because the Boltz heavy atoms are subsequently imposed
    # exactly.  Avoid an unnecessary (and fragile) macrocycle ETKDG embedding for
    # charged sulfonium systems when a conformer is already available.
    if template.GetNumConformers():
        positions = template.GetConformer().GetPositions()
        if all(math.isfinite(float(value)) for row in positions for value in row):
            return template
    template.RemoveAllConformers()
    params = AllChem.ETKDGv3()
    params.randomSeed = int(seed & 0x7FFFFFFF)
    params.useRandomCoords = True
    params.enforceChirality = True
    params.useMacrocycleTorsions = True
    params.useMacrocycle14config = True
    if int(AllChem.EmbedMolecule(template, params)) != 0:
        raise RuntimeError("RDKit could not embed a coordinate-completion template.")
    return template


def _rigid_transform(np: Any, source: Any, target: Any) -> tuple[Any, Any, Any]:
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    left, _, right_t = np.linalg.svd((source - source_center).T @ (target - target_center))
    rotation = left @ right_t
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right_t
    return source_center, rotation, target_center


def _refit_terminal_caps(np: Any, molecule: Any, template: Any, targets: dict[int, tuple[float, float, float]]) -> None:
    """Place unmapped terminal caps in the local frame of their projected peptide end.

    Boltz receives the peptide sequence, not BioMA's acetyl and primary-amide
    cap atoms.  A global completion-template transform therefore leaves those
    cap fragments disconnected after a non-rigid peptide prediction.  Refit
    each cap rigidly from its adjacent N--CA--C backbone frame instead.
    """
    template_conf = template.GetConformer()
    conformer = molecule.GetConformer()
    stable_to_index = {
        atom.GetProp(_STABLE_ID): atom.GetIdx()
        for atom in molecule.GetAtoms()
        if atom.HasProp(_STABLE_ID)
    }
    residue_indices = sorted(
        {
            int(stable_id.split(":")[1])
            for stable_id in stable_to_index
            if stable_id.startswith("PEP:")
            and len(stable_id.split(":")) == 3
            and stable_id.split(":")[1].isdigit()
        }
    )
    if not residue_indices:
        return
    first_residue, last_residue = residue_indices[0], residue_indices[-1]
    terminal_specs = (
        ("CAP:N:", tuple(f"PEP:{first_residue}:{atom_name}" for atom_name in ("N", "CA", "C"))),
        ("CAP:C:", tuple(f"PEP:{last_residue}:{atom_name}" for atom_name in ("N", "CA", "C"))),
    )
    for cap_prefix, anchor_ids in terminal_specs:
        anchor_indices = [stable_to_index.get(stable_id) for stable_id in anchor_ids]
        if any(index is None or index not in targets for index in anchor_indices):
            continue
        indices = [int(index) for index in anchor_indices]
        source = np.asarray(
            [
                [
                    template_conf.GetAtomPosition(index).x,
                    template_conf.GetAtomPosition(index).y,
                    template_conf.GetAtomPosition(index).z,
                ]
                for index in indices
            ]
        )
        target = np.asarray([targets[index] for index in indices])
        source_center, rotation, target_center = _rigid_transform(np, source, target)
        for atom in molecule.GetAtoms():
            if not atom.HasProp(_STABLE_ID) or not atom.GetProp(_STABLE_ID).startswith(cap_prefix):
                continue
            point = template_conf.GetAtomPosition(atom.GetIdx())
            xyz = (
                np.asarray([point.x, point.y, point.z]) - source_center
            ) @ rotation + target_center
            conformer.SetAtomPosition(atom.GetIdx(), xyz.tolist())


def _project(Chem: Any, np: Any, template: Any, targets: dict[int, tuple[float, float, float]]) -> Any:
    if len(targets) < 3:
        raise RuntimeError("Boltz supplied fewer than three mapped heavy atoms.")
    molecule = Chem.Mol(template)
    conformer = molecule.GetConformer()
    indices = sorted(targets)
    source = np.asarray([[conformer.GetAtomPosition(i).x, conformer.GetAtomPosition(i).y, conformer.GetAtomPosition(i).z] for i in indices])
    target = np.asarray([targets[i] for i in indices])
    source_center, rotation, target_center = _rigid_transform(np, source, target)
    for index in range(molecule.GetNumAtoms()):
        point = conformer.GetAtomPosition(index)
        xyz = (np.asarray([point.x, point.y, point.z]) - source_center) @ rotation + target_center
        conformer.SetAtomPosition(index, xyz.tolist())
    for index, xyz in targets.items():
        conformer.SetAtomPosition(index, xyz)
    _refit_terminal_caps(np, molecule, template, targets)
    return Chem.AddHs(molecule, addCoords=True)


def _confidence(path: Path) -> float | None:
    for candidate in sorted(path.parent.glob("*confidence*.json")):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("confidence_score", "complex_plddt", "ptm", "plddt"):
            if isinstance(value.get(key), (int, float)) and math.isfinite(float(value[key])):
                return float(value[key])
    return None


def _accelerator(requested: str) -> str:
    if requested != "auto":
        return requested
    executable = shutil.which("nvidia-smi")
    if executable:
        result = subprocess.run([executable, "-L"], capture_output=True, text=True, check=False, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return "gpu"
    return "cpu"


def _boltz_environment(accelerator: str) -> dict[str, str]:
    """Return an isolated runtime environment for a Boltz invocation.

    Boltz2 probes ``torch.cuda`` during model setup even when its Lightning
    trainer is configured for CPU. On a host with an incompatible CUDA driver,
    the probe fails unless CUDA is hidden from the child process. Keep this
    scoped to Boltz so RDKit and xTB retain the ordinary parent environment.
    """
    environment = os.environ.copy()
    if accelerator == "cpu":
        environment["CUDA_VISIBLE_DEVICES"] = ""
    return environment


def _ensemble_hash(molecules: list[Any]) -> str:
    return _hash([{ "candidate_id": mol.GetProp("candidate_id"), "coordinates": [[round(float(value), 8) for value in row] for row in mol.GetConformer().GetPositions()]} for mol in molecules])


@bioma_tool(
    name="generate_boltz_conformer_ensemble",
    description="Generate Boltz2 custom-CCD coordinate proposals for a validated crosslinked-peptide topology and project them onto the RDKit master graph.",
    category="analysis",
    version="1.0.0",
    author="BioMA",
    args_schema=BoltzConformerEnsembleInput,
    tags=["boltz", "crosslinked-peptide", "conformer", "ensemble", "3d"],
    requires_network=True,
)
def generate_boltz_conformer_ensemble(topology_manifest_path: str, target_ensemble_size: int, output_dir: str, boltz_options_json: str = "{}") -> str:
    contract = require_final_target(target_ensemble_size, stage="boltz_raw")
    Chem, AllChem, rd_base, np = _rdkit()
    options = _json_object(boltz_options_json, "boltz_options_json")
    manifest_file = Path(topology_manifest_path).expanduser().resolve()
    topology_manifest = _load_json(manifest_file)
    if topology_manifest.get("status") != "ok":
        raise ToolError("The topology manifest must have status 'ok'.", error_type="invalid_input", retriable=False, backend="Boltz")
    topology = _read_sdf(Chem, Path(topology_manifest["artifacts"]["topology_sdf"]).resolve())[0]
    _apply_atom_map(topology, Path(topology_manifest["artifacts"]["atom_map_json"]).resolve())
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(str(options.get("cache_dir", ".boltz_cache"))).expanduser().resolve()
    seed_count = int(options.get("seed_count", 4))
    diffusion_samples = int(options.get("diffusion_samples", 4))
    raw_candidates = int(options.get("raw_candidates", seed_count * diffusion_samples))
    base_seed = int(options.get("base_seed", 20260922))
    sampling_steps = int(options.get("sampling_steps", 200))
    recycling_steps = int(options.get("recycling_steps", 3))
    max_parallel_samples = int(options.get("max_parallel_samples", diffusion_samples))
    timeout_seconds = int(options.get("timeout_seconds", 21600))
    coverage_minimum = float(options.get("minimum_coordinate_coverage", 0.90))
    if min(seed_count, diffusion_samples, raw_candidates, sampling_steps, recycling_steps, max_parallel_samples, timeout_seconds) < 1 or not 0 < coverage_minimum <= 1:
        raise ToolError("Boltz sampling counts must be positive and minimum_coordinate_coverage must be in (0, 1].", error_type="invalid_input", retriable=False, backend="Boltz")
    prepared = options.get("prepared_output_dir")
    started = time.perf_counter()
    input_path: Path | None = None
    mapping: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    run_records: list[dict[str, Any]] = []
    pdb_paths: list[Path] = []
    preparation_error: str | None = None
    try:
        if not prepared:
            _ensure_molecule_cache(cache_dir)
        boltz_command = str(options.get("boltz_command", "boltz"))
        input_path, payload, mapping = _prepare_boltz_input(Chem, AllChem, topology, topology_manifest, destination, cache_dir, base_seed, _infer_boltz_python_command(boltz_command, options.get("boltz_python_command")))
        if prepared:
            pdb_paths = sorted(Path(str(prepared)).expanduser().resolve().rglob("*.pdb"))
        else:
            command_parts = shlex.split(boltz_command)
            executable = shutil.which(command_parts[0]) if command_parts else None
            if executable is None:
                raise RuntimeError("Boltz is not installed or boltz_command is not executable.")
            command_parts[0] = executable
            accelerator = _accelerator(str(options.get("accelerator", "auto")))
            boltz_environment = _boltz_environment(accelerator)
            for seed_index in range(seed_count):
                seed = int((base_seed + seed_index * 104729) & 0x7FFFFFFF)
                seed_dir = destination / "runs" / f"seed_{seed_index + 1:02d}"
                seed_dir.mkdir(parents=True, exist_ok=True)
                command = [*command_parts, "predict", str(input_path), "--out_dir", str(seed_dir), "--cache", str(cache_dir), "--diffusion_samples", str(diffusion_samples), "--max_parallel_samples", str(max_parallel_samples), "--sampling_steps", str(sampling_steps), "--recycling_steps", str(recycling_steps), "--step_scale", str(float(options.get("step_scale", 1.5))), "--output_format", "pdb", "--model", "boltz2", "--accelerator", accelerator, "--devices", str(int(options.get("devices", 1))), "--preprocessing-threads", str(int(options.get("preprocessing_threads", 0))), "--seed", str(seed), "--override"]
                if bool(options.get("use_potentials", True)):
                    command.append("--use_potentials")
                clock = time.perf_counter()
                try:
                    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout_seconds, env=boltz_environment)
                    return_code, stderr, stdout = completed.returncode, completed.stderr or "", completed.stdout or ""
                except subprocess.TimeoutExpired as exc:
                    return_code, stderr, stdout = None, f"timeout after {timeout_seconds}s: {exc}", ""
                (seed_dir / "boltz.stdout.log").write_text(stdout, encoding="utf-8")
                (seed_dir / "boltz.stderr.log").write_text(stderr, encoding="utf-8")
                seed_pdbs = sorted(seed_dir.rglob("*.pdb"))
                pdb_paths.extend(seed_pdbs)
                run_records.append({"seed_index": seed_index + 1, "seed": seed, "return_code": return_code, "duration_seconds": time.perf_counter() - clock, "pdb_count": len(seed_pdbs), "command": command, "cuda_visible_devices": boltz_environment.get("CUDA_VISIBLE_DEVICES")})
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        preparation_error = f"{type(exc).__name__}: {exc}"
    pdb_paths = pdb_paths[:raw_candidates]
    candidates: list[Any] = []
    projection_records: list[dict[str, Any]] = []
    if mapping is not None:
        try:
            template = _completion_template(Chem, AllChem, topology, base_seed + 7919)
            expected = sum(1 for atom in topology.GetAtoms() if atom.GetAtomicNum() > 1 and atom.HasProp(_STABLE_ID) and (atom.GetProp(_STABLE_ID).startswith("PEP:") or atom.GetProp(_STABLE_ID) in mapping["stable_atom_to_boltz"]))
            for sample_index, pdb_path in enumerate(pdb_paths, start=1):
                candidate_id = f"boltz:sample_{sample_index:04d}"
                try:
                    targets = _mapped_targets(topology, mapping, _parse_pdb(pdb_path))
                    coverage = len(targets) / expected if expected else 0.0
                    if coverage < coverage_minimum:
                        raise RuntimeError(f"coordinate coverage {coverage:.3f} is below {coverage_minimum:.3f}")
                    molecule = _project(Chem, np, template, targets)
                    confidence = _confidence(pdb_path)
                    molecule.SetProp("candidate_id", candidate_id)
                    molecule.SetProp("source_candidate_id", f"sample_{sample_index:04d}")
                    molecule.SetProp("source_backend", "boltz")
                    molecule.SetProp("source_manifest", str(destination / "boltz_raw_manifest.json"))
                    molecule.SetProp("source_pdb", str(pdb_path.resolve()))
                    molecule.SetProp("boltz_seed", str(base_seed))
                    molecule.SetProp("boltz_model_index", str(sample_index))
                    molecule.SetProp("coordinate_coverage", str(coverage))
                    molecule.SetProp("bioma_chemical_hash", str(topology_manifest["chemical_hash"]))
                    if confidence is not None:
                        molecule.SetProp("boltz_confidence", str(confidence))
                    _create_atom_property_list(Chem, molecule)
                    candidates.append(molecule)
                    projection_records.append({"candidate_id": candidate_id, "status": "success", "source_pdb": str(pdb_path.resolve()), "coordinate_coverage": coverage, "boltz_confidence": confidence})
                except (OSError, ValueError, RuntimeError) as exc:
                    projection_records.append({"candidate_id": candidate_id, "status": "failed", "source_pdb": str(pdb_path.resolve()), "error": f"{type(exc).__name__}: {exc}"})
        except (OSError, RuntimeError, ValueError) as exc:
            preparation_error = preparation_error or f"{type(exc).__name__}: {exc}"
    sdf_path = destination / "boltz_raw_conformers.sdf"
    if candidates:
        writer = Chem.SDWriter(str(sdf_path))
        for molecule in candidates:
            writer.write(molecule)
        writer.close()
    status = "ok" if candidates and len(candidates) == len(pdb_paths) and preparation_error is None else "partial_success" if candidates else "failed"
    warnings: list[str] = []
    if preparation_error:
        warnings.append(preparation_error)
    if status == "partial_success":
        warnings.append("Only valid Boltz projections are available; merge them with RDKit raw conformers.")
    if status == "failed":
        warnings.append("Boltz route is unavailable. Continue the workflow with the RDKit raw manifest only.")
    sampling = {"backend": "boltz", "raw_candidates": raw_candidates, "seed_count": seed_count, "base_seed": base_seed, "diffusion_samples": diffusion_samples, "sampling_steps": sampling_steps, "recycling_steps": recycling_steps, "max_parallel_samples": max_parallel_samples, "minimum_coordinate_coverage": coverage_minimum}
    sampling_record = {"input_yaml": str(input_path) if input_path else None, "input_payload": payload, "mapping_json": str(destination / "boltz_atom_mapping.json"), "requested_raw_candidates": raw_candidates, "pdb_count": len(pdb_paths), "projected_count": len(candidates), "failed_projection_count": len(pdb_paths) - len(candidates), "runs": run_records, "projections": projection_records, "preparation_error": preparation_error, "duration_seconds": time.perf_counter() - started}
    sampling_path = destination / "boltz_sampling.json"
    _write_json(sampling_path, sampling_record)
    ensemble_hash = _ensemble_hash(candidates) if candidates else None
    result_hash = _hash({"status": status, "candidate_ids": [mol.GetProp("candidate_id") for mol in candidates], "ensemble_hash": ensemble_hash, "preparation_error": preparation_error})
    manifest_path = destination / "boltz_raw_manifest.json"
    artifacts: dict[str, str] = {"boltz_sampling_json": str(sampling_path), "boltz_atom_mapping_json": str(destination / "boltz_atom_mapping.json")}
    if input_path:
        artifacts["boltz_input_yaml"] = str(input_path)
    if sdf_path.is_file():
        artifacts["conformers_sdf"] = str(sdf_path)
    manifest = {"contract_version": "1.0", "tool_name": "generate_boltz_conformer_ensemble", "tool_version": "1.0.0", "stage": "boltz_raw", "status": status, "chemical_hash": topology_manifest["chemical_hash"], "input_hash": topology_manifest["config_hash"], "config_hash": _hash({"input": topology_manifest["config_hash"], "sampling": sampling, "rdkit": rd_base.rdkitVersion, "ensemble_contract": contract_hash(contract)}), "result_hash": result_hash, "parent_manifest": str(manifest_file), "parent_manifests": [str(manifest_file)], "topology_manifest": str(manifest_file), "target_ensemble_size": target_ensemble_size, "ensemble_contract_hash": contract_hash(contract), "candidate_count": len(candidates), "candidate_ids": [mol.GetProp("candidate_id") for mol in candidates], "sampling": sampling, "source_backend": "boltz", "artifacts": artifacts, "artifact_hashes": {"ensemble_scientific_sha256": ensemble_hash}, "warnings": warnings, "recommended_action": "continue_with_rdkit_only" if status == "failed" else "validate_available_boltz_results" if status == "partial_success" else None, "implementation": {"rdkit_version": rd_base.rdkitVersion}}
    _write_json(manifest_path, manifest)
    return json.dumps({"status": status, "manifest_path": str(manifest_path), "conformers_sdf": str(sdf_path) if sdf_path.is_file() else None, "requested_count": raw_candidates, "pdb_count": len(pdb_paths), "projected_count": len(candidates), "seed_count": seed_count, "diffusion_samples": diffusion_samples, "config_hash": manifest["config_hash"], "result_hash": result_hash, "recommended_action": manifest["recommended_action"]}, ensure_ascii=False, indent=2)
