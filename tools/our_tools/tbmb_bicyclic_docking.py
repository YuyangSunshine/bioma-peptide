#!/usr/bin/env python3
"""Six guarded tools for receptor-only TBMB bicyclic-peptide docking.

The public tool inputs deliberately accept one query YAML.  It makes a complete run
replayable and prevents a later tool call from silently using a different ligand graph,
target chain, force-field setup, external executable, or output directory.
"""
from __future__ import annotations

from bioma.peptide.contracts import result_validator as peptide_result_validator
import argparse
import csv
import hashlib
import json
import math
import re
import shlex
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError


class DockingQueryInput(ToolInput):
    query_path: str = Field(description="Path to the complete TBMB docking query YAML.")


def _load(query_path: str) -> tuple[dict[str, Any], Path]:
    source = Path(query_path).expanduser().resolve()
    if not source.is_file():
        raise ToolError(f"Query YAML not found: {source}", error_type="invalid_input", retriable=False, backend="TBMB docking")
    cfg = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    required = ["target_structure", "retain_chains", "ligand_ensemble_sdf", "ligand_metadata", "ligand_reference_pdb", "workdir", "commands"]
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise ToolError(f"Query YAML is missing required fields: {', '.join(missing)}", error_type="invalid_input", retriable=False, backend="TBMB docking")
    if int(cfg.get("workflow_schema_version", 0)) != 2:
        raise ToolError("This six-tool workflow requires workflow_schema_version: 2.", error_type="invalid_input", retriable=False, backend="TBMB docking")
    cfg["_query"] = str(source)
    return cfg, source.parent


def _p(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _paths(cfg: dict[str, Any], base: Path) -> dict[str, Path]:
    work = _p(cfg["workdir"], base)
    return {
        "work": work,
        "clean": work / "01_receptor" / "receptor_clean.pdb",
        "receptor": work / "01_receptor" / "receptor_fixed.pdb",
        "amber_receptor": work / "01_receptor" / "receptor_amber_input.pdb",
        "receptor_atom_map": work / "01_receptor" / "receptor_atom_map.json",
        "audit": work / "01_receptor" / "cleanup_audit.json",
        "p2rank": work / "02_p2rank",
        "pockets": work / "02_p2rank" / "selected_pockets.json",
        "ligands": work / "00_inputs" / "tbmb_sdf",
        "amber_parameters": work / "00_inputs" / "amber_parameters",
        "amber_bundle": work / "00_inputs" / "amber_parameters" / "tbmb_met_modular_v1",
        "diffdock": work / "03_diffdock",
        "prefilter": work / "04_prefilter",
        "relax": work / "05_relax",
        "postfilter": work / "06_postfilter",
        "vina": work / "07_vina",
    }


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _render(template: str, **items: object) -> str:
    return template.format(**{key: shlex.quote(str(value)) for key, value in items.items()})


def _run(command: str, log: Path, cwd: Path | None = None) -> None:
    _mkdir(log.parent)
    print(f"[run] {command}", flush=True)
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            shlex.split(command), shell=False, cwd=cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        for line in process.stdout:
            handle.write(line)
            handle.flush()
        returncode = process.wait()
    if returncode:
        raise ToolError(f"External command failed ({returncode}); see {log}", error_type="unavailable", retriable=False, backend="TBMB docking")


def _json_result(stage: str, **items: Any) -> str:
    return json.dumps({"status": "ok", "stage": stage, **items}, ensure_ascii=False, indent=2)


def _parse_fasta(path: Path) -> str:
    letters = "".join(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith(">"))
    if not letters or re.search(r"[^ACDEFGHIKLMNPQRSTVWY]", letters.upper()):
        raise ToolError(f"Target FASTA is empty or noncanonical: {path}", error_type="invalid_input", retriable=False, backend="TBMB docking")
    return letters.upper()


def _lcs_length(first: str, second: str) -> int:
    """Length of an order-preserving match, allowing unresolved coordinate gaps."""
    previous = [0] * (len(second) + 1)
    for left in first:
        current = [0]
        for index, right in enumerate(second, 1):
            current.append(previous[index - 1] + 1 if left == right else max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def _amber_receptor_input(source: Path, target: Path, atom_map_path: Path) -> tuple[list[dict[str, Any]], int]:
    """Write a hydrogen-free AMBER input PDB and annotate disulfide cysteines.

    Renumber the LEaP-only PDB contiguously from one, so residue selectors and
    recorded AMBER indices cannot disagree when the source PDB has gaps.
    Preserve the original identifiers separately in the atom map and audit.
    """
    atom_lines = [line for line in source.read_text(errors="ignore").splitlines() if line.startswith(("ATOM  ", "HETATM"))]
    residue_order: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    sulfurs: list[tuple[tuple[str, str, str], tuple[float, float, float]]] = []
    for line in atom_lines:
        key = (line[21:22], line[22:26], line[26:27])
        if key not in seen:
            seen.add(key); residue_order.append(key)
        if line[12:16].strip() == "SG" and line[17:20].strip() == "CYS":
            try:
                sulfurs.append((key, (float(line[30:38]), float(line[38:46]), float(line[46:54]))))
            except ValueError:
                pass
    disulfides: list[dict[str, Any]] = []
    paired: set[tuple[str, str, str]] = set()
    residue_index = {key: index for index, key in enumerate(residue_order, 1)}
    candidates = []
    for left in range(len(sulfurs)):
        for right in range(left + 1, len(sulfurs)):
            distance = math.dist(sulfurs[left][1], sulfurs[right][1])
            if 1.7 <= distance <= 2.3:
                candidates.append((distance, sulfurs[left][0], sulfurs[right][0]))
    for distance, first, second in sorted(candidates):
        if first in paired or second in paired:
            continue
        paired.update((first, second))
        disulfides.append({
            "first": {"chain": first[0], "pdb_residue": first[1].strip() + first[2].strip(), "amber_residue_index": residue_index[first]},
            "second": {"chain": second[0], "pdb_residue": second[1].strip() + second[2].strip(), "amber_residue_index": residue_index[second]},
            "sg_distance_angstrom": distance,
        })
    output, atom_map = [], []
    serial = 0
    last_key = None
    for line in source.read_text(errors="ignore").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            if line.startswith("TER"):
                if last_key is not None and len(line) >= 27:
                    line = line[:22] + f"{residue_index[last_key]:4d}" + " " + line[27:]
                output.append(line)
            continue
        element = line[76:78].strip().upper()
        atom_name = line[12:16].strip().upper()
        if element == "H" or (not element and atom_name.startswith("H")):
            continue
        key = (line[21:22], line[22:26], line[26:27])
        original_pdb_residue = line[22:26].strip() + line[26:27].strip()
        if key in paired and line[17:20].strip() == "CYS":
            line = line[:17] + "CYX" + line[20:]
        serial += 1
        line = line[:6] + f"{serial:5d}" + line[11:]
        line = line[:22] + f"{residue_index[key]:4d}" + " " + line[27:]
        output.append(line)
        last_key = key
        atom_map.append({
            "amber_atom_index": serial,
            "atom_name": line[12:16].strip(),
            "residue_name": line[17:20].strip(),
            "chain": line[21:22].strip(),
            "pdb_residue": original_pdb_residue,
            "amber_residue_index": residue_index[key],
        })
    output.append("END")
    target.write_text("\n".join(output) + "\n", encoding="utf-8")
    atom_map_path.write_text(json.dumps(atom_map, ensure_ascii=False, indent=2), encoding="utf-8")
    return disulfides, len(atom_map)


def _prepare(cfg: dict[str, Any], base: Path) -> dict[str, Any]:
    """Keep selected polymer chains only, repair receptor, and record an audit."""
    try:
        from Bio.PDB import MMCIFParser, PDBIO, PDBParser, Select
        from Bio.Data.PDBData import protein_letters_3to1
        from pdbfixer import PDBFixer
        from openmm.app import PDBFile
    except ImportError as exc:
        raise ToolError("Biopython, PDBFixer, and OpenMM are required in the execution environment.", error_type="unavailable", retriable=False, backend="prepare_receptor") from exc
    target = _p(cfg["target_structure"], base)
    if not target.is_file():
        raise ToolError(f"Target structure not found: {target}", error_type="invalid_input", retriable=False, backend="prepare_receptor")
    keep = set(str(x) for x in cfg["retain_chains"])
    parser = MMCIFParser(QUIET=True) if target.suffix.lower() in {".cif", ".mmcif"} else PDBParser(QUIET=True)
    structure = parser.get_structure("target", str(target))
    model = next(structure.get_models())
    available = {chain.id for chain in model.get_chains()}
    absent = sorted(keep - available)
    if absent:
        raise ToolError(f"Requested retained chains absent: {absent}; available={sorted(available)}", error_type="invalid_input", retriable=False, backend="prepare_receptor")

    class _Selection(Select):
        def accept_chain(self, chain): return chain.id in keep
        def accept_residue(self, residue): return residue.id[0] == " "

    removed, observed = Counter(), []
    for chain in model:
        for residue in chain:
            if chain.id in keep and residue.id[0] == " ":
                observed.append(protein_letters_3to1.get(residue.resname.upper(), "X"))
            else:
                removed[f"{chain.id}:{residue.resname}"] += 1
    requested_sequence = None
    sequence_check = None
    if cfg.get("target_sequence"):
        fasta = _p(cfg["target_sequence"], base)
        if not fasta.is_file():
            raise ToolError(f"Target FASTA not found: {fasta}", error_type="invalid_input", retriable=False, backend="prepare_receptor")
        requested_sequence = _parse_fasta(fasta)
        structural_sequence = "".join(observed)
        comparable_sequence = structural_sequence.replace("X", "")
        matched = _lcs_length(requested_sequence, comparable_sequence)
        coordinate_identity = matched / len(comparable_sequence) if comparable_sequence else 0.0
        fasta_coverage = matched / len(requested_sequence)
        sequence_check = {
            "coordinate_sequence": structural_sequence,
            "coordinate_residue_count": len(structural_sequence),
            "order_preserving_matches": matched,
            "coordinate_sequence_identity": coordinate_identity,
            "fasta_coverage_by_coordinates": fasta_coverage,
            "policy": "Order-preserving comparison permits unresolved coordinate gaps but not a substantially different chain.",
        }
        if coordinate_identity < float(cfg.get("minimum_coordinate_sequence_identity", 0.90)) or fasta_coverage < float(cfg.get("minimum_fasta_coordinate_coverage", 0.70)):
            raise ToolError(
                f"Retained-chain coordinates are incompatible with target FASTA "
                f"(identity={coordinate_identity:.3f}, coverage={fasta_coverage:.3f}); check chain selection.",
                error_type="invalid_input", retriable=False, backend="prepare_receptor")
    paths = _paths(cfg, base)
    _mkdir(paths["clean"].parent)
    io = PDBIO(); io.set_structure(structure); io.save(str(paths["clean"]), _Selection())
    fixer = PDBFixer(filename=str(paths["clean"]))
    fixer.findMissingResidues()
    for key in list(fixer.missingResidues):
        chain_index, residue_index = key
        residues = list(list(fixer.topology.chains())[chain_index].residues())
        if residue_index in {0, len(residues)}:
            del fixer.missingResidues[key]
    fixer.findNonstandardResidues()
    substitutions = [(str(residue), name) for residue, name in fixer.nonstandardResidues]
    fixer.replaceNonstandardResidues(); fixer.findMissingAtoms(); fixer.addMissingAtoms()
    fixer.addMissingHydrogens(float(cfg.get("ph", 7.4)))
    with paths["receptor"].open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(fixer.topology, fixer.positions, handle, keepIds=True)
    disulfides, amber_atom_count = _amber_receptor_input(paths["receptor"], paths["amber_receptor"], paths["receptor_atom_map"])
    audit = {"source": str(target), "retained_chains": sorted(keep), "retained_polymer_residues": len(observed),
             "target_fasta": str(_p(cfg["target_sequence"], base)) if cfg.get("target_sequence") else None,
             "target_fasta_length": len(requested_sequence) if requested_sequence else None,
             "sequence_check": sequence_check,
             "removed_residue_counts": dict(sorted(removed.items())), "nonstandard_replacements": substitutions,
             "amber_input": str(paths["amber_receptor"]), "amber_heavy_atom_count": amber_atom_count,
             "receptor_atom_map": str(paths["receptor_atom_map"]), "disulfide_bonds": disulfides,
             "policy": "Only polymer residues in retain_chains were preserved; all other chains, ligands, solvent, ions, glycans, and buffers were removed."}
    paths["audit"].write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"receptor": str(paths["receptor"]), "amber_receptor": str(paths["amber_receptor"]),
            "audit": str(paths["audit"]), "disulfide_bonds": len(disulfides)}


def _p2rank_rows(csv_path: Path, cutoff: float, maximum: int) -> list[dict[str, Any]]:
    pockets: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            values = {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k is not None}
            try:
                probability = float(values.get("probability", values.get("prob.", "")))
                center = [float(values[x]) for x in ("center_x", "center_y", "center_z")]
            except (KeyError, ValueError):
                continue
            if probability >= cutoff:
                pockets.append({"rank": int(float(values.get("rank", "999"))), "probability": probability, "center": center,
                                "score": values.get("score", ""), "residues": values.get("residue_ids", values.get("residues", ""))})
    pockets.sort(key=lambda item: (-item["probability"], item["rank"]))
    for index, pocket in enumerate(pockets[:maximum], 1): pocket["pocket_id"] = f"pocket_{index:02d}"
    return pockets[:maximum]


def _detect(cfg: dict[str, Any], base: Path) -> dict[str, Any]:
    paths = _paths(cfg, base)
    if not paths["receptor"].is_file():
        raise ToolError("Prepared receptor missing; run prepare_receptor first.", error_type="invalid_input", retriable=False, backend="P2Rank")
    _mkdir(paths["p2rank"])
    command = _render(cfg["commands"]["p2rank"], receptor=paths["receptor"], output=paths["p2rank"])
    _run(command, paths["p2rank"] / "p2rank.log")
    csvs = sorted(paths["p2rank"].rglob("*predictions.csv"))
    if not csvs:
        raise ToolError("P2Rank produced no predictions CSV; see p2rank.log.", error_type="unavailable", retriable=False, backend="P2Rank")
    pockets = _p2rank_rows(csvs[0], float(cfg.get("p2rank_min_probability", 0.05)), int(cfg.get("max_pockets", 5)))
    if not pockets:
        raise ToolError("No P2Rank pocket passed the configured probability cutoff.", error_type="invalid_input", retriable=False, backend="P2Rank")
    payload = {"source_csv": str(csvs[0]), "requested_pockets": int(cfg.get("pocket_count", 3)), "pockets": pockets,
               "notice": "P2Rank returned fewer than requested pockets" if len(pockets) < int(cfg.get("pocket_count", 3)) else None}
    paths["pockets"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"pockets_json": str(paths["pockets"]), "pocket_count": len(pockets), "notice": payload["notice"]}


def _signature(mol) -> tuple[Any, Any]:
    return (tuple((atom.GetAtomicNum(), atom.GetFormalCharge(), atom.GetIsotope()) for atom in mol.GetAtoms()),
            tuple(sorted((min(b.GetBeginAtomIdx(), b.GetEndAtomIdx()), max(b.GetBeginAtomIdx(), b.GetEndAtomIdx()), b.GetBondTypeAsDouble(), b.GetIsAromatic()) for b in mol.GetBonds())))


def _read_xyz_pdb(pdb: Path) -> list[tuple[float, float, float]]:
    points = []
    for line in pdb.read_text(errors="ignore").splitlines():
        if line.startswith(("ATOM", "HETATM")) and line[76:78].strip().upper() != "H":
            try: points.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
            except ValueError: pass
    return points


def _confidence(name: str) -> float:
    match = re.search(r"confidence([-+]?\d+(?:\.\d+)?)", name, re.IGNORECASE)
    return float(match.group(1)) if match else float("nan")


def _regraph_and_metrics(pose: Path, reference, receptor_xyz: list[tuple[float, float, float]], output: Path) -> tuple[Path, bool, dict[str, Any]]:
    from rdkit import Chem
    docked = Chem.MolFromMolFile(str(pose), removeHs=False)
    if docked is None or docked.GetNumConformers() == 0:
        raise ValueError("unreadable SDF")
    ref_atoms = [(atom.GetAtomicNum(), atom.GetIsotope()) for atom in reference.GetAtoms()]
    dock_atoms = [(atom.GetAtomicNum(), atom.GetIsotope()) for atom in docked.GetAtoms()]
    if ref_atoms != dock_atoms:
        raise ValueError("DiffDock atom order/elements cannot map to authoritative TBMB SDF")
    remapped = False
    checked = docked
    target = pose
    if _signature(docked) != _signature(reference):
        checked = Chem.Mol(reference); new_conf = Chem.Conformer(reference.GetNumAtoms()); old_conf = docked.GetConformer(); new_conf.Set3D(True)
        for index in range(reference.GetNumAtoms()): new_conf.SetAtomPosition(index, old_conf.GetAtomPosition(index))
        checked.RemoveAllConformers(); checked.AddConformer(new_conf, assignId=True)
        target = _mkdir(output) / f"{pose.parents[1].name}_{pose.stem}_authoritative_graph.sdf"
        writer = Chem.SDWriter(str(target)); writer.write(checked); writer.close(); remapped = True
    conf = checked.GetConformer()
    ligand_xyz = [(point.x, point.y, point.z) for atom in checked.GetAtoms() if atom.GetAtomicNum() > 1 for point in [conf.GetAtomPosition(atom.GetIdx())]]
    distances = [math.dist(a, b) for a in ligand_xyz for b in receptor_xyz]
    periodic = Chem.GetPeriodicTable(); bad_bonds = 0
    for bond in checked.GetBonds():
        a, b = bond.GetBeginAtom(), bond.GetEndAtom()
        if 1 in {a.GetAtomicNum(), b.GetAtomicNum()}: continue
        pa, pb = conf.GetAtomPosition(a.GetIdx()), conf.GetAtomPosition(b.GetIdx())
        distance = math.dist((pa.x, pa.y, pa.z), (pb.x, pb.y, pb.z)); expected = periodic.GetRcovalent(a.GetAtomicNum()) + periodic.GetRcovalent(b.GetAtomicNum())
        bad_bonds += int(distance < .65 * expected or distance > expected + .45)
    centroid = [sum(point[axis] for point in ligand_xyz) / len(ligand_xyz) for axis in range(3)]
    return target, remapped, {"topology_matches_authoritative_sdf": _signature(checked) == _signature(reference),
            "formal_charge_plus_5": Chem.GetFormalCharge(checked) == Chem.GetFormalCharge(reference) == 5,
            "minimum_receptor_distance": min(distances), "clash_pairs_under_1p5A": sum(d < 1.5 for d in distances),
            "contact_pairs_2p5_to_4p5A": sum(2.5 <= d <= 4.5 for d in distances), "bad_heavy_atom_covalent_bond_count": bad_bonds, "ligand_centroid": centroid}


def _screen(cfg: dict[str, Any], base: Path) -> dict[str, Any]:
    """Run global DiffDock and apply only pre-relaxation chemistry/gross-clash gates."""
    try:
        from rdkit import Chem
    except ImportError as exc:
        raise ToolError("RDKit is required for TBMB graph validation.", error_type="unavailable", retriable=False, backend="DiffDock") from exc
    paths = _paths(cfg, base)
    for needed in (paths["receptor"], paths["pockets"], _p(cfg["ligand_ensemble_sdf"], base), _p(cfg["ligand_metadata"], base)):
        if not needed.is_file(): raise ToolError(f"Required prior artifact/input missing: {needed}", error_type="invalid_input", retriable=False, backend="DiffDock")
    metadata = json.loads(_p(cfg["ligand_metadata"], base).read_text(encoding="utf-8"))
    text_metadata = json.dumps(metadata)
    if not all(marker in text_metadata for marker in ("CM1", "CM2", "CM3")):
        raise ToolError("Ligand metadata lacks the required TBMB crosslink anchors CM1/CM2/CM3.", error_type="invalid_input", retriable=False, backend="DiffDock")
    molecules = [mol for mol in Chem.SDMolSupplier(str(_p(cfg["ligand_ensemble_sdf"], base)), removeHs=False) if mol]
    expected = int(cfg.get("expected_conformers", 10))
    if len(molecules) != expected: raise ToolError(f"Ensemble SDF contains {len(molecules)} readable conformers; expected {expected}.", error_type="invalid_input", retriable=False, backend="DiffDock")
    if any(_signature(molecule) != _signature(molecules[0]) for molecule in molecules[1:]):
        raise ToolError("The ensemble SDF conformers do not share one authoritative TBMB molecular graph.", error_type="invalid_input", retriable=False, backend="DiffDock")
    _mkdir(paths["ligands"]); _mkdir(paths["diffdock"])
    references = []
    for index, molecule in enumerate(molecules, 1):
        path = paths["ligands"] / f"TBMB_conf_{index:03d}.sdf"; writer = Chem.SDWriter(str(path)); writer.write(molecule); writer.close(); references.append(path)
    reuse_existing = bool(cfg.get("reuse_existing_diffdock_results", False))
    reused_jobs = 0
    for ligand in references:
        job = _mkdir(paths["diffdock"] / "global" / ligand.stem)
        if reuse_existing and any(job.rglob("*confidence*.sdf")):
            reused_jobs += 1
            continue
        _run(_render(cfg["commands"]["diffdock"], receptor=paths["receptor"], ligand=ligand, output=job), job / "diffdock.log")
    pockets = json.loads(paths["pockets"].read_text(encoding="utf-8"))["pockets"]
    receptor_xyz = _read_xyz_pdb(paths["receptor"])
    reference = molecules[0]  # all conformers must share the one authoritative molecular graph
    if not receptor_xyz: raise ToolError("Prepared receptor has no heavy-atom coordinates.", error_type="invalid_input", retriable=False, backend="DiffDock")
    accepted, rejected = [], []
    max_pocket = float(cfg.get("pre_relax_max_pocket_centroid_distance", 12.0))
    min_distance = float(cfg.get("pre_relax_min_heavy_atom_distance", 0.8))
    for pose in sorted(paths["diffdock"].rglob("*.sdf")):
        score = _confidence(pose.name)
        if math.isnan(score): rejected.append({"pose": str(pose), "reason": "unscored DiffDock duplicate pose"}); continue
        if score <= -999: rejected.append({"pose": str(pose), "reason": "DiffDock failed-pose sentinel confidence", "confidence": score}); continue
        try: checked, remapped, metrics = _regraph_and_metrics(pose, reference, receptor_xyz, paths["prefilter"] / "regraphed_poses")
        except ValueError as exc: rejected.append({"pose": str(pose), "reason": str(exc), "confidence": score}); continue
        pocket = min(pockets, key=lambda item: math.dist(metrics["ligand_centroid"], item["center"]))
        metrics["pocket_centroid_distance"] = math.dist(metrics["ligand_centroid"], pocket["center"])
        failures = []
        if not metrics["topology_matches_authoritative_sdf"]: failures.append("topology differs from authoritative TBMB SDF")
        if not metrics["formal_charge_plus_5"]: failures.append("formal charge is not +5")
        if metrics["minimum_receptor_distance"] < min_distance: failures.append("unrecoverable pre-relaxation receptor-ligand overlap")
        if metrics["bad_heavy_atom_covalent_bond_count"]: failures.append("invalid internal covalent-bond geometry")
        if metrics["pocket_centroid_distance"] > max_pocket: failures.append("ligand centroid outside nearest P2Rank pocket")
        item = {"pose": str(checked), "original_pose": str(pose), "topology_reconstructed_from_authoritative_sdf": remapped,
                "pocket_id": pocket["pocket_id"], "diffdock_confidence": score, **metrics}
        if failures: rejected.append({"reason": "; ".join(failures), **item})
        else: item["preliminary_score"] = score + .01 * metrics["contact_pairs_2p5_to_4p5A"]; accepted.append(item)
    accepted.sort(key=lambda item: item["preliminary_score"], reverse=True)
    _mkdir(paths["prefilter"])
    keep = int(cfg.get("keep_before_relax", 15)); shortlist = accepted[:keep]
    candidates_path = paths["prefilter"] / "relaxation_candidates.json"
    rejected_path = paths["prefilter"] / "rejected_poses.json"
    candidates_path.write_text(json.dumps(shortlist, ensure_ascii=False, indent=2), encoding="utf-8")
    rejected_path.write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"relaxation_candidates": str(candidates_path), "shortlisted_for_relaxation": len(shortlist),
            "rejected_poses": str(rejected_path), "rejected": len(rejected), "reused_diffdock_jobs": reused_jobs,
            "notice": "Fewer poses passed hard gates than requested" if len(shortlist) < keep else None}


def _required_command(cfg: dict[str, Any], name: str, backend: str) -> str:
    command = (cfg.get("commands") or {}).get(name)
    if not command:
        raise ToolError(f"Query commands.{name} is required.", error_type="invalid_input", retriable=False, backend=backend)
    return str(command)


def _sulfonium_crosslink_count(molecule) -> int:
    return sum(atom.GetAtomicNum() == 16 and atom.GetFormalCharge() == 1 and atom.GetDegree() == 3 for atom in molecule.GetAtoms())


def _frcmod_problems(path: Path) -> list[str]:
    """Find explicit missing-parameter markers and zero force constants."""
    problems: list[str] = []
    section = ""
    for number, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.strip()
        upper = line.upper()
        if upper in {"MASS", "BOND", "ANGLE", "DIHE", "IMPROPER", "NONBON"}:
            section = upper
            continue
        if "ATTN" in upper or "NEED REVISION" in upper:
            problems.append(f"line {number}: {line}")
            continue
        if not line or section not in {"BOND", "ANGLE", "DIHE", "IMPROPER"}:
            continue
        numbers = [float(value) for value in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", line)]
        if not numbers:
            continue
        if section in {"BOND", "ANGLE", "IMPROPER"} and numbers[0] <= 0:
            problems.append(f"line {number}: non-positive {section.lower()} force constant: {line}")
        if section == "DIHE" and len(numbers) >= 2 and numbers[1] <= 0:
            problems.append(f"line {number}: non-positive dihedral barrier: {line}")
    return problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_file(record: Any, manifest_dir: Path, label: str) -> tuple[Path, str]:
    if not isinstance(record, dict) or not record.get("path") or not record.get("sha256"):
        raise ValueError(f"parameter bundle manifest files.{label} requires path and sha256")
    path = _p(str(record["path"]), manifest_dir)
    if not path.is_file():
        raise ValueError(f"parameter bundle file is missing: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"parameter bundle file is empty: {path}")
    expected = str(record["sha256"]).lower()
    observed = _sha256(path)
    if observed != expected:
        raise ValueError(f"SHA-256 mismatch for {label}: expected {expected}, observed {observed}")
    return path, observed


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot read structured file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def _amber_identifier(value: Any, label: str) -> str:
    name = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{1,4}", name):
        raise ValueError(f"{label} must be a one-to-four character AMBER atom/residue identifier")
    return name


def _amber_residue_identifier(value: Any, label: str) -> str:
    name = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{1,3}", name):
        raise ValueError(
            f"{label} must be a one-to-three character residue name for PDB/TLEaP interchange"
        )
    return name


def _required_template_names(cfg: dict[str, Any], metadata: dict[str, Any]) -> set[str]:
    amber = cfg.get("amber") or {}
    variants = amber.get("residue_templates") or {}
    internal = _amber_residue_identifier(variants.get("internal", "MSU"), "amber.residue_templates.internal")
    n_terminal = _amber_residue_identifier(variants.get("n_terminal", "NMU"), "amber.residue_templates.n_terminal")
    c_terminal = _amber_residue_identifier(variants.get("c_terminal", "CMU"), "amber.residue_templates.c_terminal")
    linker = _amber_residue_identifier(variants.get("linker", "TMB"), "amber.residue_templates.linker")
    chemical = metadata.get("chemical_definition") or metadata
    sequence = str(chemical.get("sequence") or metadata.get("sequence") or "").strip().upper()
    positions = [int(value) for value in (amber.get("crosslink_positions") or chemical.get("met_positions") or [])]
    required = {linker}
    for position in positions:
        required.add(n_terminal if position == 1 else c_terminal if position == len(sequence) else internal)
    return required


def _bundle_files(manifest: dict[str, Any], manifest_path: Path) -> tuple[list[dict[str, Any]], list[tuple[Path, str]]]:
    files = manifest.get("files") or {}
    template_records = files.get("residue_templates")
    if not isinstance(template_records, list) or not template_records:
        raise ValueError("parameter bundle files.residue_templates must be a nonempty list")
    templates: list[dict[str, Any]] = []
    for index, record in enumerate(template_records):
        if not isinstance(record, dict) or not record.get("residue_name") or not record.get("format"):
            raise ValueError(f"residue template {index} requires residue_name, format, path, and sha256")
        path, digest = _manifest_file(record, manifest_path.parent, f"residue_templates[{index}]")
        format_name = str(record["format"]).strip().lower()
        if format_name not in {"off", "lib", "prepi", "prep"}:
            raise ValueError(f"unsupported residue-template format {format_name!r}")
        residue_name = _amber_residue_identifier(
            record["residue_name"], f"residue_templates[{index}].residue_name"
        )
        templates.append({"residue_name": residue_name, "format": format_name,
                          "path": str(path), "sha256": digest})
    names = [item["residue_name"] for item in templates]
    if len(names) != len(set(names)):
        raise ValueError("parameter bundle contains duplicate residue template names")
    frcmod_records = files.get("frcmod")
    if not isinstance(frcmod_records, list) or not frcmod_records:
        raise ValueError("parameter bundle files.frcmod must be a nonempty list")
    frcmods = [_manifest_file(record, manifest_path.parent, f"frcmod[{index}]")
               for index, record in enumerate(frcmod_records)]
    return templates, frcmods


def _parameterize_tbmb(cfg: dict[str, Any], base: Path, reference) -> dict[str, Any]:
    """Validate a reusable residue-template bundle for the TBMB-Met scaffold.

    The bundle is intentionally sequence-independent.  Standard residues remain in
    ff14SB; only MSU/NMU/CMU/TMB templates and bonded terms spanning the three
    S--CH2 crosslinks are custom.  Whole-peptide MOL2/AM1-BCC bundles are rejected.
    """
    from rdkit import Chem

    paths = _paths(cfg, base)
    directory = _mkdir(paths["amber_parameters"])
    audit_path = directory / "parameterization_audit.json"
    amber = cfg.get("amber") or {}
    mode = str(amber.get("parameterization_mode", "")).strip().lower()
    if mode != "reusable_tbmb_met_templates":
        raise ToolError(
            "amber.parameterization_mode must be reusable_tbmb_met_templates; "
            "whole-peptide AM1-BCC and implicit fast-charge fallbacks are disabled.",
            error_type="invalid_input", retriable=False, backend="AmberTools",
        )
    manifest_value = amber.get("parameter_bundle_manifest")
    if not manifest_value:
        raise ToolError("amber.parameter_bundle_manifest is required; run parameterize_tbmb_scaffold first.",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    manifest_path = _p(str(manifest_value), base)
    if not manifest_path.is_file():
        raise ToolError(f"Reusable TBMB-Met bundle manifest not found: {manifest_path}; run parameterize first.",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    manifest = _load_mapping(manifest_path)
    metadata = _load_mapping(_p(cfg["ligand_metadata"], base))
    expected_charge = int(amber.get("ligand_net_charge", 5))
    expected_crosslinks = int(amber.get("expected_crosslinks", 3))
    failures: list[str] = []
    if manifest.get("schema_version") != 2:
        failures.append("manifest schema_version must be 2")
    if manifest.get("validated") is not True:
        failures.append("manifest validated must be true")
    if str(manifest.get("parameterization_scope", "")) != "reusable_tbmb_met_scaffold":
        failures.append("manifest parameterization_scope must be reusable_tbmb_met_scaffold")
    if str(manifest.get("standard_residue_forcefield", "")).lower() != "ff14sb":
        failures.append("manifest standard_residue_forcefield must be ff14SB")
    allowed = manifest.get("allowed_uses")
    if not isinstance(allowed, list) or "restrained_minimization" not in allowed:
        failures.append("manifest allowed_uses must include restrained_minimization")
    scaffold = manifest.get("scaffold") or {}
    if int(scaffold.get("crosslink_count", -1)) != expected_crosslinks:
        failures.append(f"manifest scaffold.crosslink_count must be {expected_crosslinks}")
    if str(scaffold.get("methionine_atom", "")).upper() != "SD":
        failures.append("manifest scaffold.methionine_atom must be SD")
    linker_atoms = [str(value).upper() for value in scaffold.get("linker_atoms", [])]
    if len(linker_atoms) != expected_crosslinks or len(set(linker_atoms)) != expected_crosslinks:
        failures.append("manifest scaffold.linker_atoms must contain three unique atoms")
    charge = manifest.get("charge_derivation") or {}
    if "resp" not in str(charge.get("method", "")).lower():
        failures.append("custom charges must use RESP")
    if int(charge.get("conformer_count", 0)) < 2:
        failures.append("multi-conformer RESP requires at least two conformers")
    if charge.get("electronic_convergence_verified") is not True:
        failures.append("RESP source electronic convergence must be verified")
    if charge.get("charge_constraints_verified") is not True:
        failures.append("RESP residue/equivalence charge constraints must be verified")
    if (charge.get("boundary_reconciliation") or {}).get("reviewed") is not True:
        failures.append("RESP-to-template boundary charge reconciliation must be reviewed")
    bonded = manifest.get("bonded_parameter_derivation") or {}
    if bonded.get("reviewed") is not True:
        failures.append("crosslink bonded parameters must be reviewed")
    if bonded.get("peptide_boundary_terms_reviewed") is not True:
        failures.append("ff14SB/non-standard peptide-boundary terms must be reviewed")
    assembly = manifest.get("assembly_test") or {}
    if assembly.get("passed") is not True:
        failures.append("bundle must pass the recorded TLEaP assembly test")

    templates: list[dict[str, Any]] = []
    frcmods_with_hash: list[tuple[Path, str]] = []
    try:
        templates, frcmods_with_hash = _bundle_files(manifest, manifest_path)
    except ValueError as exc:
        failures.append(str(exc))
    available_names = {item["residue_name"] for item in templates}
    missing_names = sorted(_required_template_names(cfg, metadata) - available_names)
    if missing_names:
        failures.append("missing residue templates required by this sequence: " + ", ".join(missing_names))
    problems = {str(path): _frcmod_problems(path) for path, _ in frcmods_with_hash}
    if any(problems.values()) and bool(amber.get("reject_missing_parameters", True)):
        failures.append("one or more FRCMOD files contain missing or non-positive bonded parameters")

    crosslinks = _sulfonium_crosslink_count(reference)
    formal_charge = Chem.GetFormalCharge(reference)
    if formal_charge != expected_charge:
        failures.append(f"authoritative SDF formal charge is {formal_charge}, expected {expected_charge}")
    if crosslinks != expected_crosslinks:
        failures.append(f"authoritative SDF has {crosslinks} three-coordinate +1 sulfonium centers, expected {expected_crosslinks}")
    audit = {
        "validated": not failures, "parameterization_mode": mode,
        "parameterization_scope": manifest.get("parameterization_scope"),
        "standard_residue_forcefield": manifest.get("standard_residue_forcefield"),
        "charge_derivation": charge, "bonded_parameter_derivation": bonded,
        "manifest": str(manifest_path), "manifest_sha256": _sha256(manifest_path),
        "formal_charge": formal_charge, "crosslink_count": crosslinks,
        "required_residue_templates": sorted(_required_template_names(cfg, metadata)),
        "available_residue_templates": sorted(available_names),
        "file_sha256": {"residue_templates": {item["residue_name"]: item["sha256"] for item in templates},
                        "frcmod": {str(path): digest for path, digest in frcmods_with_hash}},
        "frcmod_problems": problems, "failures": failures,
        "templates": templates, "frcmods": [str(path) for path, _ in frcmods_with_hash],
        "scaffold": scaffold, "audit": str(audit_path),
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    if failures:
        raise ToolError(f"Reusable TBMB-Met AMBER bundle failed validation; see {audit_path}: {'; '.join(failures)}",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    return audit


def _pdb_element(line: str) -> str:
    value = line[76:78].strip().title() if len(line) >= 78 else ""
    if value:
        return value
    name = re.sub(r"[^A-Za-z]", "", line[12:16]).strip()
    return (name[:2].title() if name[:2].upper() in {"CL", "BR"} else name[:1].upper())


def _pdb_heavy_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        element = _pdb_element(line)
        if element.upper() == "H":
            continue
        records.append({
            "record": line[:6].strip(), "atom_name": line[12:16].strip(),
            "residue_name": line[17:20].strip(), "chain": line[21:22].strip(),
            "residue_number": int(line[22:26]), "element": element,
            "xyz": (float(line[30:38]), float(line[38:46]), float(line[46:54])),
        })
    if not records:
        raise ValueError(f"No heavy atoms found in ligand reference PDB: {path}")
    return records


def _reference_atom_map(reference_pdb: Path, reference) -> dict[int, dict[str, Any]]:
    """Map authoritative SDF atom indices to named PDB records by element/coordinates."""
    records = _pdb_heavy_records(reference_pdb)
    atoms = [atom for atom in reference.GetAtoms() if atom.GetAtomicNum() > 1]
    if len(records) != len(atoms):
        raise ValueError(f"Reference PDB has {len(records)} heavy atoms but SDF has {len(atoms)}")
    conformer = reference.GetConformer()
    unused = set(range(len(records)))
    mapping: dict[int, dict[str, Any]] = {}
    for atom in atoms:
        point = conformer.GetAtomPosition(atom.GetIdx())
        candidates = []
        for index in unused:
            record = records[index]
            if record["element"].upper() != atom.GetSymbol().upper():
                continue
            x, y, z = record["xyz"]
            distance = math.sqrt((point.x - x) ** 2 + (point.y - y) ** 2 + (point.z - z) ** 2)
            candidates.append((distance, index))
        if not candidates:
            raise ValueError(f"Cannot map SDF atom {atom.GetIdx()} ({atom.GetSymbol()}) to reference PDB")
        distance, selected = min(candidates)
        if distance > 0.25:
            raise ValueError(f"Reference PDB/SDF coordinate mapping exceeds 0.25 A at atom {atom.GetIdx()}: {distance:.3f}")
        unused.remove(selected); mapping[atom.GetIdx()] = records[selected]
    return mapping


def _write_modular_ligand_pdb(cfg: dict[str, Any], base: Path, reference, molecule,
                              parameterization: dict[str, Any], output: Path) -> dict[str, Any]:
    """Write candidate coordinates with ff14SB/custom residue names for TLEaP."""
    metadata = _load_mapping(_p(cfg["ligand_metadata"], base))
    chemical = metadata.get("chemical_definition") or metadata
    sequence = str(chemical.get("sequence") or metadata.get("sequence") or "").strip().upper()
    amber = cfg.get("amber") or {}
    positions = [int(value) for value in (amber.get("crosslink_positions") or chemical.get("met_positions") or [])]
    if len(positions) != int(amber.get("expected_crosslinks", 3)):
        raise ValueError("Crosslink positions are missing or inconsistent with expected_crosslinks")
    variants = amber.get("residue_templates") or {}
    internal = _amber_residue_identifier(variants.get("internal", "MSU"), "amber.residue_templates.internal")
    n_terminal = _amber_residue_identifier(variants.get("n_terminal", "NMU"), "amber.residue_templates.n_terminal")
    c_terminal = _amber_residue_identifier(variants.get("c_terminal", "CMU"), "amber.residue_templates.c_terminal")
    linker = _amber_residue_identifier(variants.get("linker", "TMB"), "amber.residue_templates.linker")
    scaffold = parameterization.get("scaffold") or {}
    merge_c_cap = bool(scaffold.get("c_terminal_template_includes_cap", True)) and len(sequence) in positions
    reference_map = _reference_atom_map(_p(cfg["ligand_reference_pdb"], base), reference)
    coordinates = molecule.GetConformer()
    entries: list[dict[str, Any]] = []
    heavy_order = 0
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        record = dict(reference_map[atom.GetIdx()])
        record["rdkit_atom_index"] = atom.GetIdx()
        record["reference_heavy_index"] = heavy_order
        heavy_order += 1
        point = coordinates.GetAtomPosition(atom.GetIdx())
        record["xyz"] = (float(point.x), float(point.y), float(point.z))
        residue_number = int(record["residue_number"])
        residue_name = str(record["residue_name"]).upper()
        if record["chain"] == "A" and residue_name == "MET" and residue_number in positions:
            record["residue_name"] = n_terminal if residue_number == 1 else c_terminal if residue_number == len(sequence) else internal
        elif residue_name in {"TBM", "TMB"}:
            record["residue_name"] = linker
            record["kind"] = "linker"
        elif residue_name in {"AMD", "NHE"}:
            record["residue_name"] = c_terminal if merge_c_cap else "NHE"
            record["atom_name"] = str(scaffold.get("c_terminal_cap_atom_name", "NT" if merge_c_cap else "N"))
            record["kind"] = "merged_c_cap" if merge_c_cap else "c_cap"
        elif residue_name == "ACE":
            record["kind"] = "n_cap"
        entries.append(record)

    peptide = sorted((item for item in entries if item.get("chain") == "A"),
                     key=lambda item: (item["residue_number"], item["atom_name"]))
    n_cap = [item for item in entries if item.get("kind") == "n_cap"]
    c_cap = [item for item in entries if item.get("kind") == "c_cap"]
    merged_c_cap = [item for item in entries if item.get("kind") == "merged_c_cap"]
    linker_entries = [item for item in entries if item.get("kind") == "linker"]
    expected_count = len(peptide) + len(n_cap) + len(c_cap) + len(merged_c_cap) + len(linker_entries)
    if expected_count != len(entries) or not linker_entries:
        raise ValueError("Reference PDB contains unclassified or missing ligand components")

    groups: list[tuple[str, int | str, list[dict[str, Any]]]] = []
    if n_cap:
        groups.append(("ACE", "n_cap", n_cap))
    for position in range(1, len(sequence) + 1):
        group = [item for item in peptide if item["residue_number"] == position]
        if position == len(sequence) and merge_c_cap:
            group += merged_c_cap
        if not group:
            raise ValueError(f"Reference PDB has no atoms for peptide residue {position}")
        groups.append((str(group[0]["residue_name"]), position, group))
    if c_cap:
        groups.append(("NHE", "c_cap", c_cap))
    groups.append((linker, "linker", linker_entries))

    output.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    residue_indices: dict[str, int] = {}
    heavy_atom_map: list[dict[str, Any]] = []
    serial = 1
    for leap_index, (residue_name, key, group) in enumerate(groups, 1):
        if key == "linker":
            lines.append("TER")
            residue_indices["linker"] = leap_index
        elif isinstance(key, int):
            residue_indices[str(key)] = leap_index
        for record in group:
            x, y, z = record["xyz"]
            tag = "HETATM" if key == "linker" else "ATOM  "
            chain = "B" if key == "linker" else "A"
            atom_name = str(record["atom_name"])[:4]
            element = str(record["element"])[:2]
            if len(residue_name) > 3:
                raise ValueError(f"PDB/TLEaP residue template name exceeds three characters: {residue_name}")
            # Column 17 is the blank altLoc field; residue names occupy 18-20.
            lines.append(f"{tag}{serial:5d} {atom_name:>4s}{residue_name:>4s} {chain}{leap_index:4d}    "
                         f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}")
            heavy_atom_map.append({
                "rdkit_atom_index": int(record["rdkit_atom_index"]),
                "reference_heavy_index": int(record["reference_heavy_index"]),
                "leap_residue_index": leap_index,
                "atom_name": atom_name,
                "element": element,
            })
            serial += 1
    lines.extend(["TER", "END"])
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    crosslinks = [{"peptide_position": position, "peptide_residue_index": residue_indices[str(position)],
                   "methionine_atom": _amber_identifier(scaffold.get("methionine_atom", "SD"), "scaffold.methionine_atom"),
                   "linker_residue_index": residue_indices["linker"],
                   "linker_atom": _amber_identifier(linker_atom, "scaffold.linker_atoms")}
                  for position, linker_atom in zip(positions, scaffold.get("linker_atoms", []))]
    return {"pdb": str(output), "residue_indices": residue_indices, "crosslinks": crosslinks,
            "heavy_atom_count": len(entries), "heavy_atom_map": heavy_atom_map}


def _template_load_lines(templates: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for record in templates:
        command = "loadAmberPrep" if record["format"] in {"prepi", "prep"} else "loadOff"
        lines.append(f'{command} "{record["path"]}"')
    return lines


def _tleap_text(cfg: dict[str, Any], paths: dict[str, Path], ligand_pdb: Path,
                parameterization: dict[str, Any], ligand_map: dict[str, Any],
                prmtop: Path, inpcrd: Path, initial_pdb: Path,
                ligand_prmtop: Path, ligand_inpcrd: Path) -> str:
    audit = json.loads(paths["audit"].read_text(encoding="utf-8"))
    amber = cfg.get("amber") or {}
    protein_forcefield = str(amber.get("protein_forcefield", "ff14SB"))
    lines = [f"source leaprc.protein.{protein_forcefield}", "source leaprc.gaff2", "set default PBRadii mbondi3"]
    lines.extend(_template_load_lines(parameterization["templates"]))
    lines.extend(f'loadAmberParams "{frcmod}"' for frcmod in parameterization["frcmods"])
    lines.append(f'receptor = loadPDB "{paths["amber_receptor"]}"')
    for pair in audit.get("disulfide_bonds", []):
        first = pair["first"]["amber_residue_index"]
        second = pair["second"]["amber_residue_index"]
        lines.append(f"bond receptor.{first}.SG receptor.{second}.SG")
    lines.append(f'ligand = loadPDB "{ligand_pdb}"')
    for item in ligand_map["crosslinks"]:
        lines.append(f'bond ligand.{item["peptide_residue_index"]}.{item["methionine_atom"]} '
                     f'ligand.{item["linker_residue_index"]}.{item["linker_atom"]}')
    lines.extend(["check ligand", f'saveAmberParm ligand "{ligand_prmtop}" "{ligand_inpcrd}"',
                  "complex = combine { receptor ligand }", "check complex",
                  f'saveAmberParm complex "{prmtop}" "{inpcrd}"', f'savePdb complex "{initial_pdb}"', "quit"])
    return "\n".join(lines) + "\n"


def _prmtop_charge(path: Path) -> float:
    lines = path.read_text(errors="replace").splitlines()
    values: list[float] = []
    active = False
    for line in lines:
        if line.startswith("%FLAG"):
            active = line.strip() == "%FLAG CHARGE"
            continue
        if not active or line.startswith("%FORMAT"):
            continue
        values.extend(float(value) for value in line.split())
    if not values:
        raise ValueError(f"No CHARGE section found in {path}")
    return sum(values) / 18.2223


def _amber_ligand_layout(prmtop_path: Path, ligand_map: dict[str, Any]) -> tuple[int, dict[int, int]]:
    """Return ligand atom count and heavy-atom mapping in the standalone topology.

    TLEaP orders atoms according to residue templates, so SDF/PDB atom order must not
    be assumed to survive assembly.  Residue position plus atom name is the stable
    bridge between the named modular PDB and the generated AMBER topology.
    """
    try:
        from openmm.app import AmberPrmtopFile
    except ImportError as exc:
        raise ToolError("OpenMM AMBER topology support is required to audit TLEaP atom mapping.",
                        error_type="unavailable", retriable=False, backend="OpenMM") from exc
    topology = AmberPrmtopFile(str(prmtop_path)).topology
    atoms = list(topology.atoms())
    lookup: dict[tuple[int, str], int] = {}
    duplicates: set[tuple[int, str]] = set()
    for residue_index, residue in enumerate(topology.residues(), 1):
        for atom in residue.atoms():
            key = (residue_index, atom.name.strip())
            if key in lookup:
                duplicates.add(key)
            lookup[key] = atom.index
    if duplicates:
        raise ValueError(f"AMBER ligand topology contains duplicate residue/atom keys: {sorted(duplicates)[:5]}")
    heavy_mapping: dict[int, int] = {}
    for record in ligand_map.get("heavy_atom_map", []):
        key = (int(record["leap_residue_index"]), str(record["atom_name"]).strip())
        if key not in lookup:
            raise ValueError(f"AMBER ligand topology is missing mapped heavy atom {key}")
        topology_index = lookup[key]
        topology_atom = atoms[topology_index]
        observed_element = topology_atom.element.symbol if topology_atom.element else ""
        if observed_element.upper() != str(record["element"]).upper():
            raise ValueError(
                f"Element mismatch for mapped heavy atom {key}: topology={observed_element}, "
                f"reference={record['element']}"
            )
        heavy_index = int(record["reference_heavy_index"])
        if heavy_index in heavy_mapping:
            raise ValueError(f"Duplicate reference heavy-atom index in modular mapping: {heavy_index}")
        heavy_mapping[heavy_index] = topology_index
    expected = int(ligand_map.get("heavy_atom_count", -1))
    if expected < 1 or len(heavy_mapping) != expected or set(heavy_mapping) != set(range(expected)):
        raise ValueError(
            f"Incomplete ligand heavy-atom mapping: mapped {len(heavy_mapping)} atoms, expected {expected}"
        )
    topology_heavy_count = sum(
        atom.element is not None and atom.element.symbol.upper() != "H" for atom in atoms
    )
    if topology_heavy_count != expected:
        raise ValueError(
            f"TLEaP ligand topology has {topology_heavy_count} heavy atoms but the authoritative "
            f"graph has {expected}; a residue template added or omitted heavy atoms"
        )
    return len(atoms), heavy_mapping


def _source_file(record: Any, source_dir: Path, label: str) -> Path:
    if not isinstance(record, dict) or not record.get("path") or not record.get("sha256"):
        raise ValueError(f"parameter source files.{label} requires path and sha256")
    path = _p(str(record["path"]), source_dir)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"parameter source file is missing or empty: {path}")
    expected = str(record["sha256"]).strip().lower()
    if _sha256(path) != expected:
        raise ValueError(f"parameter source SHA-256 mismatch for {label}")
    return path


def _tleap_ligand_test_text(cfg: dict[str, Any], ligand_pdb: Path,
                            parameterization: dict[str, Any], ligand_map: dict[str, Any],
                            prmtop: Path, inpcrd: Path, output_pdb: Path) -> str:
    amber = cfg.get("amber") or {}
    forcefield = str(amber.get("protein_forcefield", "ff14SB"))
    lines = [f"source leaprc.protein.{forcefield}", "source leaprc.gaff2", "set default PBRadii mbondi3"]
    lines.extend(_template_load_lines(parameterization["templates"]))
    lines.extend(f'loadAmberParams "{path}"' for path in parameterization["frcmods"])
    lines.append(f'ligand = loadPDB "{ligand_pdb}"')
    for item in ligand_map["crosslinks"]:
        lines.append(f'bond ligand.{item["peptide_residue_index"]}.{item["methionine_atom"]} '
                     f'ligand.{item["linker_residue_index"]}.{item["linker_atom"]}')
    lines.extend(["check ligand", f'saveAmberParm ligand "{prmtop}" "{inpcrd}"',
                  f'savePdb ligand "{output_pdb}"', "quit"])
    return "\n".join(lines) + "\n"


def _seal_modular_parameter_bundle(cfg: dict[str, Any], base: Path, reference) -> dict[str, Any]:
    """Seal reviewed RESP/template sources and prove they assemble the reference ligand.

    This tool does not invent QM results.  It requires converged multi-conformer RESP
    outputs and reviewed crosslink terms, copies them into a content-addressed bundle,
    and runs a ligand-only TLEaP assembly test before marking the bundle usable.
    """
    amber = cfg.get("amber") or {}
    source_value = amber.get("parameter_source_manifest")
    target_value = amber.get("parameter_bundle_manifest")
    if not source_value or not target_value:
        raise ToolError("amber.parameter_source_manifest and amber.parameter_bundle_manifest are required.",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    source_path = _p(str(source_value), base)
    if not source_path.is_file():
        raise ToolError(f"Reviewed parameter source manifest not found: {source_path}",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    source = _load_mapping(source_path)
    failures: list[str] = []
    from rdkit import Chem
    expected_charge = int(amber.get("ligand_net_charge", 5))
    expected_crosslinks = int(amber.get("expected_crosslinks", 3))
    if Chem.GetFormalCharge(reference) != expected_charge:
        failures.append(
            f"authoritative SDF formal charge is {Chem.GetFormalCharge(reference)}, expected {expected_charge}"
        )
    if _sulfonium_crosslink_count(reference) != expected_crosslinks:
        failures.append(
            f"authoritative SDF has {_sulfonium_crosslink_count(reference)} three-coordinate +1 "
            f"sulfonium centers, expected {expected_crosslinks}"
        )
    if source.get("schema_version") != 1:
        failures.append("source schema_version must be 1")
    if source.get("approved") is not True:
        failures.append("source approved must be true after qualified review")
    if str(source.get("standard_residue_forcefield", "")).lower() != "ff14sb":
        failures.append("source standard_residue_forcefield must be ff14SB")
    charge = source.get("charge_derivation") or {}
    if "resp" not in str(charge.get("method", "")).lower() or int(charge.get("conformer_count", 0)) < 2:
        failures.append("source must document multi-conformer RESP")
    for flag in ("electronic_convergence_verified", "charge_constraints_verified"):
        if charge.get(flag) is not True:
            failures.append(f"source charge_derivation.{flag} must be true")
    if (charge.get("boundary_reconciliation") or {}).get("reviewed") is not True:
        failures.append("source RESP-to-template boundary charge reconciliation must be reviewed")
    if not str(charge.get("qm_level", "")).strip():
        failures.append("source charge_derivation.qm_level is required")
    bonded = source.get("bonded_parameter_derivation") or {}
    if bonded.get("reviewed") is not True or not str(bonded.get("method", "")).strip():
        failures.append("source bonded_parameter_derivation must name a reviewed method")
    if bonded.get("peptide_boundary_terms_reviewed") is not True:
        failures.append("source ff14SB/non-standard peptide-boundary terms must be reviewed")
    if not str(source.get("reviewed_by", "")).strip():
        failures.append("source reviewed_by is required")
    scaffold = source.get("scaffold") or {}
    if str(scaffold.get("linker", "")).upper() != "TBMB":
        failures.append("source scaffold.linker must be TBMB")
    if int(scaffold.get("crosslink_count", -1)) != expected_crosslinks:
        failures.append(f"source scaffold.crosslink_count must be {expected_crosslinks}")
    if str(scaffold.get("methionine_atom", "")).upper() != "SD":
        failures.append("source scaffold.methionine_atom must be SD")
    linker_atoms = [str(value).upper() for value in scaffold.get("linker_atoms", [])]
    if len(linker_atoms) != expected_crosslinks or len(set(linker_atoms)) != expected_crosslinks:
        failures.append("source scaffold.linker_atoms must contain three unique atom names")
    if any(not re.fullmatch(r"[A-Za-z0-9]{1,4}", value) for value in linker_atoms):
        failures.append("source scaffold.linker_atoms contain an invalid AMBER atom identifier")
    if int(scaffold.get("sulfur_formal_charge_each", -999)) != 1:
        failures.append("source scaffold.sulfur_formal_charge_each must be 1")
    if failures:
        raise ToolError("Parameter source manifest is not review-ready: " + "; ".join(failures),
                        error_type="invalid_input", retriable=False, backend="AmberTools")

    target_manifest = _p(str(target_value), base)
    bundle_dir = _mkdir(target_manifest.parent)
    template_dir = _mkdir(bundle_dir / "templates")
    frcmod_dir = _mkdir(bundle_dir / "frcmod")
    files = source.get("files") or {}
    source_templates = files.get("residue_templates")
    source_frcmods = files.get("frcmod")
    if not isinstance(source_templates, list) or not source_templates:
        raise ToolError("parameter source files.residue_templates must be a nonempty list.", error_type="invalid_input", retriable=False, backend="AmberTools")
    if not isinstance(source_frcmods, list) or not source_frcmods:
        raise ToolError("parameter source files.frcmod must be a nonempty list.", error_type="invalid_input", retriable=False, backend="AmberTools")
    sealed_templates: list[dict[str, Any]] = []
    runtime_templates: list[dict[str, Any]] = []
    for index, record in enumerate(source_templates):
        path = _source_file(record, source_path.parent, f"residue_templates[{index}]")
        residue_name = str(record.get("residue_name") or "").strip()
        format_name = str(record.get("format") or "").strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9]{1,3}", residue_name) or format_name not in {"off", "lib", "prepi", "prep"}:
            raise ToolError(f"Invalid residue template record {index}.", error_type="invalid_input", retriable=False, backend="AmberTools")
        destination = template_dir / f"{residue_name}_{path.name}"
        if path.resolve() != destination.resolve():
            shutil.copy2(path, destination)
        sealed_templates.append({"residue_name": residue_name, "format": format_name,
                                 "path": str(destination.relative_to(bundle_dir)), "sha256": _sha256(destination)})
        runtime_templates.append({"residue_name": residue_name, "format": format_name,
                                  "path": str(destination), "sha256": _sha256(destination)})
    source_names = [item["residue_name"] for item in runtime_templates]
    if len(source_names) != len(set(source_names)):
        raise ToolError("Parameter source contains duplicate residue template names.",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    sealed_frcmods: list[dict[str, Any]] = []
    runtime_frcmods: list[str] = []
    for index, record in enumerate(source_frcmods):
        path = _source_file(record, source_path.parent, f"frcmod[{index}]")
        destination = frcmod_dir / path.name
        if path.resolve() != destination.resolve():
            shutil.copy2(path, destination)
        problems = _frcmod_problems(destination)
        if problems:
            raise ToolError(f"Reviewed FRCMOD still contains invalid parameters: {problems[:5]}", error_type="invalid_input", retriable=False, backend="AmberTools")
        sealed_frcmods.append({"path": str(destination.relative_to(bundle_dir)), "sha256": _sha256(destination)})
        runtime_frcmods.append(str(destination))

    metadata = _load_mapping(_p(cfg["ligand_metadata"], base))
    available = {item["residue_name"] for item in runtime_templates}
    missing = sorted(_required_template_names(cfg, metadata) - available)
    if missing:
        raise ToolError("Parameter source lacks templates required by the reference sequence: " + ", ".join(missing), error_type="invalid_input", retriable=False, backend="AmberTools")
    runtime = {"templates": runtime_templates, "frcmods": runtime_frcmods, "scaffold": scaffold}
    test_dir = _mkdir(bundle_dir / "assembly_test")
    ligand_pdb = test_dir / "reference_ligand_amber.pdb"
    ligand_map = _write_modular_ligand_pdb(cfg, base, reference, reference, runtime, ligand_pdb)
    prmtop, inpcrd, output_pdb = test_dir / "ligand.prmtop", test_dir / "ligand.inpcrd", test_dir / "ligand_assembled.pdb"
    tleap_input = test_dir / "tleap.in"
    tleap_input.write_text(_tleap_ligand_test_text(cfg, ligand_pdb, runtime, ligand_map, prmtop, inpcrd, output_pdb), encoding="utf-8")
    log = test_dir / "tleap.log"
    _run(_render(_required_command(cfg, "tleap", "AmberTools"), input=tleap_input), log, cwd=test_dir)
    log_text = log.read_text(errors="replace")
    leap_errors = _tleap_errors(log_text)
    if leap_errors or not prmtop.is_file() or not inpcrd.is_file():
        raise ToolError(f"Modular parameter bundle failed TLEaP assembly; see {log}: " + "; ".join(leap_errors[:5]), error_type="invalid_input", retriable=False, backend="AmberTools")
    ligand_atom_count, heavy_mapping = _amber_ligand_layout(prmtop, ligand_map)
    observed_charge = _prmtop_charge(prmtop)
    tolerance = float(amber.get("partial_charge_tolerance", 0.05))
    if abs(observed_charge - expected_charge) > tolerance:
        raise ToolError(f"TLEaP test ligand charge is {observed_charge:.6f}, expected {expected_charge} +/- {tolerance}; see {log}", error_type="invalid_input", retriable=False, backend="AmberTools")

    manifest = {
        "schema_version": 2, "validated": True,
        "parameterization_scope": "reusable_tbmb_met_scaffold",
        "parameterization_method": "ff14SB standard residues plus reusable RESP-derived TBMB-Met residue templates",
        "standard_residue_forcefield": "ff14SB", "allowed_uses": ["restrained_minimization"],
        "scaffold": scaffold, "charge_derivation": charge,
        "bonded_parameter_derivation": bonded, "reviewed_by": source.get("reviewed_by"),
        "source_manifest": str(source_path), "source_manifest_sha256": _sha256(source_path),
        "files": {"residue_templates": sealed_templates, "frcmod": sealed_frcmods},
        "assembly_test": {"passed": True, "reference_sequence": (metadata.get("chemical_definition") or metadata).get("sequence"),
                          "formal_charge": observed_charge, "crosslinks": ligand_map["crosslinks"],
                          "ligand_atom_count": ligand_atom_count,
                          "mapped_heavy_atom_count": len(heavy_mapping),
                          "tleap_log": str(log), "prmtop_sha256": _sha256(prmtop), "inpcrd_sha256": _sha256(inpcrd)},
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    target_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"parameter_bundle_manifest": str(target_manifest), "parameter_bundle_sha256": _sha256(target_manifest),
            "assembly_test": manifest["assembly_test"], "reusable_templates": sorted(available),
            "notice": "QM/RESP and bonded-term scientific approval is inherited from the reviewed source manifest; TLEaP assembly does not replace that review."}


def _parameterize(cfg: dict[str, Any], base: Path) -> dict[str, Any]:
    try:
        from rdkit import Chem
    except ImportError as exc:
        raise ToolError("RDKit is required to bind the modular bundle to the authoritative TBMB graph.", error_type="unavailable", retriable=False, backend="RDKit") from exc
    supplier = Chem.SDMolSupplier(str(_p(cfg["ligand_ensemble_sdf"], base)), removeHs=False)
    reference = next((item for item in supplier if item is not None), None)
    if reference is None:
        raise ToolError("Authoritative ligand ensemble contains no readable SDF molecule.", error_type="invalid_input", retriable=False, backend="RDKit")
    return _seal_modular_parameter_bundle(cfg, base, reference)


def _tleap_errors(log: str) -> list[str]:
    patterns = (r"FATAL", r"Could not find", r"does not have a type", r"No torsion terms", r"Errors\s*=\s*[1-9]")
    return [line.strip() for line in log.splitlines() if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns)]


def _rms_force(forces, unit_module) -> tuple[float, float]:
    values = forces.value_in_unit(unit_module.kilojoules_per_mole / unit_module.nanometer)
    magnitudes = [math.sqrt(vector.x * vector.x + vector.y * vector.y + vector.z * vector.z) for vector in values]
    return math.sqrt(sum(value * value for value in magnitudes) / max(1, len(magnitudes))), max(magnitudes, default=0.0)


def _aligned_rmsds(initial_nm, final_nm, topology, ligand_start: int) -> tuple[float, float]:
    import numpy as np
    initial = np.asarray([[point.x, point.y, point.z] for point in initial_nm], dtype=float) * 10.0
    final = np.asarray([[point.x, point.y, point.z] for point in final_nm], dtype=float) * 10.0
    atoms = list(topology.atoms())
    backbone = [atom.index for atom in atoms[:ligand_start] if atom.name.upper() in {"N", "CA", "C", "O", "OXT"} and atom.element and atom.element.symbol != "H"]
    if len(backbone) < 3:
        raise ValueError("Fewer than three receptor backbone atoms are available for RMSD alignment")
    mobile = final[backbone]; target = initial[backbone]
    mobile_center, target_center = mobile.mean(axis=0), target.mean(axis=0)
    u, _, vt = np.linalg.svd((mobile - mobile_center).T @ (target - target_center))
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1; rotation = u @ vt
    aligned = (final - mobile_center) @ rotation + target_center
    receptor_rmsd = float(np.sqrt(np.mean(np.sum((aligned[backbone] - initial[backbone]) ** 2, axis=1))))
    ligand_heavy = [atom.index for atom in atoms[ligand_start:] if atom.element and atom.element.symbol != "H"]
    ligand_rmsd = float(np.sqrt(np.mean(np.sum((aligned[ligand_heavy] - initial[ligand_heavy]) ** 2, axis=1))))
    return receptor_rmsd, ligand_rmsd


def _openmm_relax_candidate(cfg: dict[str, Any], prmtop_path: Path, inpcrd_path: Path,
                            ligand_atom_count: int, ligand_heavy_mapping: dict[int, int],
                            reference_heavy, job: Path) -> dict[str, Any]:
    try:
        import openmm
        from openmm import unit
        from openmm.app import AmberInpcrdFile, AmberPrmtopFile, GBn2, Modeller, NoCutoff, PDBFile, Simulation
    except ImportError as exc:
        raise ToolError("OpenMM with AMBER file support is required for restrained complex relaxation.", error_type="unavailable", retriable=False, backend="OpenMM") from exc
    prmtop = AmberPrmtopFile(str(prmtop_path)); inpcrd = AmberInpcrdFile(str(inpcrd_path))
    atoms = list(prmtop.topology.atoms()); ligand_start = len(atoms) - ligand_atom_count
    if ligand_start <= 0:
        raise ValueError("AMBER topology does not contain a receptor followed by the expected ligand atoms")
    relaxation = cfg.get("relaxation") or {}
    salt = float(relaxation.get("salt_concentration_molar", 0.15))
    temperature = float(relaxation.get("temperature_kelvin", 298.15))
    system = prmtop.createSystem(nonbondedMethod=NoCutoff, constraints=None, implicitSolvent=GBn2,
                                 implicitSolventSaltConc=salt * unit.molar, temperature=temperature * unit.kelvin)
    initial_positions = inpcrd.positions
    initial_nm = initial_positions.value_in_unit(unit.nanometer)
    ligand_heavy = [atom.index for atom in atoms[ligand_start:] if atom.element and atom.element.symbol != "H"]
    receptor_heavy = [atom.index for atom in atoms[:ligand_start] if atom.element and atom.element.symbol != "H"]
    interface_cutoff_nm = float(relaxation.get("interface_cutoff_angstrom", 6.0)) / 10.0
    interface = {
        index for index in receptor_heavy
        if any(math.dist((initial_nm[index].x, initial_nm[index].y, initial_nm[index].z),
                         (initial_nm[other].x, initial_nm[other].y, initial_nm[other].z)) <= interface_cutoff_nm
               for other in ligand_heavy)
    }
    backbone_names = {"N", "CA", "C", "O", "OXT"}
    restraint = openmm.CustomExternalForce("0.5*k*((x-x0)^2+(y-y0)^2+(z-z0)^2)")
    for parameter in ("k", "x0", "y0", "z0"):
        restraint.addPerParticleParameter(parameter)
    restrained: list[tuple[int, int]] = []
    for atom in atoms:
        if not atom.element or atom.element.symbol == "H":
            continue
        point = initial_nm[atom.index]
        force_index = restraint.addParticle(atom.index, [0.0, point.x, point.y, point.z])
        restrained.append((force_index, atom.index))
    system.addForce(restraint)
    integrator = openmm.VerletIntegrator(1.0 * unit.femtoseconds)
    platform_name = str(relaxation.get("platform", "")).strip()
    if platform_name and platform_name.lower() != "auto":
        platform = openmm.Platform.getPlatformByName(platform_name)
        properties = {str(key): str(value) for key, value in (relaxation.get("platform_properties") or {}).items()}
        simulation = Simulation(prmtop.topology, system, integrator, platform, properties)
    else:
        simulation = Simulation(prmtop.topology, system, integrator)
    simulation.context.setPositions(initial_positions)

    def update_restraints(stage: dict[str, Any] | None) -> None:
        conversion = 418.4  # kcal/mol/A^2 -> kJ/mol/nm^2
        for force_index, particle in restrained:
            atom = atoms[particle]
            if stage is None:
                value = 0.0
            elif particle >= ligand_start:
                value = float(stage.get("ligand_heavy_restraint_kcal_mol_a2", 0.0))
            elif stage.get("receptor_heavy_restraint_kcal_mol_a2") is not None:
                value = float(stage["receptor_heavy_restraint_kcal_mol_a2"])
            elif particle in interface and atom.name.upper() not in backbone_names:
                value = float(stage.get("receptor_interface_sidechain_restraint_kcal_mol_a2", 0.0))
            elif atom.name.upper() in backbone_names:
                value = float(stage.get("receptor_backbone_restraint_kcal_mol_a2", 0.0))
            else:
                value = float(stage.get("receptor_noninterface_restraint_kcal_mol_a2", 0.0))
            point = initial_nm[particle]
            restraint.setParticleParameters(force_index, particle, [value * conversion, point.x, point.y, point.z])
        restraint.updateParametersInContext(simulation.context)

    update_restraints(None)
    initial_state = simulation.context.getState(getEnergy=True, getForces=True)
    initial_energy = initial_state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)
    default_stages = [
        {"name": "hydrogen_and_local_cleanup", "receptor_heavy_restraint_kcal_mol_a2": 50.0,
         "ligand_heavy_restraint_kcal_mol_a2": 25.0, "max_iterations": 500},
        {"name": "interface_relaxation", "receptor_backbone_restraint_kcal_mol_a2": 10.0,
         "receptor_noninterface_restraint_kcal_mol_a2": 5.0, "receptor_interface_sidechain_restraint_kcal_mol_a2": 0.0,
         "ligand_heavy_restraint_kcal_mol_a2": 2.0, "max_iterations": 1500},
        {"name": "joint_refinement", "receptor_backbone_restraint_kcal_mol_a2": 2.0,
         "receptor_noninterface_restraint_kcal_mol_a2": 1.0, "receptor_interface_sidechain_restraint_kcal_mol_a2": 0.0,
         "ligand_heavy_restraint_kcal_mol_a2": 0.5, "max_iterations": 2000},
    ]
    stages = relaxation.get("stages") or default_stages
    tolerance = float(relaxation.get("force_tolerance_kj_mol_nm", 10.0))
    stage_records = []
    for stage in stages:
        update_restraints(stage)
        before = simulation.context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)
        openmm.LocalEnergyMinimizer.minimize(simulation.context,
                                             tolerance * unit.kilojoules_per_mole / unit.nanometer,
                                             int(stage.get("max_iterations", 1000)))
        state = simulation.context.getState(getEnergy=True, getForces=True)
        rms_force, max_force = _rms_force(state.getForces(), unit)
        stage_records.append({
            "name": str(stage.get("name", f"stage_{len(stage_records) + 1}")),
            "energy_before_kj_mol": before,
            "energy_after_kj_mol": state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole),
            "rms_force_kj_mol_nm": rms_force, "maximum_force_kj_mol_nm": max_force,
            "max_iterations": int(stage.get("max_iterations", 1000)),
        })
    restrained_state = simulation.context.getState(getEnergy=True, getForces=True, getPositions=True)
    restrained_rms_force, restrained_max_force = _rms_force(restrained_state.getForces(), unit)
    update_restraints(None)
    final_state = simulation.context.getState(getEnergy=True, getForces=True, getPositions=True)
    final_energy = final_state.getPotentialEnergy().value_in_unit(unit.kilojoules_per_mole)
    physical_rms_force, physical_max_force = _rms_force(final_state.getForces(), unit)
    final_positions = final_state.getPositions(); final_nm = final_positions.value_in_unit(unit.nanometer)
    receptor_rmsd, ligand_rmsd = _aligned_rmsds(initial_nm, final_nm, prmtop.topology, ligand_start)
    finite = math.isfinite(initial_energy) and math.isfinite(final_energy) and all(
        math.isfinite(value) for point in final_nm for value in (point.x, point.y, point.z))

    relaxed_complex = job / "relaxed_complex.pdb"
    with relaxed_complex.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(prmtop.topology, final_positions, handle, keepIds=True)
    modeller = Modeller(prmtop.topology, final_positions)
    modeller_atoms = list(modeller.topology.atoms())
    modeller.delete(modeller_atoms[ligand_start:])
    relaxed_receptor = job / "relaxed_receptor.pdb"
    with relaxed_receptor.open("w", encoding="utf-8") as handle:
        PDBFile.writeFile(modeller.topology, modeller.positions, handle, keepIds=True)
    from rdkit import Chem
    from rdkit.Geometry import Point3D
    if reference_heavy.GetNumAtoms() != len(ligand_heavy_mapping):
        raise ValueError(
            f"Relaxed-coordinate mapping has {len(ligand_heavy_mapping)} atoms for a "
            f"{reference_heavy.GetNumAtoms()}-atom heavy reference"
        )
    relaxed_molecule = Chem.Mol(reference_heavy); relaxed_molecule.RemoveAllConformers()
    conformer = Chem.Conformer(reference_heavy.GetNumAtoms()); conformer.Set3D(True)
    for heavy_index in range(reference_heavy.GetNumAtoms()):
        local_topology_index = ligand_heavy_mapping[heavy_index]
        point = final_nm[ligand_start + local_topology_index]
        conformer.SetAtomPosition(heavy_index, Point3D(point.x * 10.0, point.y * 10.0, point.z * 10.0))
    relaxed_molecule.AddConformer(conformer, assignId=True)
    relaxed_ligand = job / "relaxed_ligand.sdf"
    writer = Chem.SDWriter(str(relaxed_ligand)); writer.write(relaxed_molecule); writer.close()
    return {
        "relaxed_complex": str(relaxed_complex), "relaxed_receptor": str(relaxed_receptor),
        "relaxed_ligand": str(relaxed_ligand), "implicit_solvent": "GBn2", "salt_concentration_molar": salt,
        "temperature_kelvin": temperature,
        "platform": simulation.context.getPlatform().getName(), "initial_physical_energy_kj_mol": initial_energy,
        "final_physical_energy_kj_mol": final_energy, "energy_decreased": final_energy < initial_energy,
        "final_restrained_rms_force_kj_mol_nm": restrained_rms_force,
        "final_restrained_maximum_force_kj_mol_nm": restrained_max_force,
        "final_unrestrained_rms_force_kj_mol_nm": physical_rms_force,
        "final_unrestrained_maximum_force_kj_mol_nm": physical_max_force,
        "configured_force_tolerance_kj_mol_nm": tolerance, "finite_coordinates_and_energy": finite,
        "receptor_backbone_rmsd_angstrom": receptor_rmsd, "ligand_heavy_atom_rmsd_angstrom": ligand_rmsd,
        "interface_receptor_heavy_atom_count": len(interface), "stages": stage_records,
    }


def _relax_status(path: Path, state: str, **details: Any) -> None:
    payload = {"updated_at_utc": datetime.now(timezone.utc).isoformat(), "state": state, **details}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _relax(cfg: dict[str, Any], base: Path) -> dict[str, Any]:
    """Assemble each sequence from reusable TBMB-Met templates and relax complexes."""
    try:
        from rdkit import Chem
    except ImportError as exc:
        raise ToolError("RDKit is required for restrained TBMB complex relaxation.", error_type="unavailable", retriable=False, backend="OpenMM") from exc
    try:
        import openmm  # noqa: F401
    except ImportError as exc:
        raise ToolError("OpenMM is required for restrained complex relaxation.", error_type="unavailable", retriable=False, backend="OpenMM") from exc
    amber = cfg.get("amber") or {}; relaxation_cfg = cfg.get("relaxation") or {}
    supported = {
        "amber.parameterization_mode": (
            str(amber.get("parameterization_mode", "")).lower(),
            "reusable_tbmb_met_templates",
        ),
        "amber.protein_forcefield": (str(amber.get("protein_forcefield", "ff14SB")).lower(), "ff14sb"),
        "amber.implicit_solvent": (str(amber.get("implicit_solvent", "GBn2")).lower(), "gbn2"),
        "relaxation.implicit_solvent": (str(relaxation_cfg.get("implicit_solvent", "GBn2")).lower(), "gbn2"),
    }
    unsupported = [f"{name}={actual}" for name, (actual, expected) in supported.items() if actual != expected]
    if unsupported:
        raise ToolError("Unsupported relaxation model for this validated implementation: " + ", ".join(unsupported), error_type="invalid_input", retriable=False, backend="OpenMM")
    paths = _paths(cfg, base)
    _mkdir(paths["relax"]); _mkdir(paths["postfilter"])
    status_path = paths["relax"] / "run_status.json"
    _relax_status(status_path, "preflight", message="Checking prerequisite artifacts and parameter bundle declaration")
    candidates_path = paths["prefilter"] / "relaxation_candidates.json"
    for needed in (candidates_path, paths["receptor"], paths["audit"], paths["pockets"], _p(cfg["ligand_ensemble_sdf"], base)):
        if not needed.is_file():
            _relax_status(status_path, "failed", phase="preflight", error=f"Required relaxation input missing: {needed}")
            raise ToolError(f"Required relaxation input missing: {needed}", error_type="invalid_input", retriable=False, backend="OpenMM")
    # Refresh only the LEaP-specific receptor copy.  This does not rerun receptor
    # repair or invalidate the existing P2Rank/DiffDock coordinates.
    expected_disulfides = json.loads(paths["audit"].read_text(encoding="utf-8")).get("disulfide_bonds", [])
    observed_disulfides, _ = _amber_receptor_input(paths["receptor"], paths["amber_receptor"], paths["receptor_atom_map"])
    if observed_disulfides != expected_disulfides:
        _relax_status(status_path, "failed", phase="preflight", error="Receptor disulfide audit no longer matches the prepared structure")
        raise ToolError("Receptor disulfide audit no longer matches the prepared structure; rerun prepare-receptor before relaxation.",
                        error_type="invalid_input", retriable=False, backend="AmberTools")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    if not candidates:
        _relax_status(status_path, "failed", phase="preflight", error="DiffDock pre-relaxation candidate list is empty")
        raise ToolError("DiffDock pre-relaxation candidate list is empty.", error_type="invalid_input", retriable=False, backend="OpenMM")
    reference = next((mol for mol in Chem.SDMolSupplier(str(_p(cfg["ligand_ensemble_sdf"], base)), removeHs=False) if mol), None)
    if reference is None:
        _relax_status(status_path, "failed", phase="preflight", error="Authoritative ligand ensemble contains no readable SDF molecule")
        raise ToolError("Authoritative ligand ensemble contains no readable SDF molecule.", error_type="invalid_input", retriable=False, backend="OpenMM")
    reference_heavy = Chem.RemoveHs(Chem.Mol(reference))
    print("[relax] validating pre-generated ff14SB/custom TBMB-Met parameter bundle", flush=True)
    _relax_status(status_path, "validating_parameter_bundle", candidate_count=len(candidates))
    try:
        parameterization = _parameterize_tbmb(cfg, base, reference)
    except Exception as exc:
        _relax_status(status_path, "failed", phase="parameter_bundle_validation", error=str(exc))
        raise
    print(f"[relax] parameter bundle validated: {parameterization['audit']}", flush=True)
    _relax_status(
        status_path, "parameter_bundle_validated", candidate_count=len(candidates),
        parameterization_audit=parameterization["audit"],
    )
    pockets = json.loads(paths["pockets"].read_text(encoding="utf-8"))["pockets"]
    pocket_by_id = {item["pocket_id"]: item for item in pockets}
    accepted, rejected = [], []
    for index, candidate in enumerate(candidates, 1):
        job = _mkdir(paths["relax"] / f"candidate_{index:02d}")
        print(f"[relax] candidate {index}/{len(candidates)}: LEaP assembly and OpenMM minimization", flush=True)
        _relax_status(
            status_path, "candidate_running", candidate=index,
            candidate_count=len(candidates), job=str(job), accepted=len(accepted), rejected=len(rejected),
        )
        try:
            pose = Chem.MolFromMolFile(str(candidate["pose"]), removeHs=False)
            if pose is None or _signature(pose) != _signature(reference):
                raise ValueError("candidate pose does not match the authoritative heavy-atom graph")
            ligand_pdb = job / "ligand_amber_input.pdb"
            ligand_map = _write_modular_ligand_pdb(
                cfg, base, reference, pose, parameterization, ligand_pdb,
            )
            ligand_prmtop = job / "ligand.prmtop"
            ligand_inpcrd = job / "ligand.inpcrd"
            prmtop, inpcrd, initial_pdb = job / "complex.prmtop", job / "complex.inpcrd", job / "initial_complex.pdb"
            tleap_input = job / "tleap.in"
            tleap_input.write_text(
                _tleap_text(
                    cfg, paths, ligand_pdb, parameterization, ligand_map,
                    prmtop, inpcrd, initial_pdb, ligand_prmtop, ligand_inpcrd,
                ),
                encoding="utf-8",
            )
            _run(_render(_required_command(cfg, "tleap", "AmberTools"), input=tleap_input), job / "tleap.log")
            tleap_log = (job / "tleap.log").read_text(encoding="utf-8", errors="replace")
            leap_errors = _tleap_errors(tleap_log)
            required_outputs = (ligand_prmtop, ligand_inpcrd, prmtop, inpcrd, initial_pdb)
            if leap_errors or any(not path.is_file() for path in required_outputs):
                raise ValueError("LEaP topology validation failed: " + ("; ".join(leap_errors[:5]) or "prmtop/inpcrd missing"))
            observed_charge = _prmtop_charge(ligand_prmtop)
            expected_charge = int(amber.get("ligand_net_charge", 5))
            charge_tolerance = float(amber.get("partial_charge_tolerance", 0.05))
            if abs(observed_charge - expected_charge) > charge_tolerance:
                raise ValueError(
                    f"Sequence-specific ligand topology charge is {observed_charge:.6f}, "
                    f"expected {expected_charge} +/- {charge_tolerance}"
                )
            ligand_atom_count, ligand_heavy_mapping = _amber_ligand_layout(ligand_prmtop, ligand_map)
            relaxation = _openmm_relax_candidate(
                cfg, prmtop, inpcrd, ligand_atom_count, ligand_heavy_mapping, reference_heavy, job,
            )
            receptor_xyz = _read_xyz_pdb(Path(relaxation["relaxed_receptor"]))
            _, _, metrics = _regraph_and_metrics(
                Path(relaxation["relaxed_ligand"]), reference_heavy, receptor_xyz, job / "validated",
            )
            relaxed_graph = Chem.MolFromMolFile(relaxation["relaxed_ligand"], removeHs=False)
            metrics["tbmb_sulfonium_crosslink_count"] = _sulfonium_crosslink_count(relaxed_graph) if relaxed_graph else 0
            pocket = pocket_by_id.get(candidate["pocket_id"])
            if pocket is None:
                pocket = min(pockets, key=lambda item: math.dist(metrics["ligand_centroid"], item["center"]))
            metrics["pocket_centroid_distance"] = math.dist(metrics["ligand_centroid"], pocket["center"])
            failures = []
            max_force = float((cfg.get("relaxation") or {}).get("max_final_restrained_rms_force_kj_mol_nm",
                                                                  2.0 * float((cfg.get("relaxation") or {}).get("force_tolerance_kj_mol_nm", 10.0))))
            if not relaxation["finite_coordinates_and_energy"]: failures.append("non-finite OpenMM coordinates or energy")
            if bool((cfg.get("relaxation") or {}).get("require_energy_decrease", True)) and not relaxation["energy_decreased"]:
                failures.append("unrestrained physical energy did not decrease")
            if relaxation["final_restrained_rms_force_kj_mol_nm"] > max_force: failures.append("final restrained RMS force above threshold")
            if relaxation["receptor_backbone_rmsd_angstrom"] > float(cfg.get("post_relax_max_receptor_backbone_rmsd", 1.0)):
                failures.append("receptor backbone RMSD above threshold")
            if relaxation["ligand_heavy_atom_rmsd_angstrom"] > float(cfg.get("post_relax_max_ligand_rmsd", 3.0)):
                failures.append("ligand displacement above threshold")
            if not metrics["topology_matches_authoritative_sdf"]: failures.append("TBMB topology changed during relaxation")
            if not metrics["formal_charge_plus_5"]: failures.append("formal charge is not +5 after relaxation")
            if metrics["tbmb_sulfonium_crosslink_count"] != int((cfg.get("amber") or {}).get("expected_crosslinks", 3)):
                failures.append("three TBMB sulfonium crosslinks were not retained")
            if metrics["bad_heavy_atom_covalent_bond_count"]: failures.append("invalid covalent-bond geometry after relaxation")
            if metrics["minimum_receptor_distance"] < float(cfg.get("post_relax_min_heavy_atom_distance", 1.5)):
                failures.append("unresolved receptor-ligand clash after relaxation")
            if metrics["clash_pairs_under_1p5A"]: failures.append("one or more heavy-atom clashes remain after relaxation")
            if metrics["pocket_centroid_distance"] > float(cfg.get("post_relax_max_pocket_centroid_distance", 10.0)):
                failures.append("relaxed ligand left the assigned P2Rank pocket")
            record = {
                "candidate": index, "source_diffdock": candidate, "pocket_id": pocket["pocket_id"],
                "parameterization_audit": parameterization["audit"], "relaxation": relaxation,
                "sequence_specific_amber_assembly": {
                    "ligand_pdb": str(ligand_pdb), "ligand_prmtop": str(ligand_prmtop),
                    "ligand_inpcrd": str(ligand_inpcrd), "complex_prmtop": str(prmtop),
                    "complex_inpcrd": str(inpcrd), "ligand_charge": observed_charge,
                    "crosslinks": ligand_map["crosslinks"],
                },
                "post_relaxation_validation": metrics,
            }
            (job / "relaxation_metrics.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if failures:
                rejected.append({"reason": "; ".join(failures), **record})
            else:
                accepted.append(record)
        except Exception as exc:
            rejected.append({"candidate": index, "source_diffdock": candidate, "reason": f"relaxation job failed: {exc}", "job": str(job)})
        _relax_status(
            status_path, "candidate_completed", candidate=index,
            candidate_count=len(candidates), accepted=len(accepted), rejected=len(rejected), job=str(job),
        )
    accepted.sort(key=lambda item: (-item["source_diffdock"]["diffdock_confidence"],
                                    -item["post_relaxation_validation"]["contact_pairs_2p5_to_4p5A"],
                                    item["relaxation"]["ligand_heavy_atom_rmsd_angstrom"]))
    keep = int(cfg.get("keep_after_relax", 5)); shortlist = accepted[:keep]
    shortlist_path = paths["postfilter"] / "shortlist.json"
    rejected_path = paths["postfilter"] / "rejected_after_relaxation.json"
    shortlist_path.write_text(json.dumps(shortlist, ensure_ascii=False, indent=2), encoding="utf-8")
    rejected_path.write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    _relax_status(
        status_path, "completed", candidate_count=len(candidates), accepted_total=len(accepted),
        shortlisted=len(shortlist), rejected=len(rejected), shortlist=str(shortlist_path),
    )
    return {
        "parameterization_audit": parameterization["audit"], "post_relaxation_shortlist": str(shortlist_path),
        "shortlisted": len(shortlist), "rejected_after_relaxation": len(rejected), "rejected_json": str(rejected_path),
        "run_status": str(status_path),
        "top_relaxed_candidates": shortlist,
        "notice": "Fewer relaxed complexes passed post-relaxation gates than requested" if len(shortlist) < keep else None,
    }


def _write_explicit_h(source: Path, target: Path) -> None:
    from rdkit import Chem
    molecule = Chem.MolFromMolFile(str(source), removeHs=False)
    if molecule is None: raise ToolError(f"Cannot read selected ligand pose: {source}", error_type="invalid_input", retriable=False, backend="Vina")
    writer = Chem.SDWriter(str(target)); writer.write(Chem.AddHs(molecule, addCoords=True)); writer.close()


def _box(pdbqt: Path, padding: float, minimum: float) -> tuple[list[float], float]:
    points = _read_xyz_pdb(pdbqt)
    if not points: raise ToolError(f"No PDBQT atom coordinates: {pdbqt}", error_type="invalid_input", retriable=False, backend="Vina")
    low, high = ([min(point[i] for point in points) for i in range(3)], [max(point[i] for point in points) for i in range(3)])
    return ([(a + b) / 2 for a, b in zip(low, high)], max(minimum, max(b - a for a, b in zip(low, high)) + 2 * padding))


def _vina_score(log: str) -> float | None:
    """Parse Vina 1.2+ local-only, docking-table, and score-only output."""
    patterns = (
        # Vina --local_only: "Refined score: -2.542 kcal/mol"
        r"Refined\s+score\s*:\s*(-?\d+(?:\.\d+)?)\s*kcal/mol",
        # Vina 1.2 local refinement: "Estimated Free Energy of Binding : -2.542 (kcal/mol)"
        r"Estimated\s+Free\s+Energy\s+of\s+Binding\s*:\s*(-?\d+(?:\.\d+)?)\s*\(kcal/mol\)",
        # Vina --score_only: "Affinity: -2.542 (kcal/mol)"
        r"Affinity\s*:\s*(-?\d+(?:\.\d+)?)\s*\(kcal/mol\)",
        # Standard Vina docking mode table, mode 1 affinity column.
        r"^\s*1\s+(-?\d+(?:\.\d+)?)\s+",
    )
    for pattern in patterns:
        match = re.search(pattern, log, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return float(match.group(1))
    return None


def _vina(cfg: dict[str, Any], base: Path) -> dict[str, Any]:
    paths = _paths(cfg, base); shortlist_path = paths["postfilter"] / "shortlist.json"
    if not shortlist_path.is_file(): raise ToolError("Post-relaxation shortlist missing; run relax_complexes first.", error_type="invalid_input", retriable=False, backend="Vina")
    shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    if not shortlist: raise ToolError("Post-relaxation shortlist is empty; inspect rejected_after_relaxation.json.", error_type="invalid_input", retriable=False, backend="Vina")
    _mkdir(paths["vina"])
    results = []
    for index, candidate in enumerate(shortlist, 1):
        job = _mkdir(paths["vina"] / f"candidate_{index:02d}")
        relaxed = candidate["relaxation"]; source = candidate["source_diffdock"]
        receptor_pdbqt = job / "receptor.pdbqt"
        _run(_render(_required_command(cfg, "receptor_to_pdbqt", "Vina"), receptor=relaxed["relaxed_receptor"], output=receptor_pdbqt), job / "receptor_conversion.log")
        if not receptor_pdbqt.is_file():
            raise ToolError(f"Meeko did not write candidate-specific receptor PDBQT; see {job / 'receptor_conversion.log'}", error_type="unavailable", retriable=False, backend="Vina")
        explicit_h, ligand = job / "ligand_explicit_h.sdf", job / "ligand.pdbqt"
        _write_explicit_h(Path(relaxed["relaxed_ligand"]), explicit_h)
        _run(_render(_required_command(cfg, "ligand_to_pdbqt", "Vina"), ligand=explicit_h, output=ligand), job / "ligand_conversion.log")
        center, size = _box(ligand, float(cfg.get("vina_local_padding_angstrom", 4.0)), float(cfg.get("vina_minimum_box_size_angstrom", 28.0)))
        _run(_render(_required_command(cfg, "vina_score_only", "Vina"), receptor=receptor_pdbqt, ligand=ligand,
                     center_x=center[0], center_y=center[1], center_z=center[2], box_size=size), job / "vina_score_only.log")
        score_only = _vina_score((job / "vina_score_only.log").read_text(encoding="utf-8", errors="replace"))
        if score_only is None:
            raise ToolError(f"Vina score-only output is unrecognized; see {job / 'vina_score_only.log'}", error_type="unavailable", retriable=False, backend="Vina")
        output = job / "vina_out.pdbqt"
        _run(_render(_required_command(cfg, "vina", "Vina"), receptor=receptor_pdbqt, ligand=ligand, output=output,
                     center_x=center[0], center_y=center[1], center_z=center[2], box_size=size), job / "vina.log")
        log = (job / "vina.log").read_text(encoding="utf-8", errors="replace")
        score = _vina_score(log)
        if not output.is_file():
            raise ToolError(f"Vina did not write a local-refinement pose; see {job / 'vina.log'}", error_type="unavailable", retriable=False, backend="Vina")
        if score is None:
            raise ToolError(f"Vina wrote a pose but its score format is unrecognized; see {job / 'vina.log'}", error_type="unavailable", retriable=False, backend="Vina")
        # Preserve the hard-gate evidence with the Vina score.  ``candidate`` is
        # the accepted DiffDock record, not merely an input file name; dropping
        # these fields here would make the final three candidates scientifically
        # non-auditable.
        results.append({
            "candidate": index,
            "vina_score_only_kcal_per_mol": score_only,
            "vina_local_score_kcal_per_mol": score,
            "vina_local_pose": str(output),
            "recommended_complex_pose": relaxed["relaxed_complex"],
            "recommended_ligand_pose": relaxed["relaxed_ligand"],
            "vina_box_center": center,
            "vina_box_size_angstrom": size,
            "diffdock": {
                "pose": source["pose"],
                "original_pose": source.get("original_pose", source["pose"]),
                "confidence": source["diffdock_confidence"],
                "preliminary_score": source.get("preliminary_score"),
                "pocket_id": candidate["pocket_id"],
            },
            "relaxation": {
                "initial_physical_energy_kj_mol": relaxed["initial_physical_energy_kj_mol"],
                "final_physical_energy_kj_mol": relaxed["final_physical_energy_kj_mol"],
                "energy_decreased": relaxed["energy_decreased"],
                "final_restrained_rms_force_kj_mol_nm": relaxed["final_restrained_rms_force_kj_mol_nm"],
                "receptor_backbone_rmsd_angstrom": relaxed["receptor_backbone_rmsd_angstrom"],
                "ligand_heavy_atom_rmsd_angstrom": relaxed["ligand_heavy_atom_rmsd_angstrom"],
                "platform": relaxed["platform"], "implicit_solvent": relaxed["implicit_solvent"],
            },
            "pre_relaxation_validation": {
                "topology_matches_authoritative_sdf": source["topology_matches_authoritative_sdf"],
                "topology_reconstructed_from_authoritative_sdf": source["topology_reconstructed_from_authoritative_sdf"],
                "formal_charge_plus_5": source["formal_charge_plus_5"],
                "bad_heavy_atom_covalent_bond_count": source["bad_heavy_atom_covalent_bond_count"],
                "minimum_receptor_distance_angstrom": source["minimum_receptor_distance"],
                "clash_pairs_under_1p5A": source["clash_pairs_under_1p5A"],
                "contact_pairs_2p5_to_4p5A": source["contact_pairs_2p5_to_4p5A"],
                "pocket_centroid_distance_angstrom": source["pocket_centroid_distance"],
            },
            "post_relaxation_validation": candidate["post_relaxation_validation"],
            "parameterization_audit": candidate["parameterization_audit"],
            "sequence_specific_amber_assembly": candidate["sequence_specific_amber_assembly"],
        })
    results.sort(key=lambda item: (item["vina_local_score_kcal_per_mol"], item["vina_score_only_kcal_per_mol"],
                                   -item["diffdock"]["confidence"]))
    final = results[:int(cfg.get("keep_after_vina", 3))]
    report = paths["vina"] / "ranked_candidates.json"; report.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    # Include the selected records in the tool observation as well as on disk.
    # PredAgent can then state the ordered scores and all gate evidence without
    # relying on a separate file-reading tool after Vina has completed.
    return {
        "ranked_candidates": str(report),
        "selected": len(final),
        "top_candidates": final,
        "all_local_refinements": len(results),
    }


@bioma_tool(name="prepare_receptor", description="Select receptor polymer chains, remove all unrelated entities, repair with PDBFixer, and write an audit.", category="structure", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["receptor", "PDBFixer", "TBMB"], result_kind="computed_prediction", result_validator=peptide_result_validator)
def prepare_receptor(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("prepare_receptor", **_prepare(cfg, base))


@bioma_tool(name="parameterize_tbmb_scaffold", description="Seal reviewed multi-conformer RESP TBMB-Met residue templates and crosslink terms into a hash-bound reusable AMBER bundle, then prove reference-sequence TLEaP assembly and charge.", category="structure", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["AmberTools", "RESP", "ff14SB", "TBMB", "parameterization"])
def parameterize_tbmb_scaffold(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("parameterize_tbmb_scaffold", **_parameterize(cfg, base))


@bioma_tool(name="detect_pockets", description="Run P2Rank on the repaired receptor and retain up to five candidate pockets without inventing missing pockets.", category="prediction", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["P2Rank", "pocket", "TBMB"], result_kind="computed_prediction", result_validator=peptide_result_validator)
def detect_pockets(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("detect_pockets", **_detect(cfg, base))


@bioma_tool(name="diffdock_screen", description="Globally dock an authoritative TBMB SDF ensemble with DiffDock and retain chemically valid poses for physical relaxation without rejecting repairable interface clashes.", category="prediction", version="2.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["DiffDock", "TBMB", "bicyclic-peptide"], result_kind="computed_prediction", result_validator=peptide_result_validator)
def diffdock_screen(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("diffdock_screen", **_screen(cfg, base))


@bioma_tool(name="relax_complexes", description="Validate a hash-bound ff14SB/custom TBMB-Met parameter bundle, assemble complexes with AmberTools, perform staged restrained OpenMM/GBn2 minimization, and apply post-relaxation gates.", category="structure", version="2.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["OpenMM", "AmberTools", "ff14SB", "TBMB", "relaxation"])
def relax_complexes(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("relax_complexes", **_relax(cfg, base))


@bioma_tool(name="vina_refine", description="Use candidate-specific relaxed receptors for Vina score-only and local-only rescoring, then retain up to three auditable TBMB complexes.", category="prediction", version="2.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["Vina", "Meeko", "TBMB"], result_kind="computed_prediction", result_validator=peptide_result_validator)
def vina_refine(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("vina_refine", **_vina(cfg, base))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["parameterize", "prepare-receptor", "pockets", "diffdock", "relax", "vina", "all"])
    parser.add_argument("--query", required=True, help="Complete query YAML.")
    args = parser.parse_args(); cfg, base = _load(args.query)
    stages = {
        "parameterize": _parameterize, "prepare-receptor": _prepare, "pockets": _detect,
        "diffdock": _screen, "relax": _relax, "vina": _vina,
    }
    order = ["parameterize", "prepare-receptor", "pockets", "diffdock", "relax", "vina"]
    for name in (order if args.stage == "all" else [args.stage]):
        print(f"=== {name} ==="); print(json.dumps(stages[name](cfg, base), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
