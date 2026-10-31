#!/usr/bin/env python3
"""Parameter-independent preparation for TBMB/Nectin-4 molecular dynamics.

Stage A deliberately stops before TLEaP, solvation, minimization, or dynamics.  It
creates reproducible receptor-modeling inputs, audits structural decisions, binds
legacy docking poses to authoritative SDF records, validates the MD protocol, and
reports the remaining parameter-dependent gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AA3_TO_1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "HID": "H",
    "HIE": "H", "HIP": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T",
    "TRP": "W", "TYR": "Y", "VAL": "V", "CYX": "C",
}
AMBER_HIS = {"HID", "HIE", "HIP"}


class StageAError(RuntimeError):
    """A deterministic Stage-A input or validation failure."""


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


def _load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise StageAError(
                "PyYAML is required for YAML configuration; use JSON or install the "
                "project dependencies."
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise StageAError("Stage-A configuration must contain one top-level mapping")
    return data


def _validate_config_schema(config: dict[str, Any], schema_path: Path) -> None:
    """Validate the project configuration before any artifacts are written."""
    if not schema_path.is_file():
        raise StageAError(f"Stage-A JSON Schema is missing: {schema_path}")
    try:
        import jsonschema
    except ImportError as exc:
        raise StageAError(
            "jsonschema is required to validate the Stage-A configuration"
        ) from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(config), key=lambda item: list(item.absolute_path))
    if errors:
        details = []
        for error in errors[:10]:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{location}: {error.message}")
        suffix = f"; plus {len(errors) - 10} more" if len(errors) > 10 else ""
        raise StageAError("Invalid Stage-A configuration: " + "; ".join(details) + suffix)


def _resolve(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _resolve_record_path(value: str, base: Path, aliases: dict[str, str]) -> Path:
    direct = Path(value).expanduser()
    if direct.is_file():
        return direct.resolve()
    for remote, local in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        remote_prefix = remote.rstrip("/")
        if value == remote_prefix or value.startswith(remote_prefix + "/"):
            suffix = value[len(remote_prefix):].lstrip("/")
            candidate = _resolve(local, base) / suffix
            if candidate.is_file():
                return candidate.resolve()
    candidate = _resolve(value, base)
    if candidate.is_file():
        return candidate
    raise StageAError(f"Cannot resolve artifact path: {value}")


def _parse_fasta(path: Path) -> str:
    sequence = "".join(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(">")
    ).upper()
    if not sequence or re.search(r"[^ACDEFGHIKLMNPQRSTVWY]", sequence):
        raise StageAError(f"Invalid protein FASTA: {path}")
    return sequence


@dataclass(frozen=True)
class PDBAtom:
    serial: int
    atom_name: str
    residue_name: str
    chain: str
    residue_number: int
    insertion_code: str
    element: str
    xyz: tuple[float, float, float]


def _pdb_atoms(path: Path, chain: str | None = None, heavy_only: bool = False) -> list[PDBAtom]:
    atoms: list[PDBAtom] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        atom_chain = line[21:22].strip()
        if chain is not None and atom_chain != chain:
            continue
        atom_name = line[12:16].strip()
        element = line[76:78].strip().upper() or re.sub(r"[^A-Za-z]", "", atom_name)[:1].upper()
        if heavy_only and element == "H":
            continue
        try:
            atoms.append(PDBAtom(
                serial=int(line[6:11]), atom_name=atom_name,
                residue_name=line[17:20].strip().upper(), chain=atom_chain,
                residue_number=int(line[22:26]), insertion_code=line[26:27].strip(),
                element=element,
                xyz=(float(line[30:38]), float(line[38:46]), float(line[46:54])),
            ))
        except ValueError as exc:
            raise StageAError(f"Malformed PDB atom line in {path}: {line!r}") from exc
    if not atoms:
        raise StageAError(f"No matching atoms found in PDB: {path}")
    return atoms


def _residue_table(atoms: Iterable[PDBAtom]) -> dict[int, str]:
    residues: dict[int, str] = {}
    for atom in atoms:
        if atom.insertion_code:
            raise StageAError(
                f"Insertion code is not supported in Stage-A numbering: "
                f"{atom.chain}:{atom.residue_number}{atom.insertion_code}"
            )
        letter = AA3_TO_1.get(atom.residue_name)
        if not letter:
            raise StageAError(f"Unsupported receptor residue: {atom.residue_name}")
        previous = residues.setdefault(atom.residue_number, letter)
        if previous != letter:
            raise StageAError(f"Conflicting residue names at {atom.chain}:{atom.residue_number}")
    return residues


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def audit_receptor(config: dict[str, Any], base: Path) -> dict[str, Any]:
    receptor = config["receptor"]
    source_structure = _resolve(receptor["source_structure"], base)
    coordinate = _resolve(receptor["coordinate_pdb"], base)
    fasta = _resolve(receptor["target_fasta"], base)
    if not source_structure.is_file():
        raise StageAError(f"Original receptor structure is missing: {source_structure}")
    chain = str(receptor["chain_id"])
    start = int(receptor["construct"]["start"])
    end = int(receptor["construct"]["end"])
    target = _parse_fasta(fasta)
    if len(target) != end - start + 1:
        raise StageAError(
            f"Target FASTA length {len(target)} does not match construct {start}-{end}"
        )
    atoms = _pdb_atoms(coordinate, chain=chain)
    residues = _residue_table(atoms)
    expected = {number: target[number - start] for number in range(start, end + 1)}
    mismatches = [
        {"residue_number": number, "expected": expected[number], "observed": observed}
        for number, observed in sorted(residues.items())
        if number in expected and observed != expected[number]
    ]
    missing = [number for number in expected if number not in residues]
    unexpected = [number for number in residues if number not in expected]

    configured_missing: list[int] = []
    configured_region_sequence_mismatches: list[dict[str, Any]] = []
    for region in receptor.get("missing_regions", []):
        region_start = int(region["start"])
        region_end = int(region["end"])
        configured_missing.extend(range(region_start, region_end + 1))
        observed_sequence = str(region.get("sequence", "")).upper()
        expected_sequence = "".join(
            expected.get(number, "?") for number in range(region_start, region_end + 1)
        )
        if observed_sequence != expected_sequence:
            configured_region_sequence_mismatches.append({
                "start": region_start,
                "end": region_end,
                "configured": observed_sequence,
                "expected_from_fasta": expected_sequence,
            })
    configured_missing = sorted(set(configured_missing))

    atom_lookup = {
        (atom.residue_number, atom.atom_name): atom for atom in atoms
    }
    peptide_gaps: list[dict[str, Any]] = []
    for left in range(start, end):
        carbon = atom_lookup.get((left, "C"))
        nitrogen = atom_lookup.get((left + 1, "N"))
        if carbon and nitrogen:
            length = _distance(carbon.xyz, nitrogen.xyz)
            if not 1.15 <= length <= 1.55:
                peptide_gaps.append({"left": left, "right": left + 1, "c_n_angstrom": length})

    disulfide_checks: list[dict[str, Any]] = []
    for bond in receptor.get("disulfides", []):
        first = int(bond["first"])
        second = int(bond["second"])
        left = atom_lookup.get((first, "SG"))
        right = atom_lookup.get((second, "SG"))
        length = _distance(left.xyz, right.xyz) if left and right else None
        disulfide_checks.append({
            "chain": bond.get("chain", chain), "first": first, "second": second,
            "sg_distance_angstrom": length,
            "passes_geometry": length is not None and 1.8 <= length <= 2.2,
        })

    histidines = []
    for decision in receptor.get("histidines", []):
        number = int(decision["residue"])
        observed_names = sorted({
            atom.residue_name for atom in atoms if atom.residue_number == number
        })
        state = str(decision["state"]).upper()
        histidines.append({
            "chain": decision.get("chain", chain), "residue": number,
            "configured_state": state, "observed_residue_names": observed_names,
            "resolved_for_amber": state in AMBER_HIS,
            "candidate_sensitive": bool(decision.get("candidate_sensitive", False)),
        })

    return {
        "original_structure": {"path": str(source_structure), "sha256": _sha256(source_structure)},
        "source": {"path": str(coordinate), "sha256": _sha256(coordinate)},
        "target_fasta": {"path": str(fasta), "sha256": _sha256(fasta), "sequence": target},
        "chain": chain, "construct": receptor["construct"],
        "coordinate_residue_count": len(residues), "expected_residue_count": len(target),
        "missing_residue_numbers": missing,
        "configured_missing_residue_numbers": configured_missing,
        "missing_regions_match_coordinates": (
            missing == configured_missing and not configured_region_sequence_mismatches
        ),
        "configured_region_sequence_mismatches": configured_region_sequence_mismatches,
        "sequence_mismatches": mismatches, "unexpected_residue_numbers": unexpected,
        "abnormal_observed_peptide_bonds": peptide_gaps,
        "disulfides": disulfide_checks, "histidines": histidines,
        "complete_model": not missing and not mismatches and not unexpected and not peptide_gaps,
    }


def _heavy_template_pdb(source: Path, target: Path, chain: str) -> None:
    lines: list[str] = []
    serial = 1
    for raw in source.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.startswith(("ATOM  ", "HETATM")) or raw[21:22].strip() != chain:
            continue
        atom_name = raw[12:16].strip()
        element = raw[76:78].strip().upper() or re.sub(r"[^A-Za-z]", "", atom_name)[:1].upper()
        if element == "H":
            continue
        line = "ATOM  " + f"{serial:5d}" + raw[11:]
        lines.append(line)
        serial += 1
    if not lines:
        raise StageAError(f"Cannot create heavy-atom template from {source}")
    lines.extend(["TER", "END"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pir_sequence(sequence: str, width: int = 75) -> str:
    return "\n".join(sequence[index:index + width] for index in range(0, len(sequence), width))


def write_modeller_plan(config: dict[str, Any], base: Path, audit: dict[str, Any]) -> dict[str, Any]:
    if not audit["missing_regions_match_coordinates"]:
        raise StageAError(
            "Configured missing regions do not exactly match the receptor coordinates and FASTA"
        )
    receptor = config["receptor"]
    output = _resolve(config["output_dir"], base) / "receptor" / "modeller"
    output.mkdir(parents=True, exist_ok=True)
    coordinate = _resolve(receptor["coordinate_pdb"], base)
    chain = str(receptor["chain_id"])
    start = int(receptor["construct"]["start"])
    end = int(receptor["construct"]["end"])
    target_sequence = audit["target_fasta"]["sequence"]
    residues = _residue_table(_pdb_atoms(coordinate, chain=chain))
    template_sequence = "".join(
        residues.get(number, "-") for number in range(start, end + 1)
    )
    template = output / "9kkj_d1_template.pdb"
    _heavy_template_pdb(coordinate, template, chain)
    alignment = output / "nectin4_d1.ali"
    alignment.write_text(
        ">P1;9kkj_d1_template\n"
        f"structureX:9kkj_d1_template:{min(residues)}:{chain}:{max(residues)}:{chain}::::\n"
        f"{_pir_sequence(template_sequence)}*\n"
        ">P1;nectin4_d1_target\n"
        f"sequence:nectin4_d1_target:{start}:{chain}:{end}:{chain}::::\n"
        f"{_pir_sequence(target_sequence)}*\n",
        encoding="utf-8",
    )

    regions = [
        (int(item["start"]), int(item["end"])) for item in receptor.get("missing_regions", [])
    ]
    selections = ",\n                ".join(
        f"self.residue_range('{left}:{chain}', '{right}:{chain}')" for left, right in regions
    )
    disulfide_lines = "\n".join(
        f"        self.patch(residue_type='DISU', residues=(self.residues['{int(item['first'])}:{chain}'], "
        f"self.residues['{int(item['second'])}:{chain}']))"
        for item in receptor.get("disulfides", [])
    ) or "        pass"
    modeller = receptor.get("modeller", {})
    model_count = int(modeller.get("models", 20))
    script = output / "run_modeller.py"
    script.write_text(
        "from modeller import Environ, Selection, log\n"
        "from modeller.automodel import DOPEHRLoopModel, assess, refine\n"
        "import json\n\n"
        "log.verbose()\n"
        "env = Environ()\n"
        "env.io.atom_files_directory = ['.']\n\n"
        "class Nectin4LoopModel(DOPEHRLoopModel):\n"
        "    def select_loop_atoms(self):\n"
        "        return Selection(\n"
        f"                {selections}\n"
        "        )\n\n"
        "    def special_patches(self, aln):\n"
        f"        self.rename_segments(segment_ids=['{chain}'], renumber_residues=[{start}])\n"
        f"{disulfide_lines}\n\n"
        "model = Nectin4LoopModel(\n"
        "    env, alnfile='nectin4_d1.ali', knowns='9kkj_d1_template',\n"
        "    sequence='nectin4_d1_target',\n"
        "    loop_assess_methods=(assess.DOPE, assess.normalized_dope),\n"
        ")\n"
        "model.starting_model = 1\n"
        "model.ending_model = 1\n"
        "model.loop.starting_model = 1\n"
        f"model.loop.ending_model = {model_count}\n"
        "model.loop.md_level = refine.slow\n"
        "model.make()\n"
        "with open('model_scores.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(model.loop.outputs, handle, indent=2, default=str)\n",
        encoding="utf-8",
    )
    readme = output / "README.txt"
    readme.write_text(
        "TBMB/Nectin-4 阶段 A：MODELLER 补模任务\n"
        "========================================\n\n"
        "本任务将补建 Gly32 和内部缺失片段 GDSGE56-60，并产生 20 个环区模型。\n"
        "需要在已安装并配置 MODELLER 许可证的 Python 环境中运行。\n\n"
        "服务器命令：\n"
        "  cd <PROJECT_ROOT>/"
        "artifacts/tbmb_9kkj_md_stage_a/receptor/modeller\n"
        "  python -c \"import modeller; print('MODELLER 可用')\"\n"
        "  python run_modeller.py\n\n"
        "运行结束后回到仓库根目录执行：\n"
        "  cd <PROJECT_ROOT>\n"
        "  python tools/our_tools/tbmb_md_stage_a.py finalize-receptor "
        "--config tests/tbmb_md_stage_a_config.yaml\n\n"
        "注意：MODELLER 输出只是候选受体模型，不会被自动批准。\n"
        "还需要检查缺失区几何、界面、His83/His89 质子化和构建体端基。\n",
        encoding="utf-8",
    )
    return {
        "status": "planned_waiting_for_modeller",
        "directory": str(output),
        "template": {"path": str(template), "sha256": _sha256(template)},
        "alignment": {"path": str(alignment), "sha256": _sha256(alignment)},
        "script": {"path": str(script), "sha256": _sha256(script)},
        "requested_models": model_count,
        "keep_models": int(modeller.get("keep", 3)),
        "missing_regions": receptor.get("missing_regions", []),
        "note": "4JJH is supporting evidence but no single 4JJH chain contains the complete GDSGE loop.",
    }


@dataclass(frozen=True)
class SDFAtom:
    element: str
    charge: int
    xyz: tuple[float, float, float]


@dataclass(frozen=True)
class SDFRecord:
    atoms: tuple[SDFAtom, ...]
    bonds: tuple[tuple[int, int, int], ...]

    @property
    def net_charge(self) -> int:
        return sum(atom.charge for atom in self.atoms)

    @property
    def graph_sha256(self) -> str:
        payload = {
            "atoms": [(atom.element, atom.charge) for atom in self.atoms],
            "bonds": sorted(self.bonds),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def connectivity_sha256(self) -> str:
        """Connectivity identity independent of aromatic/Kekule serialization."""
        payload = {
            "atoms": [(atom.element, atom.charge) for atom in self.atoms],
            "edges": sorted((left, right) for left, right, _ in self.bonds),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _read_sdf(path: Path) -> SDFRecord:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 5 or "V2000" not in lines[3]:
        raise StageAError(f"Only single-record V2000 SDF is supported: {path}")
    try:
        atom_count = int(lines[3][0:3])
        bond_count = int(lines[3][3:6])
    except ValueError as exc:
        raise StageAError(f"Malformed SDF counts line: {path}") from exc
    charge_codes = {0: 0, 1: 3, 2: 2, 3: 1, 5: -1, 6: -2, 7: -3}
    atoms: list[SDFAtom] = []
    for raw in lines[4:4 + atom_count]:
        try:
            code = int(raw[36:39] or "0")
            atoms.append(SDFAtom(
                element=raw[31:34].strip().upper(), charge=charge_codes.get(code, 0),
                xyz=(float(raw[0:10]), float(raw[10:20]), float(raw[20:30])),
            ))
        except (ValueError, IndexError) as exc:
            raise StageAError(f"Malformed SDF atom line in {path}: {raw!r}") from exc
    bonds: list[tuple[int, int, int]] = []
    for raw in lines[4 + atom_count:4 + atom_count + bond_count]:
        try:
            left, right, order = int(raw[0:3]) - 1, int(raw[3:6]) - 1, int(raw[6:9])
        except ValueError as exc:
            raise StageAError(f"Malformed SDF bond line in {path}: {raw!r}") from exc
        bonds.append((min(left, right), max(left, right), order))
    for raw in lines[4 + atom_count + bond_count:]:
        if not raw.startswith("M  CHG"):
            continue
        fields = raw.split()
        try:
            pair_count = int(fields[2])
            for index in range(pair_count):
                atom_index = int(fields[3 + 2 * index]) - 1
                charge = int(fields[4 + 2 * index])
                atoms[atom_index] = SDFAtom(atoms[atom_index].element, charge, atoms[atom_index].xyz)
        except (ValueError, IndexError) as exc:
            raise StageAError(f"Malformed M  CHG record in {path}: {raw!r}") from exc
    return SDFRecord(tuple(atoms), tuple(sorted(bonds)))


def _centroid(points: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    return tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))  # type: ignore[return-value]


def _largest_eigenvector(matrix: list[list[float]], iterations: int = 100) -> list[float]:
    """Largest algebraic eigenvector of a 4x4 symmetric matrix by Jacobi sweeps."""
    values = [row[:] for row in matrix]
    vectors = [[float(i == j) for j in range(4)] for i in range(4)]
    for _ in range(iterations):
        p, q = max(
            ((i, j) for i in range(4) for j in range(i + 1, 4)),
            key=lambda pair: abs(values[pair[0]][pair[1]]),
        )
        if abs(values[p][q]) < 1e-14:
            break
        angle = 0.5 * math.atan2(2.0 * values[p][q], values[q][q] - values[p][p])
        sine, cosine = math.sin(angle), math.cos(angle)
        for row in range(4):
            if row in {p, q}:
                continue
            left, right = values[row][p], values[row][q]
            values[row][p] = values[p][row] = cosine * left - sine * right
            values[row][q] = values[q][row] = sine * left + cosine * right
        app, aqq, apq = values[p][p], values[q][q], values[p][q]
        values[p][p] = cosine * cosine * app - 2 * sine * cosine * apq + sine * sine * aqq
        values[q][q] = sine * sine * app + 2 * sine * cosine * apq + cosine * cosine * aqq
        values[p][q] = values[q][p] = 0.0
        for row in range(4):
            left, right = vectors[row][p], vectors[row][q]
            vectors[row][p] = cosine * left - sine * right
            vectors[row][q] = sine * left + cosine * right
    index = max(range(4), key=lambda item: values[item][item])
    vector = [vectors[row][index] for row in range(4)]
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def _aligned_rmsd(first: list[tuple[float, float, float]], second: list[tuple[float, float, float]]) -> float:
    """Horn quaternion least-squares RMSD without a NumPy dependency."""
    if len(first) != len(second) or not first:
        raise StageAError("RMSD point sets must have the same non-zero length")
    ca, cb = _centroid(first), _centroid(second)
    left = [[point[i] - ca[i] for i in range(3)] for point in first]
    right = [[point[i] - cb[i] for i in range(3)] for point in second]
    s = [[sum(a[i] * b[j] for a, b in zip(left, right)) for j in range(3)] for i in range(3)]
    sxx, sxy, sxz = s[0]
    syx, syy, syz = s[1]
    szx, szy, szz = s[2]
    trace = sxx + syy + szz
    n = [
        [trace, syz - szy, szx - sxz, sxy - syx],
        [syz - szy, sxx - syy - szz, sxy + syx, szx + sxz],
        [szx - sxz, sxy + syx, -sxx + syy - szz, syz + szy],
        [sxy - syx, szx + sxz, syz + szy, -sxx - syy + szz],
    ]
    w, x, y, z = _largest_eigenvector(n)
    rotation = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    squared = 0.0
    for source, target in zip(left, right):
        rotated = [sum(rotation[i][j] * source[j] for j in range(3)) for i in range(3)]
        squared += sum((rotated[i] - target[i]) ** 2 for i in range(3))
    return math.sqrt(squared / len(first))


def build_candidate_manifest(config: dict[str, Any], base: Path) -> dict[str, Any]:
    section = config["candidates"]
    aliases = {str(key): str(value) for key, value in section.get("path_aliases", {}).items()}
    shortlist_path = _resolve(section["shortlist_json"], base)
    ranked_path = _resolve(section["ranked_json"], base)
    source_dir = _resolve(section["source_sdf_dir"], base)
    vina_dir = _resolve(section["vina_dir"], base)
    shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
    if not isinstance(shortlist, list) or not isinstance(ranked, list):
        raise StageAError("Candidate shortlist and ranked JSON must contain arrays")
    expected_shortlist = int(section.get("expected_shortlist", 5))
    expected_ranked = int(section.get("expected_ranked", 3))
    if len(shortlist) != expected_shortlist or len(ranked) != expected_ranked:
        raise StageAError(
            f"Candidate count mismatch: shortlist={len(shortlist)}/{expected_shortlist}, "
            f"ranked={len(ranked)}/{expected_ranked}"
        )
    vina_rank_by_job = {int(item["candidate"]): rank for rank, item in enumerate(ranked, 1)}
    ranked_record_by_job = {int(item["candidate"]): item for item in ranked}
    output = _resolve(config["output_dir"], base) / "candidates"
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for shortlist_index, item in enumerate(shortlist, 1):
        pose_value = str(item["pose"])
        pose = _resolve_record_path(pose_value, base, aliases)
        match = re.search(r"TBMB_conf_(\d{3})", pose_value)
        if not match:
            raise StageAError(f"Cannot identify source conformer from pose path: {pose_value}")
        conformer = int(match.group(1))
        source = source_dir / f"TBMB_conf_{conformer:03d}.sdf"
        if not source.is_file():
            raise StageAError(f"Authoritative source conformer is missing: {source}")
        source_sdf, pose_sdf = _read_sdf(source), _read_sdf(pose)
        elements_match = [atom.element for atom in source_sdf.atoms] == [atom.element for atom in pose_sdf.atoms]
        if not elements_match:
            raise StageAError(f"Source/pose atom order differs for shortlist candidate {shortlist_index}")
        if source_sdf.net_charge != int(config["ligand"]["net_charge"]):
            raise StageAError(
                f"Source conformer charge is {source_sdf.net_charge}, expected {config['ligand']['net_charge']}"
            )
        if pose_sdf.net_charge != source_sdf.net_charge:
            raise StageAError(
                f"Source/pose formal charge differs for shortlist candidate {shortlist_index}: "
                f"{source_sdf.net_charge} versus {pose_sdf.net_charge}"
            )
        if pose_sdf.connectivity_sha256 != source_sdf.connectivity_sha256:
            raise StageAError(
                f"Source/pose indexed connectivity differs for shortlist candidate {shortlist_index}; "
                "an identity atom map is unsafe"
            )
        atom_map = [
            {"source_atom_index": index, "pose_atom_index": index, "element": atom.element}
            for index, atom in enumerate(source_sdf.atoms)
        ]
        map_path = output / f"candidate_{shortlist_index:02d}_atom_map.json"
        _write_json(map_path, atom_map)
        source_heavy = [atom.xyz for atom in source_sdf.atoms if atom.element != "H"]
        pose_heavy = [atom.xyz for atom in pose_sdf.atoms if atom.element != "H"]
        job = vina_dir / f"candidate_{shortlist_index:02d}"
        files: dict[str, Any] = {}
        for label, path in {
            "ligand_explicit_h_sdf": job / "ligand_explicit_h.sdf",
            "ligand_pdbqt": job / "ligand.pdbqt",
            "vina_pose_pdbqt": job / "vina_out.pdbqt",
        }.items():
            files[label] = (
                {"path": str(path.resolve()), "sha256": _sha256(path)}
                if path.is_file() else None
            )
        ranked_record = ranked_record_by_job.get(shortlist_index, {})
        records.append({
            "candidate_id": f"legacy_shortlist_{shortlist_index:02d}",
            "legacy_status": "coordinate_only_unrelaxed",
            "shortlist_index": shortlist_index,
            "vina_final_rank": vina_rank_by_job.get(shortlist_index),
            "source_conformer": conformer,
            "authoritative_source_sdf": {"path": str(source.resolve()), "sha256": _sha256(source)},
            "diffdock_pose_sdf": {"path": str(pose), "sha256": _sha256(pose)},
            "legacy_vina_files": files,
            "legacy_vina_score_kcal_per_mol": ranked_record.get("vina_score_kcal_per_mol"),
            "diffdock_confidence": item.get("diffdock_confidence"),
            "pocket_id": item.get("pocket_id"),
            "chemistry": {
                "net_charge": source_sdf.net_charge,
                "atom_count": len(source_sdf.atoms),
                "heavy_atom_count": len(source_heavy),
                "graph_sha256": source_sdf.graph_sha256,
                "connectivity_sha256": source_sdf.connectivity_sha256,
                "pose_connectivity_matches_authoritative": (
                    pose_sdf.connectivity_sha256 == source_sdf.connectivity_sha256
                ),
                "pose_bond_order_serialization_matches_authoritative": (
                    pose_sdf.graph_sha256 == source_sdf.graph_sha256
                ),
                "authoritative_graph_policy": (
                    "Always use source SDF bond orders; docking SDF may contain an equivalent "
                    "aromatic/Kekule serialization."
                ),
                "crosslink_positions": config["ligand"]["crosslink_positions"],
                "sulfur_stereochemistry": config["ligand"]["sulfur_stereochemistry"],
            },
            "source_to_pose_mapping": {
                "method": "validated_identical_atom_order",
                "atom_map": {"path": str(map_path), "sha256": _sha256(map_path)},
                "mapped_heavy_atom_rmsd_angstrom": _aligned_rmsd(source_heavy, pose_heavy),
            },
        })
    manifest = {
        "schema_version": 1, "generated_at": _utc_now(),
        "status": "legacy_coordinates_catalogued_not_md_ready",
        "shortlist_source": {"path": str(shortlist_path), "sha256": _sha256(shortlist_path)},
        "ranked_source": {"path": str(ranked_path), "sha256": _sha256(ranked_path)},
        "shortlist_count": len(shortlist), "vina_ranked_count": len(ranked),
        "candidates": records,
    }
    target = output / "candidate_manifest.json"
    _write_json(target, manifest)
    manifest["manifest"] = {"path": str(target), "sha256": _sha256(target)}
    return manifest


def _severe_nonbonded_clashes(path: Path, chain: str) -> int:
    atoms = _pdb_atoms(path, chain=chain, heavy_only=True)
    count = 0
    for index, left in enumerate(atoms):
        for right in atoms[index + 1:]:
            separation = abs(left.residue_number - right.residue_number)
            if left.residue_number == right.residue_number or separation == 1:
                continue
            if {left.residue_number, right.residue_number} == {52, 127}:
                continue
            if _distance(left.xyz, right.xyz) < 1.5:
                count += 1
    return count


def finalize_receptor_models(config: dict[str, Any], base: Path) -> dict[str, Any]:
    """Audit MODELLER outputs without silently approving a scientific model."""
    receptor = config["receptor"]
    output = _resolve(config["output_dir"], base) / "receptor"
    modeller_dir = output / "modeller"
    candidates = sorted(
        path for path in modeller_dir.glob("*.pdb")
        if path.name != "9kkj_d1_template.pdb"
    )
    if not candidates:
        raise StageAError(f"No MODELLER output PDB files found in {modeller_dir}")
    scores_path = modeller_dir / "model_scores.json"
    score_by_name: dict[str, dict[str, Any]] = {}
    if scores_path.is_file():
        raw_scores = json.loads(scores_path.read_text(encoding="utf-8"))
        if isinstance(raw_scores, list):
            for record in raw_scores:
                if isinstance(record, dict) and record.get("name"):
                    score_by_name[Path(str(record["name"])).name] = record
    records: list[dict[str, Any]] = []
    for path in candidates:
        model_config = dict(config)
        model_receptor = dict(receptor)
        model_receptor["coordinate_pdb"] = str(path)
        model_config["receptor"] = model_receptor
        audit = audit_receptor(model_config, base)
        clashes = _severe_nonbonded_clashes(path, str(receptor["chain_id"]))
        passes = (
            audit["complete_model"]
            and all(item["passes_geometry"] for item in audit["disulfides"])
            and clashes == 0
        )
        score_record = score_by_name.get(path.name, {})
        normalized_dope = score_record.get("Normalized DOPE score")
        if normalized_dope is None:
            normalized_dope = score_record.get("Normalized DOPE")
        records.append({
            "path": str(path.resolve()), "sha256": _sha256(path),
            "passes_automated_geometry_gates": passes,
            "severe_nonbonded_clashes_under_1p5A": clashes,
            "audit": audit,
            "modeller_scores": score_record,
            "normalized_dope": normalized_dope,
        })
    passed = [record for record in records if record["passes_automated_geometry_gates"]]
    passed.sort(key=lambda item: (
        item["normalized_dope"] is None,
        float(item["normalized_dope"]) if item["normalized_dope"] is not None else math.inf,
        item["path"],
    ))
    keep = int(receptor.get("modeller", {}).get("keep", 3))
    selected = passed[:keep]
    unresolved = [
        int(item["residue"]) for item in receptor.get("histidines", [])
        if str(item.get("state", "")).upper() not in AMBER_HIS
    ]
    manifest = {
        "schema_version": 1, "generated_at": _utc_now(),
        "status": (
            "models_passed_waiting_for_histidine_and_human_review"
            if selected and unresolved else
            "models_passed_waiting_for_human_review" if selected else
            "no_model_passed_automated_gates"
        ),
        "approved": False,
        "approval_policy": (
            "Automated gates never approve a receptor. Resolve HID/HIE/HIP, inspect the "
            "loop and interface visually, then set approved=true and approved_model to one "
            "selected model in a reviewed copy of this manifest."
        ),
        "construct": receptor["construct"],
        "disulfides": receptor.get("disulfides", []),
        "histidines": receptor.get("histidines", []),
        "unresolved_histidines": unresolved,
        "models_generated": len(records), "models_passed": len(passed),
        "selected_for_review": selected,
        "all_models": records,
        "approved_model": None,
    }
    target = output / "receptor_model_manifest.json"
    _write_json(target, manifest)
    return {"manifest": str(target), **manifest}


def validate_protocol(config: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    if int(config.get("stage_a_schema_version", 0)) != 1:
        failures.append("stage_a_schema_version must be 1")
    if config.get("status") != "prepared_waiting_for_parameters":
        failures.append("status must be prepared_waiting_for_parameters")
    ligand = config.get("ligand", {})
    ligand_sequence = str(ligand.get("sequence", "")).upper()
    if int(ligand.get("net_charge", 999)) != 5:
        failures.append("ligand.net_charge must be +5")
    if [int(value) for value in ligand.get("crosslink_positions", [])] != [2, 9, 16]:
        failures.append("ligand.crosslink_positions must be [2, 9, 16]")
    elif any(
        position > len(ligand_sequence) or ligand_sequence[position - 1] != "M"
        for position in (2, 9, 16)
    ):
        failures.append("ligand sequence must contain Met at crosslink positions 2, 9, and 16")
    if ligand.get("n_terminal") != "ACE" or ligand.get("c_terminal") != "NHE":
        failures.append("ligand termini must be the confirmed ACE/NHE chemical state")
    if ligand.get("sulfur_stereochemistry") not in {"unspecified", "enumerated", "experimentally_assigned"}:
        failures.append("ligand.sulfur_stereochemistry must state how sulfur stereochemistry is handled")
    md = config.get("md", {})
    if str(md.get("protein_forcefield", "")).lower() != "ff14sb":
        failures.append("md.protein_forcefield must remain ff14SB for this parameter workflow")
    if str(md.get("water_model", "")).upper() != "TIP3P":
        failures.append("md.water_model must be TIP3P for the current baseline")
    if str(md.get("nonbonded_method", "")).upper() != "PME":
        failures.append("md.nonbonded_method must be PME")
    if float(md.get("padding_angstrom", 0)) < 12.0:
        failures.append("md.padding_angstrom must be at least 12.0")
    if not 0.0 <= float(md.get("salt_molar", -1)) <= 1.0:
        failures.append("md.salt_molar must be between 0 and 1 M")
    if float(md.get("timestep_fs", 0)) > 2.0 and not md.get("hydrogen_mass_repartitioning"):
        failures.append("timesteps above 2 fs require explicit HMR validation")
    if not md.get("constrain_h_bonds"):
        failures.append("2 fs baseline requires hydrogen-bond constraints")
    if int(md.get("replicas", 0)) < 3:
        warnings.append("fewer than three replicas weakens candidate ranking")
    if not md.get("random_seeds") or len(md["random_seeds"]) != int(md.get("replicas", 0)):
        failures.append("md.random_seeds count must equal md.replicas")
    elif len(set(md["random_seeds"])) != len(md["random_seeds"]):
        failures.append("md.random_seeds must be unique across replicas")
    parameters = config.get("parameters", {})
    if parameters.get("bundle_manifest"):
        warnings.append("Stage A records but does not validate or promote the parameter bundle")
    if parameters.get("required_capability") != "production_md":
        failures.append("parameters.required_capability must be production_md")
    receptor = config.get("receptor", {})
    states = {str(item.get("state", "")).upper() for item in receptor.get("histidines", [])}
    if "UNRESOLVED" in states:
        warnings.append("His protonation remains unresolved; production topology must stay blocked")
    construct = receptor.get("construct", {})
    allowed_terminal_states = {"ACE", "NME", "NHE", "charged", "neutral"}
    for label in ("n_terminal", "c_terminal"):
        if construct.get(label) not in allowed_terminal_states:
            failures.append(
                f"receptor.construct.{label} must explicitly define the terminal chemical state"
            )

    conditions = config.get("experimental_conditions", {})
    if not conditions.get("source"):
        warnings.append("binding-buffer source remains unspecified")
    buffer_name = str(conditions.get("buffer", "")).upper()
    representation = conditions.get("simulation_representation")
    if buffer_name == "PBS" and representation == "0.15_M_NaCl_proxy":
        if abs(float(conditions.get("salt_molar", -1)) - float(md.get("salt_molar", -2))) > 1e-9:
            failures.append(
                "experimental_conditions.salt_molar must match md.salt_molar for the PBS proxy"
            )
        if conditions.get("explicitly_model_phosphate"):
            failures.append(
                "PBS proxy cannot simultaneously request explicit phosphate; use explicit_PBS"
            )
        warnings.append(
            "PBS is represented by 0.15 M NaCl ionic strength; phosphate- and "
            "potassium-specific interactions are not modeled"
        )
    if (
        conditions.get("temperature_kelvin") is not None
        and md.get("temperature_kelvin") is not None
        and abs(
            float(conditions["temperature_kelvin"])
            - float(md["temperature_kelvin"])
        ) > 1e-9
    ):
        failures.append(
            "experimental_conditions.temperature_kelvin must match md.temperature_kelvin"
        )
    return {
        "status": "pass" if not failures else "fail",
        "failures": failures, "warnings": warnings,
        "parameter_dependent_steps_blocked": [
            "tleap_assembly", "explicit_solvation", "energy_force_validation",
            "equilibration", "production_md", "binding_affinity_analysis",
        ],
    }


def run_stage_a(config: dict[str, Any], base: Path) -> dict[str, Any]:
    output = _resolve(config["output_dir"], base)
    output.mkdir(parents=True, exist_ok=True)
    receptor_audit = audit_receptor(config, base)
    _write_json(output / "receptor" / "input_audit.json", receptor_audit)
    modeller_plan = write_modeller_plan(config, base, receptor_audit)
    candidate_manifest = build_candidate_manifest(config, base)
    protocol = validate_protocol(config)
    _write_json(output / "md_protocol_validation.json", protocol)
    missing_inputs = []
    for label, value in {
        "parameter_bundle_manifest": config.get("parameters", {}).get("bundle_manifest"),
        "binding_assay_conditions": config.get("experimental_conditions", {}).get("source"),
    }.items():
        if not value:
            missing_inputs.append(label)
    unresolved_histidines = [
        item["residue"] for item in config["receptor"].get("histidines", [])
        if str(item.get("state", "")).upper() not in AMBER_HIS
    ]
    summary = {
        "schema_version": 1, "generated_at": _utc_now(),
        "status": "code_ready_inputs_pending" if protocol["status"] == "pass" else "invalid_configuration",
        "declared_stage_status": config.get("status"),
        "receptor": {
            "input_audit": str(output / "receptor" / "input_audit.json"),
            "current_missing_residues": receptor_audit["missing_residue_numbers"],
            "modeller_plan": modeller_plan,
            "unresolved_histidines": unresolved_histidines,
        },
        "candidates": {
            "manifest": candidate_manifest["manifest"],
            "shortlist_count": candidate_manifest["shortlist_count"],
            "vina_ranked_count": candidate_manifest["vina_ranked_count"],
        },
        "protocol_validation": str(output / "md_protocol_validation.json"),
        "missing_external_inputs": missing_inputs,
        "ready_now": [
            "receptor_modeling_job", "candidate_provenance", "md_protocol_static_validation",
        ],
        "not_authorized_or_not_possible_in_stage_a": protocol["parameter_dependent_steps_blocked"],
    }
    target = output / "stage_a_status.json"
    _write_json(target, summary)
    return {"status_file": str(target), **summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=[
            "all", "audit-receptor", "plan-modeller", "finalize-receptor",
            "candidates", "validate-protocol",
        ]
    )
    parser.add_argument("--config", required=True, help="Stage-A YAML or JSON configuration")
    parser.add_argument(
        "--schema",
        help=(
            "JSON Schema path; defaults to tbmb_md_stage_a.schema.json next to the "
            "configuration"
        ),
    )
    args = parser.parse_args()
    source = Path(args.config).expanduser().resolve()
    config = _load_config(source)
    schema_path = (
        Path(args.schema).expanduser().resolve()
        if args.schema else source.with_name("tbmb_md_stage_a.schema.json")
    )
    _validate_config_schema(config, schema_path)
    base = source.parent
    if args.command == "all":
        result = run_stage_a(config, base)
    elif args.command == "audit-receptor":
        result = audit_receptor(config, base)
    elif args.command == "plan-modeller":
        result = write_modeller_plan(config, base, audit_receptor(config, base))
    elif args.command == "finalize-receptor":
        result = finalize_receptor_models(config, base)
    elif args.command == "candidates":
        result = build_candidate_manifest(config, base)
    else:
        result = validate_protocol(config)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
