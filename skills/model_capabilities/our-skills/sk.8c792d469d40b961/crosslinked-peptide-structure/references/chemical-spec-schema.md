# Chemical specification

Pass one JSON object to `build_crosslinked_peptide_topology`.

## Required fields

```json
{
  "sequence": "GMNSQWRSMKVQLVAM",
  "residue_stereochemistry": "standard_L",
  "n_terminus": "acetylated",
  "c_terminus": "primary_amide",
  "components": [],
  "connections": [],
  "charge_states": [],
  "total_charge": 0,
  "uhf": 0,
  "pH": 7.0,
  "experimental_conditions": {},
  "expected_components": 1
}
```

Canonical peptides are generated from `sequence`. A peptide containing noncanonical residues can provide `peptide_sdf_path` and `peptide_atom_labels`; every label supplies a zero-based atom index, one-based residue number, residue name, and atom name. Use `predefined` terminal caps when the supplied structure already contains its final termini.

## Components

Each external linker or covalent component has an identifier and SMILES:

```json
{
  "component_id": "TBMB",
  "name": "1,3,5-tris(bromomethyl)benzene",
  "smiles": "BrCc1cc(CBr)cc(CBr)c1",
  "residue_name": "TBM"
}
```

Mapped SMILES provides stable component atom identities. Unmapped SMILES can use deterministic leaving-group handles.

## Connections

The peptide endpoint uses a one-based residue number and PDB atom name. A reactive handle selects the atom bonded to the indexed leaving group:

```json
{
  "connection_id": "met2_tbmb_arm1",
  "peptide_residue": 2,
  "peptide_atom": "SD",
  "component_id": "TBMB",
  "reactive_group_index": 1,
  "leaving_element": "Br",
  "bond_order": 1,
  "stereochemistry": "unspecified"
}
```

Use `stereocenter_atoms` with `atom1`, `atom2`, or stable atom identifiers when a connection creates a stereocenter. A trivalent sulfur or phosphorus at `atom1` is recorded as unspecified by default when the experiment supplies no configuration.

Advanced specifications can address endpoints directly:

```json
{
  "atom1": "PEP:1:N",
  "atom2": "PEP:16:C",
  "bond_order": 1,
  "remove_atoms": []
}
```

Component atoms use `COMP:<component_id>:MAP:<atom-map>` for mapped SMILES and `COMP:<component_id>:IDX:<one-based-index>` for indexed atoms.

## Charge states

Apply the dominant pH-dependent state after constructing caps and connections:

```json
[
  {
    "residue": 7,
    "group": "arginine_sidechain",
    "formal_charge": 1,
    "hydrogen_delta": 1,
    "basis": "dominant state at the supplied pH"
  },
  {
    "residue": 10,
    "atom_name": "NZ",
    "formal_charge": 1,
    "hydrogen_delta": 1,
    "basis": "dominant state at the supplied pH"
  },
  {
    "atom": "PEP:2:SD",
    "formal_charge": 1,
    "explicit_hydrogens": 0,
    "basis": "alkylated sulfonium product"
  }
]
```

Record `pKa`, `pH`, `state`, `basis`, and `microstate_sensitive` as additional metadata when available.

## Experimental conditions

```json
{
  "solvents": [
    {"name": "water", "fraction": 0.70, "fraction_type": "volume"},
    {"name": "acetonitrile", "fraction": 0.30, "fraction_type": "volume"}
  ],
  "additives": [
    {"name": "formic acid", "amount": "1%"}
  ],
  "pH": 2.5,
  "duration": "24 h",
  "temperature": null
}
```
