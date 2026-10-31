"""Cyclic-peptide and protein-peptide structure prediction."""

from __future__ import annotations

from bioma.peptide.contracts import result_validator as peptide_result_validator
import json
import csv
import hashlib
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError
from tools.our_tools._crosslinked_impl.ensemble_contract import (
    contract_hash,
    internal_pool,
    require_final_target,
)

# ---------------------------------------------------------------------------
# Generic crosslinked-peptide topology and conformer-selection tools
# ---------------------------------------------------------------------------

_CP_STABLE_ID = "_BioMAStableAtomId"
_CP_HARTREE_TO_KCAL_MOL = 627.509474


class CrosslinkedPeptideTopologyInput(ToolInput):
    chemical_spec_json: str = Field(
        description=(
            "JSON chemical specification containing the peptide, terminal caps, "
            "components, bonds, charge states, pH, and experimental conditions."
        )
    )
    output_dir: str = Field(description="Directory for topology artifacts.")


class ConformerSelectionInput(ToolInput):
    input_manifest_path: str = Field(description="Manifest from RDKit or xTB.")
    additional_input_manifest_paths_json: str = Field(
        default="[]",
        description="JSON list of additional manifests to merge before validation and selection.",
    )
    prior_selection_manifest_path: str | None = Field(
        default=None,
        description=(
            "Previous select_for_normal manifest whose already selected candidate IDs "
            "must be carried forward and excluded from an expansion batch."
        ),
    )
    purpose: Literal["select_for_normal", "select_for_tight", "select_for_final"]
    target_ensemble_size: int = Field(ge=1, le=100)
    output_dir: str = Field(description="Directory for validation and selection artifacts.")
    selection_options_json: str = Field(
        default="{}",
        description="Optional JSON overrides for RMSD and geometry thresholds.",
    )


def _cp_json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"{label} is not valid JSON: {exc}",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        ) from exc
    if not isinstance(value, dict):
        raise ToolError(
            f"{label} must be a JSON object.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    return value


def _cp_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cp_ensemble_result_hash(molecules: list[Any]) -> str:
    """Hash molecular identities and coordinates without artifact paths."""
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
    return _cp_hash(records)


def _cp_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _cp_load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _cp_rdkit() -> tuple[Any, Any, Any, Any]:
    try:
        import numpy as np
        from rdkit import Chem, rdBase
        from rdkit.Chem import AllChem
    except ImportError as exc:
        raise ToolError(
            "RDKit and NumPy are required for crosslinked-peptide structure tools.",
            error_type="unavailable",
            retriable=False,
            backend="RDKit",
        ) from exc
    return Chem, AllChem, rdBase, np


def _cp_cycle_rank(mol: Any) -> int:
    Chem, *_ = _cp_rdkit()
    return int(mol.GetNumBonds() - mol.GetNumAtoms() + len(Chem.GetMolFrags(mol)))


def _cp_pdb_info(
    atom: Any,
    atom_name: str,
    residue_name: str,
    residue_number: int,
    chain_id: str,
    hetero: bool,
) -> None:
    Chem, *_ = _cp_rdkit()
    info = Chem.AtomPDBResidueInfo()
    info.SetName(f"{atom_name:>4}"[-4:])
    info.SetResidueName(f"{residue_name:>3}"[-3:])
    info.SetResidueNumber(int(residue_number))
    info.SetChainId((chain_id or "A")[:1])
    info.SetIsHeteroAtom(bool(hetero))
    atom.SetMonomerInfo(info)


def _cp_atom_by_id(mol: Any, stable_id: str) -> Any:
    matches = [
        atom
        for atom in mol.GetAtoms()
        if atom.HasProp(_CP_STABLE_ID)
        and atom.GetProp(_CP_STABLE_ID) == stable_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Stable atom identifier {stable_id!r} matched {len(matches)} atoms."
        )
    return matches[0]


def _cp_label_canonical_peptide(mol: Any) -> None:
    seen: set[str] = set()
    for atom in mol.GetAtoms():
        info = atom.GetPDBResidueInfo()
        if info is None:
            raise ValueError("RDKit peptide atoms require PDB residue metadata.")
        stable_id = f"PEP:{info.GetResidueNumber()}:{info.GetName().strip()}"
        if stable_id in seen:
            raise ValueError(f"Duplicate peptide atom identifier {stable_id}.")
        seen.add(stable_id)
        atom.SetProp(_CP_STABLE_ID, stable_id)
        atom.SetProp("bioma_component", "peptide")


def _cp_label_supplied_peptide(mol: Any, labels: list[dict[str, Any]]) -> None:
    if not labels:
        _cp_label_canonical_peptide(mol)
        return
    seen: set[str] = set()
    for label in labels:
        index = int(label["atom_index"])
        if index < 0 or index >= mol.GetNumAtoms():
            raise ValueError(f"Peptide atom index {index} is outside the supplied structure.")
        atom = mol.GetAtomWithIdx(index)
        stable_id = str(
            label.get("stable_id")
            or f"PEP:{int(label['residue'])}:{str(label['atom_name']).upper()}"
        )
        if stable_id in seen:
            raise ValueError(f"Duplicate peptide atom identifier {stable_id}.")
        seen.add(stable_id)
        atom.SetProp(_CP_STABLE_ID, stable_id)
        atom.SetProp("bioma_component", "peptide")
        _cp_pdb_info(
            atom,
            str(label.get("atom_name", stable_id.rsplit(":", 1)[-1])),
            str(label.get("residue_name", "UNL")),
            int(label.get("residue", 1)),
            str(label.get("chain_id", "A")),
            bool(label.get("hetero", False)),
        )


def _cp_apply_residue_stereochemistry(mol: Any, specification: Any) -> None:
    Chem, *_ = _cp_rdkit()
    if specification in (None, "standard_L", "L", "l"):
        return
    if isinstance(specification, dict):
        overrides = specification.get("overrides", [])
    elif isinstance(specification, list):
        overrides = specification
    else:
        raise ValueError("residue_stereochemistry must be standard_L, a list, or an object.")
    for override in overrides:
        residue = int(override["residue"])
        configuration = str(override["configuration"]).upper()
        atom = _cp_atom_by_id(mol, f"PEP:{residue}:CA")
        if configuration == "D":
            atom.InvertChirality()
        elif configuration in {"UNSPECIFIED", "UNASSIGNED"}:
            atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
        elif configuration != "L":
            raise ValueError(f"Unsupported residue configuration {configuration!r}.")


def _cp_add_atom(
    editable: Any,
    symbol: str,
    stable_id: str,
    atom_name: str,
    residue_name: str,
    residue_number: int,
) -> int:
    Chem, *_ = _cp_rdkit()
    index = int(editable.AddAtom(Chem.Atom(symbol)))
    atom = editable.GetAtomWithIdx(index)
    atom.SetProp(_CP_STABLE_ID, stable_id)
    atom.SetProp("bioma_component", "terminal_cap")
    _cp_pdb_info(atom, atom_name, residue_name, residue_number, "A", True)
    return index


def _cp_apply_terminal_caps(mol: Any, sequence_length: int, spec: dict[str, Any]) -> Any:
    Chem, *_ = _cp_rdkit()
    editable = Chem.RWMol(mol)
    n_cap = str(spec.get("n_terminus", "free")).lower()
    c_cap = str(spec.get("c_terminus", "free_acid")).lower()

    if c_cap in {"primary_amide", "amide", "nh2"}:
        oxygen = _cp_atom_by_id(editable, f"PEP:{sequence_length}:OXT")
        editable.RemoveAtom(oxygen.GetIdx())
        carbon = _cp_atom_by_id(editable, f"PEP:{sequence_length}:C")
        nitrogen_index = _cp_add_atom(
            editable,
            "N",
            "CAP:C:N",
            "NT",
            "AMD",
            sequence_length + 1,
        )
        editable.AddBond(carbon.GetIdx(), nitrogen_index, Chem.BondType.SINGLE)
    elif c_cap not in {"free", "free_acid", "carboxyl", "predefined"}:
        raise ValueError(f"Unsupported C-terminal cap {c_cap!r}.")

    if n_cap in {"acetylated", "acetyl", "ace"}:
        nitrogen = _cp_atom_by_id(editable, "PEP:1:N")
        carbonyl = _cp_add_atom(editable, "C", "CAP:N:C", "C", "ACE", 0)
        oxygen = _cp_add_atom(editable, "O", "CAP:N:O", "O", "ACE", 0)
        methyl = _cp_add_atom(editable, "C", "CAP:N:CH3", "CH3", "ACE", 0)
        editable.AddBond(nitrogen.GetIdx(), carbonyl, Chem.BondType.SINGLE)
        editable.AddBond(carbonyl, oxygen, Chem.BondType.DOUBLE)
        editable.AddBond(carbonyl, methyl, Chem.BondType.SINGLE)
    elif n_cap not in {"free", "none", "predefined"}:
        raise ValueError(f"Unsupported N-terminal cap {n_cap!r}.")

    result = editable.GetMol()
    result.UpdatePropertyCache(strict=False)
    Chem.SanitizeMol(result)
    return result


def _cp_component_atom_id(component_id: str, atom: Any) -> str:
    atom_map = int(atom.GetAtomMapNum())
    if atom_map > 0:
        return f"COMP:{component_id}:MAP:{atom_map}"
    return f"COMP:{component_id}:IDX:{atom.GetIdx() + 1}"


def _cp_prepare_component(component: dict[str, Any]) -> tuple[Any, dict[int, str]]:
    Chem, *_ = _cp_rdkit()
    component_id = str(component.get("component_id") or component.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", component_id):
        raise ValueError("Each component_id must use letters, digits, underscore, or hyphen.")
    smiles = str(component.get("smiles", "")).strip()
    mol = Chem.MolFromSmiles(smiles, sanitize=True)
    if mol is None:
        raise ValueError(f"RDKit could not parse component {component_id!r}.")
    residue_name = str(component.get("residue_name", component_id[:3] or "LNK"))[:3]
    stable_ids: dict[int, str] = {}
    element_counts: dict[str, int] = {}
    for atom in mol.GetAtoms():
        stable_id = _cp_component_atom_id(component_id, atom)
        if stable_id in stable_ids.values():
            raise ValueError(f"Component {component_id!r} has duplicate atom-map values.")
        stable_ids[atom.GetIdx()] = stable_id
        atom.SetProp(_CP_STABLE_ID, stable_id)
        atom.SetProp("bioma_component", component_id)
        element = atom.GetSymbol().upper()
        element_counts[element] = element_counts.get(element, 0) + 1
        atom_name = f"{element[:2]}{element_counts[element]}"[:4]
        _cp_pdb_info(atom, atom_name, residue_name, 1, component_id[:1] or "X", True)
    return mol, stable_ids


def _cp_reactive_handle(
    mol: Any,
    component_id: str,
    ordinal: int,
    leaving_element: str,
) -> tuple[str, str]:
    Chem, *_ = _cp_rdkit()
    handles: list[tuple[int, str, str]] = []
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=True))
    for atom in mol.GetAtoms():
        if atom.GetSymbol().lower() != leaving_element.lower():
            continue
        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 1:
            continue
        leaving_id = atom.GetProp(_CP_STABLE_ID)
        bonded_id = neighbors[0].GetProp(_CP_STABLE_ID)
        handles.append((int(ranks[neighbors[0].GetIdx()]), bonded_id, leaving_id))
    handles.sort(key=lambda item: (item[0], item[1]))
    if ordinal < 1 or ordinal > len(handles):
        raise ValueError(
            f"Component {component_id!r} has {len(handles)} {leaving_element} handles; "
            f"requested handle {ordinal}."
        )
    _, bonded, leaving = handles[ordinal - 1]
    return bonded, leaving


def _cp_resolve_charge_atom(mol: Any, state: dict[str, Any]) -> Any:
    if state.get("atom"):
        return _cp_atom_by_id(mol, str(state["atom"]))
    residue = int(state.get("residue", 0))
    atom_name = str(state.get("atom_name", "")).strip().upper()
    group = str(state.get("group", "")).strip().lower()
    if group == "arginine_sidechain":
        Chem, *_ = _cp_rdkit()
        for name in ("NH1", "NH2"):
            atom = _cp_atom_by_id(mol, f"PEP:{residue}:{name}")
            if any(
                mol.GetBondBetweenAtoms(atom.GetIdx(), neighbor.GetIdx()).GetBondType()
                == Chem.BondType.DOUBLE
                for neighbor in atom.GetNeighbors()
            ):
                return atom
        raise ValueError(f"Arginine sidechain at residue {residue} has no imine nitrogen.")
    if residue and atom_name:
        return _cp_atom_by_id(mol, f"PEP:{residue}:{atom_name}")
    raise ValueError("Each charge state needs atom, or residue plus atom_name/group.")


def _cp_apply_charge_states(mol: Any, states: list[dict[str, Any]]) -> None:
    for state in states:
        atom = _cp_resolve_charge_atom(mol, state)
        atom.SetFormalCharge(int(state["formal_charge"]))
        if "explicit_hydrogens" in state:
            atom.SetNumExplicitHs(int(state["explicit_hydrogens"]))
            atom.SetNoImplicit(True)
        elif "hydrogen_delta" in state:
            total_h = int(atom.GetTotalNumHs(includeNeighbors=True))
            explicit_neighbor_h = sum(1 for item in atom.GetNeighbors() if item.GetAtomicNum() == 1)
            target = total_h + int(state["hydrogen_delta"]) - explicit_neighbor_h
            if target < 0:
                raise ValueError(f"Charge state creates a negative hydrogen count for atom {atom.GetIdx()}.")
            atom.SetNumExplicitHs(target)
            atom.SetNoImplicit(True)


def _cp_create_atom_property_list(Chem: Any, mol: Any) -> None:
    creator = getattr(Chem, "CreateAtomStringPropertyList", None)
    if creator is not None:
        creator(mol, _CP_STABLE_ID)


@bioma_tool(
    name="build_crosslinked_peptide_topology",
    description=(
        "Build a capped, charged peptide-plus-component covalent topology and write "
        "validated SDF, atom-map, chemical-specification, and manifest artifacts."
    ),
    category="analysis",
    version="1.1.0",
    author="BioMA",
    args_schema=CrosslinkedPeptideTopologyInput,
    tags=["peptide", "crosslink", "topology", "rdkit", "protonation"],
 result_kind="computed_prediction", result_validator=peptide_result_validator)
def build_crosslinked_peptide_topology(
    chemical_spec_json: str,
    output_dir: str,
) -> str:
    Chem, _, rd_base, _ = _cp_rdkit()
    spec = _cp_json_object(chemical_spec_json, "chemical_spec_json")
    if spec.get("pH") is None:
        raise ToolError(
            "chemical_spec_json requires the experimental pH.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    try:
        p_h = float(spec["pH"])
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "chemical_spec_json.pH must be numeric.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        ) from exc
    if not math.isfinite(p_h):
        raise ToolError(
            "chemical_spec_json.pH must be finite.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    if not isinstance(spec.get("experimental_conditions"), dict):
        raise ToolError(
            "chemical_spec_json requires experimental_conditions as an object.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    sequence = re.sub(r"\s+", "", str(spec.get("sequence", ""))).upper()
    supplied_peptide_path = str(spec.get("peptide_sdf_path", "")).strip()
    if not sequence or not re.fullmatch(r"[A-Z]+", sequence):
        raise ToolError(
            "sequence must contain amino-acid one-letter codes.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        if supplied_peptide_path:
            supplied = Chem.SDMolSupplier(
                str(Path(supplied_peptide_path).expanduser().resolve()),
                removeHs=False,
                sanitize=True,
            )
            peptide = next((item for item in supplied if item is not None), None)
            if peptide is None:
                raise ValueError("RDKit could not read peptide_sdf_path.")
            labels = spec.get("peptide_atom_labels", [])
            if not isinstance(labels, list):
                raise ValueError("peptide_atom_labels must be a list.")
            _cp_label_supplied_peptide(peptide, labels)
        else:
            if not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", sequence):
                raise ValueError(
                    "A sequence containing noncanonical residue codes requires peptide_sdf_path and peptide_atom_labels."
                )
            peptide = Chem.MolFromSequence(sequence, sanitize=True)
            if peptide is None:
                raise ValueError("RDKit could not create the peptide sequence.")
            _cp_label_canonical_peptide(peptide)
        _cp_apply_residue_stereochemistry(
            peptide, spec.get("residue_stereochemistry", "standard_L")
        )
        peptide = _cp_apply_terminal_caps(peptide, len(sequence), spec)

        component_molecules: dict[str, Any] = {}
        combined = peptide
        for component in spec.get("components", []):
            component_mol, _ = _cp_prepare_component(component)
            component_id = str(component.get("component_id") or component.get("id"))
            component_molecules[component_id] = component_mol
            combined = Chem.CombineMols(combined, component_mol)

        editable = Chem.RWMol(combined)
        planned_bonds: list[dict[str, Any]] = []
        leaving_ids: set[str] = set()
        connections = spec.get("connections", spec.get("bond_edits", []))
        if not isinstance(connections, list):
            raise ValueError("connections must be a JSON list.")
        for position, connection in enumerate(connections, start=1):
            if not isinstance(connection, dict):
                raise ValueError("Each connection must be a JSON object.")
            atom1 = str(connection.get("atom1", "")).strip()
            atom2 = str(connection.get("atom2", "")).strip()
            leaving: list[str] = [str(item) for item in connection.get("remove_atoms", [])]
            if not atom1:
                atom1 = f"PEP:{int(connection['peptide_residue'])}:{str(connection['peptide_atom']).upper()}"
            if not atom2:
                component_id = str(connection["component_id"])
                if connection.get("component_atom_map") is not None:
                    atom2 = f"COMP:{component_id}:MAP:{int(connection['component_atom_map'])}"
                elif connection.get("component_atom_index") is not None:
                    atom2 = f"COMP:{component_id}:IDX:{int(connection['component_atom_index'])}"
                else:
                    ordinal = int(connection.get("reactive_group_index", position))
                    leaving_element = str(connection.get("leaving_element", "Br"))
                    atom2, detected_leaving = _cp_reactive_handle(
                        component_molecules[component_id],
                        component_id,
                        ordinal,
                        leaving_element,
                    )
                    leaving.append(detected_leaving)
            _cp_atom_by_id(editable, atom1)
            _cp_atom_by_id(editable, atom2)
            for stable_id in leaving:
                _cp_atom_by_id(editable, stable_id)
                leaving_ids.add(stable_id)
            planned_bonds.append(
                {
                    "connection_id": str(connection.get("connection_id", f"bond_{position}")),
                    "atom1": atom1,
                    "atom2": atom2,
                    "bond_order": float(connection.get("bond_order", 1)),
                    "removed_atoms": leaving,
                    "stereochemistry": str(connection.get("stereochemistry", "unspecified")),
                    "stereocenter_atoms": connection.get("stereocenter_atoms"),
                }
            )

        leaving_indices = [_cp_atom_by_id(editable, value).GetIdx() for value in leaving_ids]
        for atom_index in sorted(leaving_indices, reverse=True):
            editable.RemoveAtom(atom_index)
        deprotected = editable.GetMol()
        deprotected.UpdatePropertyCache(strict=False)
        Chem.SanitizeMol(deprotected)
        cycle_rank_before = _cp_cycle_rank(deprotected)

        editable = Chem.RWMol(deprotected)
        bond_types = {
            1.0: Chem.BondType.SINGLE,
            1.5: Chem.BondType.AROMATIC,
            2.0: Chem.BondType.DOUBLE,
            3.0: Chem.BondType.TRIPLE,
        }
        for connection in planned_bonds:
            first = _cp_atom_by_id(editable, connection["atom1"])
            second = _cp_atom_by_id(editable, connection["atom2"])
            order = connection["bond_order"]
            if order not in bond_types:
                raise ValueError(f"Unsupported bond order {order}.")
            if editable.GetBondBetweenAtoms(first.GetIdx(), second.GetIdx()) is None:
                editable.AddBond(first.GetIdx(), second.GetIdx(), bond_types[order])
            if connection["stereochemistry"].lower() in {"unspecified", "unassigned"}:
                targets = connection.get("stereocenter_atoms")
                if targets is None:
                    targets = [connection["atom1"]] if first.GetSymbol() in {"S", "P"} else []
                for target in targets:
                    stable_id = (
                        connection["atom1"]
                        if target == "atom1"
                        else connection["atom2"]
                        if target == "atom2"
                        else str(target)
                    )
                    _cp_atom_by_id(editable, stable_id).SetChiralTag(
                        Chem.ChiralType.CHI_UNSPECIFIED
                    )

        product = editable.GetMol()
        product.UpdatePropertyCache(strict=False)
        charge_states = spec.get("charge_states", spec.get("site_charges", []))
        if not isinstance(charge_states, list):
            raise ValueError("charge_states must be a JSON list.")
        _cp_apply_charge_states(product, charge_states)
        product.UpdatePropertyCache(strict=False)
        Chem.SanitizeMol(product)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ToolError(
            str(exc),
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        ) from exc

    cycle_rank_after = _cp_cycle_rank(product)
    total_charge = int(sum(atom.GetFormalCharge() for atom in product.GetAtoms()))
    expected_charge = int(spec.get("total_charge", total_charge))
    expected_components = int(spec.get("expected_components", 1))
    checks: dict[str, bool] = {
        "sanitized": True,
        "expected_component_count": len(Chem.GetMolFrags(product)) == expected_components,
        "total_formal_charge_matches": total_charge == expected_charge,
        "leaving_atoms_removed": all(
            not any(
                atom.HasProp(_CP_STABLE_ID)
                and atom.GetProp(_CP_STABLE_ID) == stable_id
                for atom in product.GetAtoms()
            )
            for stable_id in leaving_ids
        ),
    }
    for connection in planned_bonds:
        first = _cp_atom_by_id(product, connection["atom1"])
        second = _cp_atom_by_id(product, connection["atom2"])
        bond = product.GetBondBetweenAtoms(first.GetIdx(), second.GetIdx())
        checks[f"connection_{connection['connection_id']}"] = bool(
            bond is not None
            and math.isclose(float(bond.GetBondTypeAsDouble()), connection["bond_order"])
        )
    cycle_delta = cycle_rank_after - cycle_rank_before
    if spec.get("expected_cycle_rank_delta") is not None:
        checks["expected_cycle_rank_delta"] = cycle_delta == int(
            spec["expected_cycle_rank_delta"]
        )

    atom_records: list[dict[str, Any]] = []
    connection_atoms = {
        value for connection in planned_bonds for value in (connection["atom1"], connection["atom2"])
    }
    for atom in product.GetAtoms():
        stable_id = (
            atom.GetProp(_CP_STABLE_ID)
            if atom.HasProp(_CP_STABLE_ID)
            else f"ATOM:{atom.GetIdx() + 1}"
        )
        info = atom.GetPDBResidueInfo()
        fields = stable_id.split(":")
        peptide_core = (
            len(fields) == 3 and fields[0] == "PEP" and fields[2] in {"N", "CA", "C", "O"}
        )
        core = atom.GetAtomicNum() > 1 and (
            peptide_core
            or stable_id.startswith("COMP:")
            or stable_id.startswith("CAP:")
            or stable_id in connection_atoms
        )
        atom_records.append(
            {
                "atom_index": atom.GetIdx(),
                "stable_id": stable_id,
                "element": atom.GetSymbol(),
                "formal_charge": atom.GetFormalCharge(),
                "component": atom.GetProp("bioma_component") if atom.HasProp("bioma_component") else "",
                "residue_number": info.GetResidueNumber() if info else None,
                "residue_name": info.GetResidueName().strip() if info else "",
                "atom_name": info.GetName().strip() if info else "",
                "core_heavy_atom": core,
            }
        )

    chemical_hash = _cp_hash(spec)
    product.SetProp("bioma_chemical_hash", chemical_hash)
    product.SetProp("bioma_total_charge", str(total_charge))
    product.SetProp("bioma_uhf", str(int(spec.get("uhf", 0))))
    product.SetProp("bioma_connections_json", json.dumps(planned_bonds, sort_keys=True))
    _cp_create_atom_property_list(Chem, product)

    topology_path = destination / "topology.sdf"
    writer = Chem.SDWriter(str(topology_path))
    writer.write(product)
    writer.close()
    spec_path = destination / "chemical_spec.json"
    atom_map_path = destination / "atom_map.json"
    validation_path = destination / "topology_validation.json"
    manifest_path = destination / "topology_manifest.json"
    _cp_write_json(spec_path, spec)
    _cp_write_json(atom_map_path, {"atoms": atom_records})
    validation = {
        "passed": all(checks.values()),
        "checks": checks,
        "details": {
            "sequence_length": len(sequence),
            "atom_count": product.GetNumAtoms(),
            "bond_count": product.GetNumBonds(),
            "total_formal_charge": total_charge,
            "uhf": int(spec.get("uhf", 0)),
            "cycle_rank_before_connections": cycle_rank_before,
            "cycle_rank_after_connections": cycle_rank_after,
            "cycle_rank_delta": cycle_delta,
            "connections": planned_bonds,
        },
    }
    _cp_write_json(validation_path, validation)
    status = "ok" if validation["passed"] else "failed_topology"
    config_hash = _cp_hash(
        {"chemical_hash": chemical_hash, "rdkit": rd_base.rdkitVersion}
    )
    result_hash = _cp_hash(
        {
            "status": status,
            "chemical_hash": chemical_hash,
            "atom_records": atom_records,
            "connections": planned_bonds,
            "validation": validation,
        }
    )
    manifest = {
        "contract_version": "1.0",
        "tool_name": "build_crosslinked_peptide_topology",
        "tool_version": "1.1.0",
        "stage": "topology",
        "status": status,
        "chemical_hash": chemical_hash,
        "input_hash": chemical_hash,
        "config_hash": config_hash,
        "result_hash": result_hash,
        "parent_manifest": None,
        "parent_manifests": [],
        "topology_manifest": str(manifest_path),
        "sequence": sequence,
        "sequence_length": len(sequence),
        "target_total_charge": expected_charge,
        "total_charge": total_charge,
        "uhf": int(spec.get("uhf", 0)),
        "pH": spec.get("pH"),
        "experimental_conditions": spec.get("experimental_conditions", {}),
        "connections": planned_bonds,
        "candidate_count": 1,
        "candidate_ids": ["topology"],
        "artifacts": {
            "topology_sdf": str(topology_path),
            "chemical_spec_json": str(spec_path),
            "atom_map_json": str(atom_map_path),
            "topology_validation_json": str(validation_path),
        },
        "warnings": [],
        "recommended_action": None if validation["passed"] else "revise_chemical_specification",
        "implementation": {"rdkit_version": rd_base.rdkitVersion},
    }
    _cp_write_json(manifest_path, manifest)
    return json.dumps(
        {
            "status": status,
            "manifest_path": str(manifest_path),
            "topology_sdf": str(topology_path),
            "validation_path": str(validation_path),
            "chemical_hash": chemical_hash,
            "config_hash": config_hash,
            "result_hash": result_hash,
            "total_charge": total_charge,
            "cycle_rank_delta": cycle_delta,
        },
        ensure_ascii=False,
        indent=2,
    )


def _cp_apply_atom_map(mol: Any, atom_map_path: Path) -> list[int]:
    mapping = _cp_load_json(atom_map_path).get("atoms", [])
    core: list[int] = []
    for record in mapping:
        index = int(record["atom_index"])
        if index >= mol.GetNumAtoms():
            raise ValueError("Atom map is incompatible with the conformer atom order.")
        atom = mol.GetAtomWithIdx(index)
        if atom.GetSymbol() != record["element"]:
            raise ValueError("Atom-map element order differs from the conformer.")
        atom.SetProp(_CP_STABLE_ID, str(record["stable_id"]))
        if record.get("core_heavy_atom"):
            core.append(index)
    return core


def _cp_read_sdf(path: Path) -> list[Any]:
    Chem, *_ = _cp_rdkit()
    supplier = Chem.SDMolSupplier(str(path), removeHs=False, sanitize=True)
    molecules = [mol for mol in supplier if mol is not None]
    if not molecules:
        raise ValueError(f"No readable molecules were found in {path}.")
    return molecules


def _cp_single_conf(mol: Any) -> Any:
    Chem, *_ = _cp_rdkit()
    copied = Chem.Mol(mol)
    if copied.GetNumConformers() != 1:
        raise ValueError("Each SDF record must contain exactly one conformer.")
    return copied


def _cp_connectivity_hash(mol: Any) -> str:
    Chem, *_ = _cp_rdkit()
    heavy = Chem.RemoveHs(Chem.Mol(mol), sanitize=True)
    Chem.RemoveStereochemistry(heavy)
    return _cp_hash(Chem.MolToSmiles(heavy, canonical=True, isomericSmiles=True))


def _cp_kabsch_rmsd(first: Any, second: Any, atom_indices: list[int]) -> float:
    *_, np = _cp_rdkit()
    p = np.asarray(first.GetConformer().GetPositions(), dtype=float)[atom_indices]
    q = np.asarray(second.GetConformer().GetPositions(), dtype=float)[atom_indices]
    p = p - p.mean(axis=0)
    q = q - q.mean(axis=0)
    covariance = p.T @ q
    left, _, right_t = np.linalg.svd(covariance)
    rotation = left @ right_t
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right_t
    delta = p @ rotation - q
    return float(np.sqrt(np.mean(np.sum(delta * delta, axis=1))))


def _cp_expected_cip(mol: Any) -> dict[str, str]:
    Chem, *_ = _cp_rdkit()
    probe = Chem.Mol(mol)
    Chem.AssignStereochemistry(probe, cleanIt=True, force=True)
    result: dict[str, str] = {}
    for atom in probe.GetAtoms():
        if atom.HasProp(_CP_STABLE_ID) and atom.HasProp("_CIPCode"):
            stable_id = atom.GetProp(_CP_STABLE_ID)
            if stable_id.startswith("PEP:") and stable_id.endswith(":CA"):
                result[stable_id] = atom.GetProp("_CIPCode")
    return result


def _cp_observed_cip(mol: Any) -> dict[str, str]:
    Chem, *_ = _cp_rdkit()
    probe = Chem.Mol(mol)
    Chem.RemoveStereochemistry(probe)
    Chem.AssignAtomChiralTagsFromStructure(probe, confId=0, replaceExistingTags=True)
    Chem.AssignStereochemistry(probe, cleanIt=True, force=True)
    result: dict[str, str] = {}
    for atom in probe.GetAtoms():
        if atom.HasProp(_CP_STABLE_ID) and atom.HasProp("_CIPCode"):
            stable_id = atom.GetProp(_CP_STABLE_ID)
            if stable_id.startswith("PEP:") and stable_id.endswith(":CA"):
                result[stable_id] = atom.GetProp("_CIPCode")
    return result


def _cp_sulfur_signatures(mol: Any) -> dict[str, str]:
    *_, np = _cp_rdkit()
    conf = mol.GetConformer()
    signatures: dict[str, str] = {}
    for atom in mol.GetAtoms():
        if atom.GetSymbol() != "S" or atom.GetDegree() != 3:
            continue
        point = conf.GetAtomPosition(atom.GetIdx())
        center = np.asarray([point.x, point.y, point.z], dtype=float)
        neighbors = sorted(atom.GetNeighbors(), key=lambda item: item.GetIdx())
        vectors = []
        for neighbor in neighbors:
            point = conf.GetAtomPosition(neighbor.GetIdx())
            vectors.append(np.asarray([point.x, point.y, point.z]) - center)
        volume = float(np.dot(vectors[0], np.cross(vectors[1], vectors[2])))
        stable_id = atom.GetProp(_CP_STABLE_ID) if atom.HasProp(_CP_STABLE_ID) else str(atom.GetIdx())
        signatures[stable_id] = (
            "planar_or_undefined" if abs(volume) < 1.0e-4 else "positive" if volume > 0 else "negative"
        )
    return signatures


def _cp_geometry_report(
    mol: Any,
    reference_connectivity_hash: str,
    expected_cip: dict[str, str],
    strict: bool,
) -> dict[str, Any]:
    Chem, _, _, np = _cp_rdkit()
    coordinates = np.asarray(mol.GetConformer().GetPositions(), dtype=float)
    periodic = Chem.GetPeriodicTable()
    bond_low = 0.60 if strict else 0.50
    bond_high = 1.55 if strict else 1.75
    unusual_bonds: list[dict[str, Any]] = []
    conf = mol.GetConformer()
    for bond in mol.GetBonds():
        first = bond.GetBeginAtom()
        second = bond.GetEndAtom()
        distance = float(conf.GetAtomPosition(first.GetIdx()).Distance(conf.GetAtomPosition(second.GetIdx())))
        expected = float(periodic.GetRcovalent(first.GetAtomicNum()) + periodic.GetRcovalent(second.GetAtomicNum()))
        ratio = distance / expected if expected > 0 else float("inf")
        if ratio < bond_low or ratio > bond_high:
            unusual_bonds.append(
                {
                    "atom1": first.GetIdx(),
                    "atom1_stable_id": (
                        first.GetProp(_CP_STABLE_ID)
                        if first.HasProp(_CP_STABLE_ID)
                        else None
                    ),
                    "atom1_element": first.GetSymbol(),
                    "atom2": second.GetIdx(),
                    "atom2_stable_id": (
                        second.GetProp(_CP_STABLE_ID)
                        if second.HasProp(_CP_STABLE_ID)
                        else None
                    ),
                    "atom2_element": second.GetSymbol(),
                    "distance": distance,
                    "expected_covalent_distance": expected,
                    "ratio": ratio,
                    "allowed_ratio": [bond_low, bond_high],
                }
            )

    topological = Chem.GetDistanceMatrix(mol)
    clashes: list[dict[str, Any]] = []
    heavy_indices = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1]
    clash_scale = 0.60 if strict else 0.45
    for offset, first_index in enumerate(heavy_indices):
        first = mol.GetAtomWithIdx(first_index)
        for second_index in heavy_indices[offset + 1 :]:
            if topological[first_index, second_index] <= 2:
                continue
            second = mol.GetAtomWithIdx(second_index)
            distance = float(conf.GetAtomPosition(first_index).Distance(conf.GetAtomPosition(second_index)))
            limit = clash_scale * float(
                periodic.GetRvdw(first.GetAtomicNum()) + periodic.GetRvdw(second.GetAtomicNum())
            )
            if distance < limit:
                clashes.append(
                    {
                        "atom1": first_index,
                        "atom1_stable_id": (
                            first.GetProp(_CP_STABLE_ID)
                            if first.HasProp(_CP_STABLE_ID)
                            else None
                        ),
                        "atom1_element": first.GetSymbol(),
                        "atom2": second_index,
                        "atom2_stable_id": (
                            second.GetProp(_CP_STABLE_ID)
                            if second.HasProp(_CP_STABLE_ID)
                            else None
                        ),
                        "atom2_element": second.GetSymbol(),
                        "distance": distance,
                        "limit": limit,
                    }
                )

    observed_cip = _cp_observed_cip(mol)
    chirality_mismatches = {
        stable_id: {"expected": expected, "observed": observed_cip.get(stable_id)}
        for stable_id, expected in expected_cip.items()
        if observed_cip.get(stable_id) != expected
    }
    checks = {
        "finite_coordinates": bool(np.isfinite(coordinates).all()),
        "topology_preserved": (
            _cp_connectivity_hash(mol) == reference_connectivity_hash
        ),
        "bond_geometry_valid": len(unusual_bonds) == 0,
        "no_severe_heavy_atom_clashes": len(clashes) == 0,
        "peptide_chirality_preserved": len(chirality_mismatches) == 0,
    }
    score = float(len(unusual_bonds) * 10 + len(clashes) * 5 + len(chirality_mismatches) * 20)
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "unusual_bonds": unusual_bonds,
        "severe_clashes": clashes,
        "chirality_mismatches": chirality_mismatches,
        "sulfur_orientation_signatures": _cp_sulfur_signatures(mol),
        "geometry_score": score,
    }


def _cp_write_molecules(Chem: Any, path: Path, molecules: list[Any]) -> None:
    writer = Chem.SDWriter(str(path))
    for mol in molecules:
        _cp_create_atom_property_list(Chem, mol)
        writer.write(mol)
    writer.close()


def _cp_rmsd_matrix(molecules: list[Any], core: list[int]) -> list[list[float]]:
    matrix = [[0.0 for _ in molecules] for _ in molecules]
    for first in range(len(molecules)):
        for second in range(first):
            value = _cp_kabsch_rmsd(molecules[first], molecules[second], core)
            matrix[first][second] = value
            matrix[second][first] = value
    return matrix


def _cp_diversity_order(indices: list[int], matrix: list[list[float]], start: int) -> list[int]:
    if not indices:
        return []
    selected = [start]
    remaining = set(indices) - {start}
    while remaining:
        chosen = max(
            remaining,
            key=lambda index: (min(matrix[index][item] for item in selected), -index),
        )
        selected.append(chosen)
        remaining.remove(chosen)
    return selected


def _cp_validation_failure_summary(
    validation_records: list[dict[str, Any]],
    valid_candidate_count: int,
) -> tuple[dict[str, Any], bool]:
    failure_signatures = [
        tuple(record["failed_checks"])
        for record in validation_records
        if not record["passed"]
    ]
    uniform_single_failure = (
        bool(validation_records)
        and len(failure_signatures) == len(validation_records)
        and len(set(failure_signatures)) == 1
        and len(failure_signatures[0]) == 1
    )
    validation_contract_error = (
        valid_candidate_count == 0 and uniform_single_failure
    )
    summary = {
        "total_candidates": len(validation_records),
        "valid_candidates": valid_candidate_count,
        "failure_counts": {
            name: sum(
                1 for record in validation_records if name in record["failed_checks"]
            )
            for name in sorted(
                {
                    name
                    for record in validation_records
                    for name in record["failed_checks"]
                }
            )
        },
        "uniform_single_failed_check": (
            failure_signatures[0][0] if validation_contract_error else None
        ),
    }
    return summary, validation_contract_error


def _cp_cluster(
    indices: list[int],
    matrix: list[list[float]],
    energies: list[float],
    threshold: float,
) -> list[list[int]]:
    clusters: list[list[int]] = []
    for index in sorted(indices, key=lambda item: (energies[item], item)):
        placed = False
        for cluster in clusters:
            if matrix[index][cluster[0]] < threshold:
                cluster.append(index)
                placed = True
                break
        if not placed:
            clusters.append([index])
    return clusters


@bioma_tool(
    name="validate_cluster_select_conformers",
    description=(
        "Merge one or more conformer sources, validate topology, geometry, chirality, "
        "convergence and energy, then select core-heavy-atom-diverse structures."
    ),
    category="analysis",
    version="1.4.0",
    author="BioMA",
    args_schema=ConformerSelectionInput,
    tags=["conformer", "validation", "rmsd", "clustering", "selection"],
 result_kind="computed_prediction", result_validator=peptide_result_validator)
def validate_cluster_select_conformers(
    input_manifest_path: str,
    purpose: Literal["select_for_normal", "select_for_tight", "select_for_final"],
    target_ensemble_size: int,
    output_dir: str,
    selection_options_json: str = "{}",
    additional_input_manifest_paths_json: str = "[]",
    prior_selection_manifest_path: str | None = None,
) -> str:
    Chem, AllChem, rd_base, _ = _cp_rdkit()
    contract = require_final_target(target_ensemble_size, stage=purpose)
    options = _cp_json_object(selection_options_json, "selection_options_json")
    try:
        additional_values = json.loads(additional_input_manifest_paths_json)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"additional_input_manifest_paths_json is not valid JSON: {exc}",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        ) from exc
    if not isinstance(additional_values, list) or not all(
        isinstance(item, str) for item in additional_values
    ):
        raise ToolError(
            "additional_input_manifest_paths_json must be a JSON list of paths.",
            error_type="invalid_input",
            retriable=False,
            backend="RDKit",
        )
    input_manifest_files = [
        Path(input_manifest_path).expanduser().resolve(),
        *[Path(item).expanduser().resolve() for item in additional_values],
    ]
    input_manifests = [_cp_load_json(path) for path in input_manifest_files]
    for path, manifest in zip(input_manifest_files, input_manifests):
        if manifest.get("status") not in {"ok", "partial_success"}:
            raise ToolError(
                "Conformer selection requires an ok or partial_success input manifest; "
                f"{path} has status {manifest.get('status')!r}. Inspect that stage's "
                "results artifact before continuing.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
    input_manifest_file = input_manifest_files[0]
    input_manifest = input_manifests[0]
    topology_manifest_file = Path(input_manifest["topology_manifest"]).resolve()
    topology_manifest = _cp_load_json(topology_manifest_file)
    prior_selection_file: Path | None = None
    prior_selection: dict[str, Any] | None = None
    prior_candidate_ids: set[str] = set()
    if prior_selection_manifest_path is not None:
        if purpose != "select_for_normal":
            raise ToolError(
                "prior_selection_manifest_path is only valid for select_for_normal expansion.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
        prior_selection_file = Path(prior_selection_manifest_path).expanduser().resolve()
        prior_selection = _cp_load_json(prior_selection_file)
        if prior_selection.get("stage") != "select_for_normal":
            raise ToolError(
                "prior_selection_manifest_path must identify a select_for_normal manifest.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
        if prior_selection.get("chemical_hash") != topology_manifest.get("chemical_hash"):
            raise ToolError(
                "The prior selection must use the same chemical topology.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
        if prior_selection.get("target_ensemble_size") != target_ensemble_size:
            raise ToolError(
                "The prior selection must use the same target_ensemble_size.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
        prior_candidate_ids = {
            str(candidate_id) for candidate_id in prior_selection.get("candidate_ids", [])
        }
    for manifest in input_manifests[1:]:
        candidate_topology = Path(manifest["topology_manifest"]).resolve()
        if candidate_topology != topology_manifest_file:
            other_topology = _cp_load_json(candidate_topology)
            if other_topology.get("chemical_hash") != topology_manifest.get("chemical_hash"):
                raise ToolError(
                    "All merged manifests must use the same chemical topology.",
                    error_type="invalid_input",
                    retriable=False,
                    backend="RDKit",
                )
    if purpose != "select_for_normal" and len(input_manifests) > 1:
        comparable_fields = (
            "method",
            "gfn",
            "solvent_model",
            "solvent",
            "total_charge",
            "uhf",
            "optimization_level",
        )
        reference_optimization = input_manifest.get("optimization", {})
        reference_state = {
            field: reference_optimization.get(field) for field in comparable_fields
        }
        for manifest in input_manifests[1:]:
            optimization = manifest.get("optimization", {})
            candidate_state = {
                field: optimization.get(field) for field in comparable_fields
            }
            if candidate_state != reference_state:
                raise ToolError(
                    "Merged xTB manifests must use the same method, optimization "
                    "level, charge, UHF, and solvent model.",
                    error_type="invalid_input",
                    retriable=False,
                    backend="RDKit",
                )
    topology_path = Path(topology_manifest["artifacts"]["topology_sdf"]).resolve()
    atom_map_path = Path(topology_manifest["artifacts"]["atom_map_json"]).resolve()
    reference = _cp_read_sdf(topology_path)[0]
    _cp_apply_atom_map(reference, atom_map_path)
    reference_connectivity_hash = _cp_connectivity_hash(reference)
    expected_cip = _cp_expected_cip(reference)

    artifact_candidates = [
        "conformers_sdf",
        "optimized_sdf",
        "selected_sdf",
        "topology_sdf",
    ]
    molecules: list[Any] = []
    seen_candidate_ids: set[str] = set()
    for manifest_index, manifest in enumerate(input_manifests, start=1):
        source_value = next(
            (
                manifest.get("artifacts", {}).get(key)
                for key in artifact_candidates
                if manifest.get("artifacts", {}).get(key)
            ),
            None,
        )
        if source_value is None:
            raise ToolError(
                f"Input manifest {input_manifest_files[manifest_index - 1]} has no conformer SDF artifact.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
        manifest_source = str(
            manifest.get("source_backend")
            or manifest.get("sampling", {}).get("backend")
            or manifest.get("tool_name", f"source_{manifest_index}")
        ).lower()
        for local_index, raw_molecule in enumerate(
            _cp_read_sdf(Path(source_value).resolve()), start=1
        ):
            molecule = _cp_single_conf(raw_molecule)
            source_candidate_id = (
                molecule.GetProp("source_candidate_id")
                if molecule.HasProp("source_candidate_id")
                else molecule.GetProp("candidate_id")
                if molecule.HasProp("candidate_id")
                else f"candidate_{local_index:04d}"
            )
            candidate_id = (
                molecule.GetProp("candidate_id")
                if molecule.HasProp("candidate_id")
                else f"{manifest_source}:{source_candidate_id}"
            )
            if candidate_id in seen_candidate_ids:
                candidate_id = f"{manifest_source}:{manifest_index}:{source_candidate_id}"
            seen_candidate_ids.add(candidate_id)
            coordinate_source = (
                molecule.GetProp("source_backend")
                if molecule.HasProp("source_backend")
                else manifest_source
            )
            origin_manifest = (
                molecule.GetProp("source_manifest")
                if molecule.HasProp("source_manifest")
                else str(input_manifest_files[manifest_index - 1])
            )
            molecule.SetProp("candidate_id", candidate_id)
            molecule.SetProp("source_candidate_id", source_candidate_id)
            molecule.SetProp("source_backend", coordinate_source)
            molecule.SetProp("source_manifest", origin_manifest)
            molecules.append(molecule)
    if prior_candidate_ids:
        available_candidate_ids = {
            molecule.GetProp("candidate_id") for molecule in molecules
        }
        missing_prior_ids = sorted(prior_candidate_ids - available_candidate_ids)
        if missing_prior_ids:
            raise ToolError(
                "The merged source manifests do not contain every candidate from the "
                f"prior selection; missing {missing_prior_ids[:10]}.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
    core = _cp_apply_atom_map(molecules[0], atom_map_path)
    for mol in molecules[1:]:
        candidate_core = _cp_apply_atom_map(mol, atom_map_path)
        if candidate_core != core:
            raise ValueError("Core atom mapping differs between conformers.")
    if not core:
        raise ValueError("The topology atom map defines no core heavy atoms.")

    strict = purpose != "select_for_normal"
    validation_records: list[dict[str, Any]] = []
    valid_indices: list[int] = []
    for index, mol in enumerate(molecules):
        candidate_id = mol.GetProp("candidate_id") if mol.HasProp("candidate_id") else f"candidate_{index + 1:04d}"
        report = _cp_geometry_report(
            mol,
            reference_connectivity_hash,
            expected_cip,
            strict=strict,
        )
        converged = True
        energy = None
        if purpose != "select_for_normal":
            converged = mol.HasProp("xtb_converged") and mol.GetProp("xtb_converged").lower() == "true"
            if mol.HasProp("xtb_energy_hartree"):
                energy = float(mol.GetProp("xtb_energy_hartree"))
            report["checks"]["xtb_converged"] = converged
            report["checks"]["xtb_energy_available"] = energy is not None and math.isfinite(energy)
            report["passed"] = all(report["checks"].values())
        record = {
            "candidate_id": candidate_id,
            "source_candidate_id": mol.GetProp("source_candidate_id"),
            "source_backend": mol.GetProp("source_backend"),
            "source_manifest": mol.GetProp("source_manifest"),
            "boltz_confidence": float(mol.GetProp("boltz_confidence")) if mol.HasProp("boltz_confidence") else None,
            "passed": report["passed"],
            "energy_hartree": energy,
            "geometry_score": report["geometry_score"],
            "checks": report["checks"],
            "failed_checks": [
                name for name, passed in report["checks"].items() if not passed
            ],
            "unusual_bond_count": len(report["unusual_bonds"]),
            "unusual_bonds": report["unusual_bonds"],
            "severe_clash_count": len(report["severe_clashes"]),
            "severe_clashes": report["severe_clashes"],
            "chirality_mismatch_count": len(report["chirality_mismatches"]),
            "chirality_mismatches": report["chirality_mismatches"],
            "sulfur_orientation_signatures": report["sulfur_orientation_signatures"],
        }
        validation_records.append(record)
        if report["passed"]:
            valid_indices.append(index)

    relief_records: list[dict[str, Any]] = []
    # Candidates already passing the full geometry contract must not be
    # perturbed by a generic MMFF relaxation.  This is particularly important
    # for projected Boltz macrocycles and charged sulfonium products, whose
    # coordinates are valid but may not converge under RDKit's force field.
    # A caller may opt in for a deliberately prepared low-quality input pool.
    if purpose == "select_for_normal" and options.get("mmff_collision_relief", False):
        relieved: list[int] = []
        for index in valid_indices:
            mol = molecules[index]
            available = bool(AllChem.MMFFHasAllMoleculeParams(mol))
            converged: bool | None = None
            status_code: int | None = None
            if available:
                status_code = int(
                    AllChem.MMFFOptimizeMolecule(
                        mol,
                        confId=0,
                        maxIters=int(options.get("mmff_iterations", 200)),
                        mmffVariant="MMFF94s",
                    )
                )
                converged = status_code == 0
            report = _cp_geometry_report(
                mol,
                reference_connectivity_hash,
                expected_cip,
                strict=False,
            )
            relief_records.append(
                {
                    "candidate_id": validation_records[index]["candidate_id"],
                    "mmff_parameters_available": available,
                    "mmff_converged": converged,
                    "mmff_status_code": status_code,
                    "post_relief_passed": report["passed"],
                }
            )
            if report["passed"]:
                relieved.append(index)
                validation_records[index]["geometry_score"] = report["geometry_score"]
        valid_indices = relieved

    validation_failure_summary, validation_contract_error = (
        _cp_validation_failure_summary(validation_records, len(valid_indices))
    )

    initial_rmsd = float(options.get("initial_rmsd", 1.0))
    cluster_rmsd = float(options.get("cluster_rmsd", 1.5))
    matrix = _cp_rmsd_matrix(molecules, core)
    selected_indices: list[int] = []
    duplicate_of: dict[int, int] = {}
    clusters: list[list[int]] = []
    energies = [
        float(record["energy_hartree"]) if record["energy_hartree"] is not None else float("inf")
        for record in validation_records
    ]
    cumulative_selection_target: int | None = None
    cumulative_candidate_count: int | None = None

    if purpose == "select_for_normal":
        prior_indices = [
            index
            for index in valid_indices
            if validation_records[index]["candidate_id"] in prior_candidate_ids
        ]
        unique: list[int] = []
        ordered_candidates = sorted(
            [
                index
                for index in valid_indices
                if validation_records[index]["candidate_id"] not in prior_candidate_ids
            ],
            key=lambda item: (validation_records[item]["geometry_score"], validation_records[item]["candidate_id"]),
        )
        for index in ordered_candidates:
            match = next(
                (
                    kept
                    for kept in [*prior_indices, *unique]
                    if matrix[index][kept] < initial_rmsd
                ),
                None,
            )
            if match is None:
                unique.append(index)
            else:
                duplicate_of[index] = match
        default_normal_multiplier = 6 if len(input_manifests) > 1 else 3
        default_normal_target = math.ceil(
            max(default_normal_multiplier * target_ensemble_size, 16) / 8
        ) * 8
        if contract is not None and "normal_target" in options:
            raise ToolError("normal_target is controlled by the trusted ensemble contract.", error_type="invalid_input", retriable=False, backend="RDKit")
        cumulative_selection_target = internal_pool(
            contract, key="normal_optimization_pool_size", fallback=int(options.get("normal_target", default_normal_target))
        )
        if cumulative_selection_target < len(prior_candidate_ids):
            raise ToolError(
                "normal_target cannot be lower than the number of candidates carried "
                "from the prior selection.",
                error_type="invalid_input",
                retriable=False,
                backend="RDKit",
            )
        target = max(0, cumulative_selection_target - len(prior_candidate_ids))
        if prior_indices:
            diversity = []
            remaining_unique = set(unique)
            while remaining_unique:
                chosen = max(
                    remaining_unique,
                    key=lambda item: (
                        min(
                            matrix[item][selected]
                            for selected in [*prior_indices, *diversity]
                        ),
                        -validation_records[item]["geometry_score"],
                        validation_records[item]["candidate_id"],
                    ),
                )
                diversity.append(chosen)
                remaining_unique.remove(chosen)
        elif unique:
            start = min(unique, key=lambda item: (validation_records[item]["geometry_score"], item))
            diversity = _cp_diversity_order(unique, matrix, start)
        else:
            diversity = []
        # Sources are provenance, not quotas: all valid RDKit and Boltz proposals
        # compete under the same geometry and core-heavy-atom diversity criteria.
        selected_indices = []
        remaining = list(diversity)
        while remaining and len(selected_indices) < target:
            if selected_indices:
                chosen = max(
                    remaining,
                    key=lambda item: (
                        min(matrix[item][selected] for selected in selected_indices),
                        -validation_records[item]["geometry_score"],
                    ),
                )
            else:
                chosen = remaining[0]
            selected_indices.append(chosen)
            remaining.remove(chosen)
        selected_indices = selected_indices[:target]
        cumulative_candidate_count = len(prior_candidate_ids) + len(selected_indices)
        recommended_action = (
            "generate_more_source_conformers"
            if len(input_manifests) > 1
            and cumulative_candidate_count < cumulative_selection_target
            else "generate_more_rdkit_conformers"
            if cumulative_candidate_count < cumulative_selection_target
            else None
        )
    else:
        finite_valid = [index for index in valid_indices if math.isfinite(energies[index])]
        if finite_valid:
            minimum = min(energies[index] for index in finite_valid)
            for index in finite_valid:
                validation_records[index]["relative_energy_kcal_mol"] = (
                    energies[index] - minimum
                ) * _CP_HARTREE_TO_KCAL_MOL
        clusters = _cp_cluster(finite_valid, matrix, energies, cluster_rmsd)
        representatives = [cluster[0] for cluster in clusters]
        if purpose == "select_for_tight":
            default_tight_target = math.ceil(max(3 * target_ensemble_size, 16) / 8) * 8
            if contract is not None and "tight_target" in options:
                raise ToolError("tight_target is controlled by the trusted ensemble contract.", error_type="invalid_input", retriable=False, backend="RDKit")
            target = internal_pool(contract, key="tight_optimization_pool_size", fallback=int(options.get("tight_target", default_tight_target)))
            quota = min(int(options.get("energy_quota", math.ceil(target / 2))), len(representatives))
            energy_selected = sorted(representatives, key=lambda item: (energies[item], item))[:quota]
            remaining = [item for item in representatives if item not in energy_selected]
            diversity_selected: list[int] = []
            pool_selected = list(energy_selected)
            while remaining and len(pool_selected) < target:
                if pool_selected:
                    chosen = max(
                        remaining,
                        key=lambda item: (
                            min(matrix[item][selected] for selected in pool_selected),
                            -energies[item],
                        ),
                    )
                else:
                    chosen = min(remaining, key=lambda item: energies[item])
                diversity_selected.append(chosen)
                pool_selected.append(chosen)
                remaining.remove(chosen)
            selected_indices = energy_selected + diversity_selected
            recommended_action = "expand_normal_candidates" if len(selected_indices) < target else None
        else:
            target = target_ensemble_size
            selected_indices = sorted(representatives, key=lambda item: (energies[item], item))[:target]
            recommended_action = "expand_tight_candidates" if len(selected_indices) < target else None

    if validation_contract_error:
        selected_indices = []
        clusters = []
        recommended_action = "inspect_validation_contract"

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    selected_molecules: list[Any] = []
    for rank, index in enumerate(selected_indices, start=1):
        mol = Chem.Mol(molecules[index])
        mol.SetProp("selection_rank", str(rank))
        mol.SetProp("selection_purpose", purpose)
        if purpose != "select_for_normal":
            cluster_index = next(
                (number for number, cluster in enumerate(clusters, start=1) if index in cluster),
                0,
            )
            mol.SetProp("cluster_id", str(cluster_index))
            relative = validation_records[index].get("relative_energy_kcal_mol")
            if relative is not None:
                mol.SetProp("relative_energy_kcal_mol", str(relative))
        selected_molecules.append(mol)

    selected_path = destination / f"{purpose}_conformers.sdf"
    _cp_write_molecules(Chem, selected_path, selected_molecules)
    validation_path = destination / f"{purpose}_validation.json"
    rmsd_path = destination / f"{purpose}_rmsd_matrix.csv"
    clusters_path = destination / f"{purpose}_clusters.json"
    selection_path = destination / f"{purpose}_selection.csv"
    _cp_write_json(validation_path, {"purpose": purpose, "records": validation_records, "relief": relief_records})
    _cp_write_json(
        clusters_path,
        {
            "clusters": [
                [validation_records[index]["candidate_id"] for index in cluster]
                for cluster in clusters
            ],
            "duplicates": {
                validation_records[index]["candidate_id"]: validation_records[kept]["candidate_id"]
                for index, kept in duplicate_of.items()
            },
        },
    )
    with rmsd_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        labels = [record["candidate_id"] for record in validation_records]
        writer.writerow(["candidate_id", *labels])
        for label, row in zip(labels, matrix):
            writer.writerow([label, *[f"{value:.6f}" for value in row]])
    with selection_path.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = [
            "selection_rank",
            "candidate_id",
            "source_candidate_id",
            "source_backend",
            "boltz_confidence",
            "energy_hartree",
            "relative_energy_kcal_mol",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for rank, index in enumerate(selected_indices, start=1):
            writer.writerow(
                {
                    "selection_rank": rank,
                    "candidate_id": validation_records[index]["candidate_id"],
                    "source_candidate_id": validation_records[index]["source_candidate_id"],
                    "source_backend": validation_records[index]["source_backend"],
                    "boltz_confidence": validation_records[index]["boltz_confidence"],
                    "energy_hartree": validation_records[index].get("energy_hartree"),
                    "relative_energy_kcal_mol": validation_records[index].get("relative_energy_kcal_mol"),
                }
            )

    achieved_selection_count = (
        cumulative_candidate_count
        if purpose == "select_for_normal" and cumulative_candidate_count is not None
        else len(selected_indices)
    )
    required_selection_count = (
        cumulative_selection_target
        if purpose == "select_for_normal" and cumulative_selection_target is not None
        else target
    )
    status = (
        "validation_contract_error"
        if validation_contract_error
        else "ok"
        if achieved_selection_count >= required_selection_count
        else "insufficient_candidates"
    )
    manifest_path = destination / f"{purpose}_manifest.json"
    ensemble_hash = _cp_ensemble_result_hash(selected_molecules)
    result_hash = _cp_hash(
        {
            "status": status,
            "purpose": purpose,
            "candidate_ids": [
                validation_records[index]["candidate_id"] for index in selected_indices
            ],
            "carried_candidate_ids": sorted(prior_candidate_ids),
            "cumulative_candidate_count": cumulative_candidate_count,
            "valid_candidate_count": len(valid_indices),
            "cluster_count": (
                len(clusters) if purpose != "select_for_normal" else len(selected_indices)
            ),
            "recommended_action": recommended_action,
            "validation_failure_summary": validation_failure_summary,
            "ensemble_hash": ensemble_hash,
            "ensemble_contract": contract_hash(contract),
        }
    )
    manifest = {
        "contract_version": "1.0",
        "tool_name": "validate_cluster_select_conformers",
        "tool_version": "1.4.0",
        "stage": purpose,
        "status": status,
        "chemical_hash": topology_manifest["chemical_hash"],
        "input_hash": _cp_hash(
            {
                "inputs": [manifest.get("config_hash") for manifest in input_manifests],
                "prior_selection": (
                    prior_selection.get("config_hash") if prior_selection else None
                ),
            }
        ),
        "config_hash": _cp_hash(
            {
                "purpose": purpose,
                "target_ensemble_size": target_ensemble_size,
                "options": options,
                "inputs": [manifest.get("config_hash") for manifest in input_manifests],
                "prior_selection": (
                    prior_selection.get("config_hash") if prior_selection else None
                ),
                "rdkit": rd_base.rdkitVersion,
                "ensemble_contract": contract_hash(contract),
            }
        ),
        "result_hash": result_hash,
        "parent_manifest": str(input_manifest_file),
        "parent_manifests": [
            *[str(path) for path in input_manifest_files],
            *([str(prior_selection_file)] if prior_selection_file else []),
        ],
        "topology_manifest": str(topology_manifest_file),
        "target_ensemble_size": target_ensemble_size,
        "ensemble_contract_hash": contract_hash(contract),
        "internal_pool_size": target if purpose != "select_for_final" else target_ensemble_size,
        "selection_target": target,
        "cumulative_selection_target": cumulative_selection_target,
        "candidate_count": len(selected_indices),
        "candidate_ids": [validation_records[index]["candidate_id"] for index in selected_indices],
        "prior_selection_manifest": (
            str(prior_selection_file) if prior_selection_file else None
        ),
        "carried_candidate_count": len(prior_candidate_ids),
        "carried_candidate_ids": sorted(prior_candidate_ids),
        "cumulative_candidate_count": cumulative_candidate_count,
        "valid_candidate_count": len(valid_indices),
        "cluster_count": len(clusters) if purpose != "select_for_normal" else len(selected_indices),
        "source_candidate_counts": {
            source: sum(1 for record in validation_records if record["source_backend"] == source)
            for source in sorted({record["source_backend"] for record in validation_records})
        },
        "selected_source_counts": {
            source: sum(
                1
                for index in selected_indices
                if validation_records[index]["source_backend"] == source
            )
            for source in sorted({record["source_backend"] for record in validation_records})
        },
        "selection_policy": {
            "initial_rmsd_angstrom": initial_rmsd,
            "cluster_rmsd_angstrom": cluster_rmsd,
            "source_quota": "none",
            "energy_window_filter": False,
            "connectivity_hash_excludes_stereochemistry": True,
            "peptide_chirality_checked_separately": True,
        },
        "validation_failure_summary": validation_failure_summary,
        "optimization": input_manifest.get("optimization", {}),
        "optimization_timing": input_manifest.get("timing", {}),
        "solvent_selection": input_manifest.get("solvent_selection", {}),
        "experimental_conditions": topology_manifest.get("experimental_conditions", {}),
        "total_charge": topology_manifest["total_charge"],
        "uhf": topology_manifest["uhf"],
        "artifacts": {
            "selected_sdf": str(selected_path),
            "validation_json": str(validation_path),
            "rmsd_matrix_csv": str(rmsd_path),
            "clusters_json": str(clusters_path),
            "selection_csv": str(selection_path),
        },
        "artifact_hashes": {
            "ensemble_scientific_sha256": ensemble_hash,
        },
        "warnings": (
            [
                "Every candidate failed only the same validation check; inspect "
                "the validation contract before generating more conformers."
            ]
            if validation_contract_error
            else []
        ),
        "recommended_action": recommended_action,
    }
    _cp_write_json(manifest_path, manifest)
    return json.dumps(
        {
            "status": status,
            "purpose": purpose,
            "manifest_path": str(manifest_path),
            "selected_sdf": str(selected_path),
            "selected_count": len(selected_indices),
            "selection_target": target,
            "carried_candidate_count": len(prior_candidate_ids),
            "cumulative_candidate_count": cumulative_candidate_count,
            "cumulative_selection_target": cumulative_selection_target,
            "recommended_action": recommended_action,
            "config_hash": manifest["config_hash"],
            "result_hash": result_hash,
            "validation_failure_summary": validation_failure_summary,
        },
        ensure_ascii=False,
        indent=2,
    )
