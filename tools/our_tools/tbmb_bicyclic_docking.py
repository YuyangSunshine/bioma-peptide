#!/usr/bin/env python3
"""Four guarded tools for receptor-only TBMB bicyclic-peptide docking.

The public tool inputs deliberately accept one query YAML.  It makes a complete run
replayable and prevents a later tool call from silently using a different ligand graph,
target chain, external executable, or output directory.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shlex
import subprocess
from collections import Counter
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
    required = ["target_structure", "retain_chains", "ligand_ensemble_sdf", "ligand_metadata", "workdir", "commands"]
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise ToolError(f"Query YAML is missing required fields: {', '.join(missing)}", error_type="invalid_input", retriable=False, backend="TBMB docking")
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
        "audit": work / "01_receptor" / "cleanup_audit.json",
        "p2rank": work / "02_p2rank",
        "pockets": work / "02_p2rank" / "selected_pockets.json",
        "ligands": work / "00_inputs" / "tbmb_sdf",
        "diffdock": work / "03_diffdock",
        "filter": work / "04_filter",
        "vina": work / "05_vina",
    }


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _render(template: str, **items: object) -> str:
    return template.format(**{key: shlex.quote(str(value)) for key, value in items.items()})


def _run(command: str, log: Path, cwd: Path | None = None) -> None:
    result = subprocess.run(command, shell=True, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _mkdir(log.parent)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise ToolError(f"External command failed ({result.returncode}); see {log}", error_type="unavailable", retriable=False, backend="TBMB docking")


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
    audit = {"source": str(target), "retained_chains": sorted(keep), "retained_polymer_residues": len(observed),
             "target_fasta": str(_p(cfg["target_sequence"], base)) if cfg.get("target_sequence") else None,
             "target_fasta_length": len(requested_sequence) if requested_sequence else None,
             "sequence_check": sequence_check,
             "removed_residue_counts": dict(sorted(removed.items())), "nonstandard_replacements": substitutions,
             "policy": "Only polymer residues in retain_chains were preserved; all other chains, ligands, solvent, ions, glycans, and buffers were removed."}
    paths["audit"].write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"receptor": str(paths["receptor"]), "audit": str(paths["audit"])}


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
    match = re.search(r"confidence([-+]?\d+(?:\.\d+)?)", name, re.I)
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
    """Run global DiffDock and apply topology, charge, geometry, clash, and pocket gates."""
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
    max_pocket = float(cfg.get("max_pocket_centroid_distance", 10.0)); min_distance = float(cfg.get("min_heavy_atom_distance", 1.5))
    for pose in sorted(paths["diffdock"].rglob("*.sdf")):
        score = _confidence(pose.name)
        if math.isnan(score): rejected.append({"pose": str(pose), "reason": "unscored DiffDock duplicate pose"}); continue
        if score <= -999: rejected.append({"pose": str(pose), "reason": "DiffDock failed-pose sentinel confidence", "confidence": score}); continue
        try: checked, remapped, metrics = _regraph_and_metrics(pose, reference, receptor_xyz, paths["filter"] / "regraphed_poses")
        except ValueError as exc: rejected.append({"pose": str(pose), "reason": str(exc), "confidence": score}); continue
        pocket = min(pockets, key=lambda item: math.dist(metrics["ligand_centroid"], item["center"]))
        metrics["pocket_centroid_distance"] = math.dist(metrics["ligand_centroid"], pocket["center"])
        failures = []
        if not metrics["topology_matches_authoritative_sdf"]: failures.append("topology differs from authoritative TBMB SDF")
        if not metrics["formal_charge_plus_5"]: failures.append("formal charge is not +5")
        if metrics["minimum_receptor_distance"] < min_distance: failures.append("severe receptor-ligand clash")
        if metrics["clash_pairs_under_1p5A"]: failures.append("one or more heavy-atom clashes")
        if metrics["bad_heavy_atom_covalent_bond_count"]: failures.append("invalid internal covalent-bond geometry")
        if metrics["pocket_centroid_distance"] > max_pocket: failures.append("ligand centroid outside nearest P2Rank pocket")
        item = {"pose": str(checked), "original_pose": str(pose), "topology_reconstructed_from_authoritative_sdf": remapped,
                "pocket_id": pocket["pocket_id"], "diffdock_confidence": score, **metrics}
        if failures: rejected.append({"reason": "; ".join(failures), **item})
        else: item["preliminary_score"] = score + .01 * metrics["contact_pairs_2p5_to_4p5A"]; accepted.append(item)
    accepted.sort(key=lambda item: item["preliminary_score"], reverse=True)
    _mkdir(paths["filter"])
    keep = int(cfg.get("keep_after_diffdock", 5)); shortlist = accepted[:keep]
    (paths["filter"] / "shortlist.json").write_text(json.dumps(shortlist, ensure_ascii=False, indent=2), encoding="utf-8")
    (paths["filter"] / "rejected_poses.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"shortlist": str(paths["filter"] / "shortlist.json"), "shortlisted": len(shortlist), "rejected": len(rejected), "reused_diffdock_jobs": reused_jobs,
            "notice": "Fewer poses passed hard gates than requested" if len(shortlist) < keep else None}


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
    paths = _paths(cfg, base); shortlist_path = paths["filter"] / "shortlist.json"
    if not shortlist_path.is_file(): raise ToolError("DiffDock shortlist missing; run diffdock_screen first.", error_type="invalid_input", retriable=False, backend="Vina")
    shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    if not shortlist: raise ToolError("DiffDock shortlist is empty; inspect rejected_poses.json instead of continuing.", error_type="invalid_input", retriable=False, backend="Vina")
    _mkdir(paths["vina"]); receptor_pdbqt = paths["vina"] / "receptor.pdbqt"
    if not receptor_pdbqt.is_file(): _run(_render(cfg["commands"]["receptor_to_pdbqt"], receptor=paths["receptor"], output=receptor_pdbqt), paths["vina"] / "receptor_pdbqt.log")
    if not receptor_pdbqt.is_file(): raise ToolError("Meeko did not write receptor PDBQT; see receptor_pdbqt.log.", error_type="unavailable", retriable=False, backend="Vina")
    results = []
    for index, candidate in enumerate(shortlist, 1):
        job = _mkdir(paths["vina"] / f"candidate_{index:02d}"); explicit_h, ligand = job / "ligand_explicit_h.sdf", job / "ligand.pdbqt"
        _write_explicit_h(Path(candidate["pose"]), explicit_h)
        _run(_render(cfg["commands"]["ligand_to_pdbqt"], ligand=explicit_h, output=ligand), job / "ligand_conversion.log")
        center, size = _box(ligand, float(cfg.get("vina_local_padding_angstrom", 4.0)), float(cfg.get("vina_minimum_box_size_angstrom", 28.0)))
        output = job / "vina_out.pdbqt"
        _run(_render(cfg["commands"]["vina"], receptor=receptor_pdbqt, ligand=ligand, output=output, center_x=center[0], center_y=center[1], center_z=center[2], box_size=size), job / "vina.log")
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
            "vina_score_kcal_per_mol": score,
            "vina_pose": str(output),
            "vina_box_center": center,
            "vina_box_size_angstrom": size,
            "diffdock": {
                "pose": candidate["pose"],
                "original_pose": candidate.get("original_pose", candidate["pose"]),
                "confidence": candidate["diffdock_confidence"],
                "preliminary_score": candidate.get("preliminary_score"),
                "pocket_id": candidate["pocket_id"],
            },
            "hard_gate_validation": {
                "topology_matches_authoritative_sdf": candidate["topology_matches_authoritative_sdf"],
                "topology_reconstructed_from_authoritative_sdf": candidate["topology_reconstructed_from_authoritative_sdf"],
                "formal_charge_plus_5": candidate["formal_charge_plus_5"],
                "bad_heavy_atom_covalent_bond_count": candidate["bad_heavy_atom_covalent_bond_count"],
                "minimum_receptor_distance_angstrom": candidate["minimum_receptor_distance"],
                "clash_pairs_under_1p5A": candidate["clash_pairs_under_1p5A"],
                "contact_pairs_2p5_to_4p5A": candidate["contact_pairs_2p5_to_4p5A"],
                "pocket_centroid_distance_angstrom": candidate["pocket_centroid_distance"],
            },
        })
    results.sort(key=lambda item: item["vina_score_kcal_per_mol"])
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


@bioma_tool(name="prepare_receptor", description="Select receptor polymer chains, remove all unrelated entities, repair with PDBFixer, and write an audit.", category="structure", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["receptor", "PDBFixer", "TBMB"])
def prepare_receptor(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("prepare_receptor", **_prepare(cfg, base))


@bioma_tool(name="detect_pockets", description="Run P2Rank on the repaired receptor and retain up to five candidate pockets without inventing missing pockets.", category="prediction", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["P2Rank", "pocket", "TBMB"])
def detect_pockets(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("detect_pockets", **_detect(cfg, base))


@bioma_tool(name="diffdock_screen", description="Globally dock an authoritative TBMB SDF ensemble with DiffDock and retain chemically and geometrically valid poses.", category="prediction", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["DiffDock", "TBMB", "bicyclic-peptide"])
def diffdock_screen(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("diffdock_screen", **_screen(cfg, base))


@bioma_tool(name="vina_refine", description="Use Meeko and Vina local-only optimization to rescore valid TBMB poses and retain the top three.", category="prediction", version="1.0.0", author="BioMA", args_schema=DockingQueryInput, tags=["Vina", "Meeko", "TBMB"])
def vina_refine(query_path: str) -> str:
    cfg, base = _load(query_path); return _json_result("vina_refine", **_vina(cfg, base))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare-receptor", "pockets", "diffdock", "vina", "all"])
    parser.add_argument("--query", required=True, help="Complete query YAML.")
    args = parser.parse_args(); cfg, base = _load(args.query)
    stages = {"prepare-receptor": _prepare, "pockets": _detect, "diffdock": _screen, "vina": _vina}
    for name in (["prepare-receptor", "pockets", "diffdock", "vina"] if args.stage == "all" else [args.stage]):
        print(f"=== {name} ==="); print(json.dumps(stages[name](cfg, base), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
