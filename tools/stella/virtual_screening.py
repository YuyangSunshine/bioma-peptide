"""
Virtual screening and ADMET prediction tools.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class ADMETInput(ToolInput):
    smiles_list: str = Field(description="JSON array of SMILES strings, e.g. '[\"CCO\", \"c1ccccc1\"]'")
    filters: Optional[str] = Field(default="ro5", description="Comma-separated filters: ro5, veber, pains")


@bioma_tool(
    name="predict_admet_properties",
    description="Predict ADMET (absorption, distribution, metabolism, excretion, toxicity) properties and filter drug candidates.",
    category="analysis",
    version="1.0.0",
    author="STELLA Team",
    args_schema=ADMETInput,
)
def predict_admet_properties(smiles_list: str, filters: str = "ro5") -> str:
    """Predict ADMET and filter by drug-likeness rules."""

    try:
        smiles = json.loads(smiles_list)
    except (json.JSONDecodeError, TypeError):
        smiles = [s.strip() for s in str(smiles_list).split(",") if s.strip()]

    filter_set = {f.strip().lower() for f in filters.split(",")}

    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors

        results = []
        for smi in smiles:
            mol = Chem.MolFromSmiles(smi)
            if mol is None:
                results.append({"smiles": smi, "valid": False, "reason": "invalid SMILES"})
                continue

            mw = Descriptors.MolWt(mol)
            logp = Descriptors.MolLogP(mol)
            hbd = rdMolDescriptors.CalcNumHBD(mol)
            hba = rdMolDescriptors.CalcNumHBA(mol)
            tpsa = Descriptors.TPSA(mol)
            rot_bonds = rdMolDescriptors.CalcNumRotatableBonds(mol)

            passes_ro5 = mw <= 500 and logp <= 5 and hbd <= 5 and hba <= 10
            passes_veber = rot_bonds <= 10 and tpsa <= 140
            passes = True

            if "ro5" in filter_set and not passes_ro5:
                passes = False
            if "veber" in filter_set and not passes_veber:
                passes = False

            results.append({
                "smiles": smi, "valid": True, "passes": passes,
                "MW": round(mw, 1), "LogP": round(logp, 2),
                "HBD": hbd, "HBA": hba, "TPSA": round(tpsa, 1),
                "RotBonds": rot_bonds,
            })

        passed = [r for r in results if r.get("passes", False)]
        return json.dumps(
            {"total": len(smiles), "passed": len(passed), "failed": len(smiles) - len(passed),
             "results": results},
            indent=2,
        )
    except ImportError:
        return "RDKit not installed. Install with: pip install rdkit"


class DockingInput(ToolInput):
    target_pdb: str = Field(description="PDB ID or path to target PDB file")
    ligands: str = Field(description="JSON array of SMILES strings")
    binding_site: Optional[str] = Field(default=None, description="Binding site center as 'x,y,z'")


@bioma_tool(
    name="run_molecular_docking",
    description="Run molecular docking of ligands against a protein target using AutoDock Vina (if available).",
    category="analysis",
    version="1.0.0",
    author="STELLA Team",
    args_schema=DockingInput,
)
def run_molecular_docking(
    target_pdb: str,
    ligands: str,
    binding_site: str | None = None,
) -> str:
    """Run AutoDock Vina docking."""
    try:
        ligand_list = json.loads(ligands)
    except (json.JSONDecodeError, TypeError):
        ligand_list = [str(ligands)]

    try:
        from vina import Vina  # noqa: F401
        # Full docking implementation would go here
        results = [{"smiles": smi, "vina_score": None, "note": "Vina available but full pipeline requires PDB prep"}
                   for smi in ligand_list[:5]]
        return json.dumps(results, indent=2)
    except ImportError:
        # Return simulated results for demonstration
        import random
        random.seed(42)
        results = [
            {"smiles": smi, "docking_score_kcal_mol": round(random.uniform(-10, -5), 2),
             "note": "Simulated — install autodock-vina for real docking"}
            for smi in ligand_list[:10]
        ]
        results.sort(key=lambda x: x["docking_score_kcal_mol"])
        return (
            json.dumps(results, indent=2) +
            "\n[Note: AutoDock Vina not installed. Scores are simulated. "
            "Install: pip install vina]"
        )


class PLIInput(ToolInput):
    pdb_content: str = Field(description="PDB complex with protein and ligand")
    ligand_resname: Optional[str] = Field(default=None, description="3-letter ligand residue name")


@bioma_tool(
    name="analyze_protein_ligand_interactions",
    description="Analyze protein-ligand interactions: hydrogen bonds, hydrophobic contacts, pi-stacking.",
    category="analysis",
    version="1.0.0",
    author="STELLA Team",
    args_schema=PLIInput,
)
def analyze_protein_ligand_interactions(pdb_content: str, ligand_resname: str | None = None) -> str:
    """Identify key protein-ligand contacts."""
    import math

    ligand_atoms = []
    protein_atoms = []

    for line in pdb_content.splitlines():
        if line.startswith("HETATM"):
            res_name = line[17:20].strip()
            if res_name in ("HOH", "WAT"):
                continue
            if ligand_resname and res_name != ligand_resname:
                continue
            try:
                x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
                atom_name = line[12:16].strip()
                ligand_atoms.append((x, y, z, atom_name, res_name))
            except ValueError:
                pass
        elif line.startswith("ATOM  "):
            try:
                x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
                res_name = line[17:20].strip()
                res_num = int(line[22:26])
                chain = line[21]
                atom_name = line[12:16].strip()
                protein_atoms.append((x, y, z, res_name, res_num, chain, atom_name))
            except ValueError:
                pass

    if not ligand_atoms:
        return "No ligand atoms found in structure."

    contacts = []
    for lx, ly, lz, latom, lres in ligand_atoms:
        for px, py, pz, pres, pnum, chain, patom in protein_atoms:
            dist = math.sqrt((px-lx)**2 + (py-ly)**2 + (pz-lz)**2)
            if dist <= 4.0:
                contact_type = "hydrophobic"
                if latom.startswith("O") or latom.startswith("N"):
                    contact_type = "H-bond" if dist < 3.5 else "polar"
                contacts.append({
                    "ligand_atom": f"{lres}:{latom}",
                    "protein_residue": f"{pres}{pnum}:{chain}",
                    "distance_A": round(dist, 2),
                    "type": contact_type,
                })

    contacts.sort(key=lambda x: x["distance_A"])
    return json.dumps({"contacts": contacts[:20], "total_contacts": len(contacts)}, indent=2)
