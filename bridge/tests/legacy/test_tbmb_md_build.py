from pathlib import Path

import pytest

from tools.our_tools.tbmb_md_build import (
    PDBAtom,
    _dry_tleap_text,
    _lcs_pairs,
    _prmtop_charge,
    _protein_sequence,
    _read_pdb_atoms,
    _tleap_failures,
    _write_pdb,
    _write_tleap_sequential_pdb,
)


def test_lcs_maps_complete_receptor_across_terminal_and_internal_gap():
    complete = "GELETSDVVTVVLGQDAKLPCFYRGDSGEQVGQVA"
    incomplete = "ELETSDVVTVVLGQDAKLPCFYRQVGQVA"
    pairs = _lcs_pairs(complete, incomplete)
    assert len(pairs) == len(incomplete) == 29
    assert pairs[0] == (1, 0)
    assert pairs[22] == (23, 22)
    assert pairs[23] == (29, 23)


def test_protein_sequence_accepts_amber_histidine_and_disulfide_names():
    atoms = [
        PDBAtom("ATOM", 1, "CA", "", "HIE", "A", 83, "", (0, 0, 0), 1, 0, "C"),
        PDBAtom("ATOM", 2, "CA", "", "HID", "A", 89, "", (1, 0, 0), 1, 0, "C"),
        PDBAtom("ATOM", 3, "CA", "", "CYX", "A", 127, "", (2, 0, 0), 1, 0, "C"),
    ]
    sequence, keys = _protein_sequence(atoms)
    assert sequence == "HHC"
    assert [key[1] for key in keys] == [83, 89, 127]


def test_tleap_failure_gate_detects_long_backbone_bond():
    log = "There is a bond of 8.282 angstroms between C and N atoms:\n"
    assert _tleap_failures(log)
    assert not _tleap_failures("There is a bond of 1.372 angstroms between C and N atoms:\n")


def test_prmtop_charge_parser(tmp_path: Path):
    path = tmp_path / "test.prmtop"
    path.write_text(
        "%FLAG POINTERS\n%FORMAT(10I8)\n       2\n"
        "%FLAG CHARGE\n%FORMAT(5E16.8)\n  0.18222300E+02 -0.18222300E+02\n"
        "%FLAG MASS\n%FORMAT(5E16.8)\n  1.0 1.0\n"
    )
    assert _prmtop_charge(path) == pytest.approx(0.0, abs=1e-8)


def test_dry_tleap_disulfide_uses_input_pdb_residue_numbers(tmp_path: Path):
    config = {
        "receptor": {"disulfides": [{"first": 52, "second": 127}]},
        "ligand": {
            "crosslinks": [
                {"peptide_residue_index": 3, "methionine_atom": "SD", "linker_residue_index": 18, "linker_atom": "CM1"},
                {"peptide_residue_index": 10, "methionine_atom": "SD", "linker_residue_index": 18, "linker_atom": "CM2"},
                {"peptide_residue_index": 17, "methionine_atom": "SD", "linker_residue_index": 18, "linker_atom": "CM3"},
            ]
        },
    }
    bundle = {"templates": [], "frcmods": []}
    receptor_atoms = [
        PDBAtom("ATOM", 1, "SG", "", "CYX", "A", 52, "", (0, 0, 0), 1, 0, "S"),
        PDBAtom("ATOM", 2, "SG", "", "CYX", "A", 127, "", (2, 0, 0), 1, 0, "S"),
    ]
    text = _dry_tleap_text(
        config, bundle, tmp_path / "receptor.pdb", tmp_path / "ligand.pdb",
        tmp_path / "dry.prmtop", tmp_path / "dry.inpcrd", tmp_path / "dry.pdb",
        receptor_atoms,
    )
    assert "bond receptor.52.SG receptor.127.SG" in text
    assert "bond receptor.21.SG receptor.96.SG" not in text


def test_tleap_combined_pdb_has_globally_sequential_residue_numbers(tmp_path: Path):
    source = tmp_path / "review_numbering.pdb"
    output = tmp_path / "tleap_numbering.pdb"
    atoms = [
        PDBAtom("ATOM", 1, "CA", "", "ALA", "A", 32, "", (0, 0, 0), 1, 0, "C"),
        PDBAtom("ATOM", 2, "CA", "", "CYX", "A", 52, "", (1, 0, 0), 1, 0, "C"),
        PDBAtom("ATOM", 3, "C", "", "ACE", "B", 1, "", (2, 0, 0), 1, 0, "C"),
        PDBAtom("HETATM", 4, "CM1", "", "TMB", "C", 18, "", (3, 0, 0), 1, 0, "C"),
    ]
    _write_pdb(source, [atoms[:2], atoms[2:3], atoms[3:]])
    audit = _write_tleap_sequential_pdb(source, output)
    renumbered = _read_pdb_atoms(output, include_hydrogen=True)
    assert [atom.residue_number for atom in renumbered] == [1, 2, 3, 4]
    assert [atom.chain for atom in renumbered] == ["A", "A", "B", "C"]
    assert audit["residue_count"] == 4
    assert audit["residue_mapping"][1]["source_residue_number"] == 52
