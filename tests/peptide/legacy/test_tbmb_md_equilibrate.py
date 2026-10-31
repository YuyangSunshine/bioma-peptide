import json
from pathlib import Path

import pytest

from tools.our_tools.tbmb_md_equilibrate import (
    MDEquilibrateError,
    _input_records,
    _sha256,
    _steps_for_duration,
    _tail_statistics,
)


def _minimal_prmtop(path: Path, natom: int) -> None:
    path.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n"
        + f"{natom:8d}\n"
        + "%FLAG RESIDUE_LABEL\n%FORMAT(20a4)\nCYX HIE HID MSU CMU TMB\n",
        encoding="utf-8",
    )


def test_steps_for_duration():
    assert _steps_for_duration(200.0, 2.0) == 100000
    assert _steps_for_duration(0.002, 2.0) == 1
    with pytest.raises(MDEquilibrateError):
        _steps_for_duration(0.001, 2.0)


def test_tail_statistics_uses_requested_tail():
    records = [
        {
            "temperature_kelvin": value,
            "density_g_ml": 1.0 + value / 10000.0,
            "volume_nm3": 100.0 + value,
        }
        for value in (10.0, 20.0, 30.0, 40.0)
    ]
    observed = _tail_statistics(records, fraction=0.5)
    assert observed["sample_count"] == 2
    assert observed["temperature_mean_kelvin"] == pytest.approx(35.0)
    assert observed["density_mean_g_ml"] == pytest.approx(1.0035)


def test_input_records_require_passed_hash_bound_stage3_pair(tmp_path: Path):
    input_dir = tmp_path / "stage3"
    rank_dir = input_dir / "rank_01"
    rank_dir.mkdir(parents=True)
    prmtop = rank_dir / "complex_minimized.prmtop"
    inpcrd = rank_dir / "complex_minimized.inpcrd"
    _minimal_prmtop(prmtop, 2)
    inpcrd.write_text("title\n     2\n", encoding="utf-8")
    manifest = {
        "status": "passed",
        "output_prmtop_sha256": _sha256(prmtop),
        "output_inpcrd_sha256": _sha256(inpcrd),
    }
    (rank_dir / "stage3_minimization_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    summary = {
        "status": "completed",
        "candidate_count": 1,
        "candidates": [{"rank": 1, "ranked_candidate": 4, "status": "passed"}],
    }
    (input_dir / "stage3_minimization_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    summary_path, records = _input_records(
        {"input_dir": str(input_dir), "candidate_count": 1}, tmp_path
    )
    assert summary_path.name == "stage3_minimization_summary.json"
    assert len(records) == 1
    assert records[0]["ranked_candidate"] == 4
