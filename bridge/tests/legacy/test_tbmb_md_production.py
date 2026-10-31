import csv
import json
from pathlib import Path

import pytest

from tools.our_tools.tbmb_md_production import (
    MDProductionError,
    _input_records,
    _production_tail_statistics,
    _sha256,
    _steps_for_duration,
)


def _minimal_prmtop(path: Path, natom: int) -> None:
    path.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n"
        + f"{natom:8d}\n"
        + "%FLAG RESIDUE_LABEL\n%FORMAT(20a4)\nCYX HIE HID MSU CMU TMB\n",
        encoding="utf-8",
    )


def test_steps_for_duration_ns():
    assert _steps_for_duration(20.0, 2.0) == 10_000_000
    assert _steps_for_duration(1.0, 2.0) == 500_000
    assert _steps_for_duration(0.1, 2.0) == 50_000
    with pytest.raises(MDProductionError):
        _steps_for_duration(0.000001, 3.0)


def test_tail_statistics_reads_openmm_columns(tmp_path: Path):
    paths = []
    for segment, temperatures in enumerate(((298.0, 300.0), (296.0, 298.0)), 1):
        path = tmp_path / f"segment_{segment:03d}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    '#"Step"', "Temperature (K)", "Density (g/mL)",
                    "Box Volume (nm^3)", "Potential Energy (kJ/mole)",
                ],
            )
            writer.writeheader()
            for temperature in temperatures:
                writer.writerow({
                    '#"Step"': 1,
                    "Temperature (K)": temperature,
                    "Density (g/mL)": 1.01,
                    "Box Volume (nm^3)": 250.0,
                    "Potential Energy (kJ/mole)": -1000.0,
                })
        paths.append(path)
    observed = _production_tail_statistics(paths)
    assert observed["sample_count"] == 4
    assert observed["temperature_mean_kelvin"] == pytest.approx(298.0)
    assert observed["density_mean_g_ml"] == pytest.approx(1.01)


def test_input_records_require_passed_hash_bound_stage4_pair(tmp_path: Path):
    input_dir = tmp_path / "stage4"
    rank_dir = input_dir / "rank_01"
    rank_dir.mkdir(parents=True)
    prmtop = rank_dir / "complex_equilibrated.prmtop"
    inpcrd = rank_dir / "complex_equilibrated.inpcrd"
    _minimal_prmtop(prmtop, 2)
    inpcrd.write_text("title\n     2\n", encoding="utf-8")
    manifest = {
        "status": "passed",
        "output_prmtop_sha256": _sha256(prmtop),
        "output_inpcrd_sha256": _sha256(inpcrd),
    }
    (rank_dir / "stage4_equilibration_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    summary = {
        "status": "completed",
        "candidate_count": 1,
        "candidates": [{"rank": 1, "ranked_candidate": 4, "status": "passed"}],
    }
    (input_dir / "stage4_equilibration_summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )
    summary_path, records = _input_records(
        {"input_dir": str(input_dir), "candidate_count": 1}, tmp_path
    )
    assert summary_path.name == "stage4_equilibration_summary.json"
    assert len(records) == 1
    assert records[0]["ranked_candidate"] == 4
