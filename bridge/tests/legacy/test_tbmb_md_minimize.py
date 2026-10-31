import json
from pathlib import Path

import pytest
from tools.our_tools.tbmb_md_minimize import (
    _angle_degrees,
    _inpcrd_natom,
    _input_records,
    _prmtop_natom,
    _sha256,
    _write_amber_inpcrd,
)


class _FakeQuantity:
    def __init__(self, values):
        self.values = values

    def value_in_unit(self, _target):
        return self.values


class _FakeUnit:
    angstrom = object()


def _minimal_prmtop(path: Path, natom: int) -> None:
    path.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n" + f"{natom:8d}\n"
        "%FLAG BOX_DIMENSIONS\n%FORMAT(5E16.8)\n  90.0 10.0 10.0 10.0\n",
        encoding="utf-8",
    )


def test_atom_count_parsers(tmp_path: Path):
    prmtop = tmp_path / "system.prmtop"
    inpcrd = tmp_path / "system.inpcrd"
    _minimal_prmtop(prmtop, 12)
    inpcrd.write_text("title\n    12\n", encoding="utf-8")
    assert _prmtop_natom(prmtop) == 12
    assert _inpcrd_natom(inpcrd) == 12


def test_amber_inpcrd_writer_preserves_atom_count_and_box(tmp_path: Path):
    output = tmp_path / "minimized.inpcrd"
    positions = _FakeQuantity([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
    box = [
        _FakeQuantity((10.0, 0.0, 0.0)),
        _FakeQuantity((0.0, 11.0, 0.0)),
        _FakeQuantity((0.0, 0.0, 12.0)),
    ]
    _write_amber_inpcrd(output, positions, box, _FakeUnit())
    lines = output.read_text(encoding="utf-8").splitlines()
    assert int(lines[1]) == 2
    assert [float(value) for value in lines[-1].split()] == pytest.approx(
        [10.0, 11.0, 12.0, 90.0, 90.0, 90.0]
    )


def test_input_records_require_hash_bound_stage2_pair(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    rank_dir = input_dir / "rank_01"
    rank_dir.mkdir(parents=True)
    prmtop = rank_dir / "complex_solvated.prmtop"
    inpcrd = rank_dir / "complex_solvated.inpcrd"
    _minimal_prmtop(prmtop, 2)
    inpcrd.write_text("title\n     2\n", encoding="utf-8")
    manifest = {
        "solvated_prmtop_sha256": _sha256(prmtop),
        "solvated_inpcrd_sha256": _sha256(inpcrd),
    }
    (rank_dir / "stage2_solvation_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    summary = {
        "status": "completed", "candidate_count": 1,
        "candidates": [{"rank": 1, "ranked_candidate": 4}],
    }
    (input_dir / "stage2_solvation_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    summary_path, records = _input_records(
        {"input_dir": str(input_dir), "candidate_count": 1}, tmp_path
    )
    assert summary_path.name == "stage2_solvation_summary.json"
    assert len(records) == 1
    assert records[0]["ranked_candidate"] == 4


def test_angle_degrees_for_orthogonal_vectors():
    assert _angle_degrees((1.0, 0.0, 0.0), (0.0, 2.0, 0.0)) == pytest.approx(90.0)
