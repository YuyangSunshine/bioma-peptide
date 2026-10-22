"""
Small molecule tools: RDKit-based property calculation, similarity, substructure, 3D conformer.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


class MolPropsInput(ToolInput):
    smiles: str = Field(description="SMILES string of the molecule")


@bioma_tool(
    name="compute_molecular_properties",
    description="Compute drug-likeness properties (MW, LogP, HBD, HBA, TPSA, Lipinski RO5) from SMILES.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=MolPropsInput,
)
def compute_molecular_properties(smiles: str) -> str:
    """Calculate physicochemical properties using RDKit."""
    import json

    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, rdMolDescriptors

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return f"Error: invalid SMILES '{smiles}'"

        mw = round(Descriptors.MolWt(mol), 2)
        logp = round(Descriptors.MolLogP(mol), 2)
        hbd = rdMolDescriptors.CalcNumHBD(mol)
        hba = rdMolDescriptors.CalcNumHBA(mol)
        tpsa = round(Descriptors.TPSA(mol), 1)
        rot_bonds = rdMolDescriptors.CalcNumRotatableBonds(mol)
        rings = rdMolDescriptors.CalcNumRings(mol)
        arom_rings = rdMolDescriptors.CalcNumAromaticRings(mol)

        ro5 = mw <= 500 and logp <= 5 and hbd <= 5 and hba <= 10
        veber = rot_bonds <= 10 and tpsa <= 140

        result = {
            "MW": mw,
            "LogP": logp,
            "HBD": hbd,
            "HBA": hba,
            "TPSA": tpsa,
            "RotatableBonds": rot_bonds,
            "Rings": rings,
            "AromaticRings": arom_rings,
            "Lipinski_RO5": ro5,
            "Veber_Rule": veber,
        }
        return json.dumps(result, indent=2)

    except ImportError:
        # Fallback without RDKit
        return (
            '{"error": "RDKit not installed", '
            '"install": "pip install rdkit", '
            f'"smiles": "{smiles}"}}'
        )


class SimilarityInput(ToolInput):
    smiles_query: str = Field(description="Query molecule SMILES")
    smiles_target: str = Field(description="Target molecule SMILES")
    fingerprint: Optional[str] = Field(default="morgan", description="Fingerprint type: morgan | maccs | topological")


@bioma_tool(
    name="compute_molecular_similarity",
    description="Compute Tanimoto similarity between two molecules using molecular fingerprints.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=SimilarityInput,
)
def compute_molecular_similarity(
    smiles_query: str,
    smiles_target: str,
    fingerprint: str = "morgan",
) -> str:
    """Compute Tanimoto coefficient between two molecules."""
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem, MACCSkeys
        from rdkit import DataStructs

        mol_q = Chem.MolFromSmiles(smiles_query)
        mol_t = Chem.MolFromSmiles(smiles_target)
        if mol_q is None or mol_t is None:
            return "Error: one or both SMILES are invalid"

        if fingerprint == "morgan":
            fp_q = AllChem.GetMorganFingerprintAsBitVect(mol_q, radius=2, nBits=2048)
            fp_t = AllChem.GetMorganFingerprintAsBitVect(mol_t, radius=2, nBits=2048)
        elif fingerprint == "maccs":
            fp_q = MACCSkeys.GenMACCSKeys(mol_q)
            fp_t = MACCSkeys.GenMACCSKeys(mol_t)
        else:
            from rdkit.Chem.Fingerprints import FingerprintMols
            fp_q = FingerprintMols.FingerprintMol(mol_q)
            fp_t = FingerprintMols.FingerprintMol(mol_t)

        sim = round(DataStructs.TanimotoSimilarity(fp_q, fp_t), 4)
        interpretation = (
            "highly similar" if sim >= 0.7
            else "moderately similar" if sim >= 0.4
            else "low similarity"
        )
        return f"Tanimoto similarity ({fingerprint}): {sim} — {interpretation}"

    except ImportError:
        return "RDKit not installed. Install with: pip install rdkit"


class SubstructureInput(ToolInput):
    smiles: str = Field(description="Molecule SMILES")
    smarts: str = Field(description="SMARTS pattern to search for")


@bioma_tool(
    name="search_substructure",
    description="Search for a SMARTS pattern (substructure/pharmacophore) within a molecule.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=SubstructureInput,
)
def search_substructure(smiles: str, smarts: str) -> str:
    """Check if a SMARTS pattern is present in a molecule."""
    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(smiles)
        pattern = Chem.MolFromSmarts(smarts)
        if mol is None:
            return f"Error: invalid SMILES '{smiles}'"
        if pattern is None:
            return f"Error: invalid SMARTS '{smarts}'"

        matches = mol.GetSubstructMatches(pattern)
        if matches:
            return (
                f"Pattern '{smarts}' FOUND in molecule. "
                f"Match count: {len(matches)}. "
                f"Matching atom indices: {list(matches[0])}"
            )
        return f"Pattern '{smarts}' NOT found in molecule."

    except ImportError:
        return "RDKit not installed. Install with: pip install rdkit"


class ConformerInput(ToolInput):
    smiles: str = Field(description="Molecule SMILES")
    output_format: Optional[str] = Field(default="sdf", description="Output format: sdf | pdb")


@bioma_tool(
    name="generate_3d_conformation",
    description="Generate an energy-minimized 3D conformation for a small molecule using RDKit/MMFF94.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ConformerInput,
)
def generate_3d_conformation(smiles: str, output_format: str = "sdf") -> str:
    """Generate 3D coordinates using ETKDG + MMFF94 minimization."""
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
        from rdkit.Chem.rdMolDescriptors import CalcNumRotatableBonds  # noqa: F401

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return f"Error: invalid SMILES '{smiles}'"

        mol = Chem.AddHs(mol)
        AllChem.EmbedMolecule(mol, AllChem.ETKDGv3())
        result = AllChem.MMFFOptimizeMolecule(mol, mmffVariant="MMFF94")

        if result == -1:
            return "MMFF94 minimization failed — molecule may be too complex"

        mol = Chem.RemoveHs(mol)
        if output_format == "pdb":
            from rdkit.Chem import AllChem
            pdb_block = Chem.MolToPDBBlock(mol)
            return f"3D structure (PDB format, first 1000 chars):\n{pdb_block[:1000]}"
        else:
            sdf_block = Chem.MolToMolBlock(mol)
            return f"3D structure generated successfully. MMFF94 minimization: {'converged' if result == 0 else 'not converged'}.\nSDF block (first 500 chars):\n{sdf_block[:500]}"

    except ImportError:
        return "RDKit not installed. Install with: pip install rdkit"
