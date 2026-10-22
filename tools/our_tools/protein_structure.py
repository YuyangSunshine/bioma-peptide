"""
Protein structure tools: PDB fetch, chain parsing, binding site detection,
ESMFold prediction, AMBER relaxation.
"""

from __future__ import annotations

import json
from typing import Optional

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError, requests_tool_error


class PDBFetchInput(ToolInput):
    pdb_id: str = Field(description="4-letter PDB accession code, e.g. '1IEP'")


@bioma_tool(
    name="fetch_pdb_structure",
    description="Download a PDB structure file from RCSB PDB by accession code.",
    category="database",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=PDBFetchInput,
    requires_network=True,
)
def fetch_pdb_structure(pdb_id: str) -> str:
    """Download PDB file from RCSB."""
    pdb_id = pdb_id.upper().strip()
    url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        content = resp.text
        lines = content.count("\n")
        atoms = sum(1 for line in content.splitlines() if line.startswith("ATOM  "))
        return (
            f"Downloaded PDB {pdb_id}: {lines} lines, {atoms} ATOM records.\n"
            f"First 500 chars:\n{content[:500]}"
        )
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="RCSB PDB") from exc


class ChainParseInput(ToolInput):
    pdb_content: str = Field(description="PDB file content as string")


@bioma_tool(
    name="parse_pdb_chains",
    description="Parse PDB content and extract chain IDs, residue counts, and sequences.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ChainParseInput,
)
def parse_pdb_chains(pdb_content: str) -> str:
    """Extract chain information from PDB content."""
    THREE_TO_ONE = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
        "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
        "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
        "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    }
    chains: dict[str, dict] = {}
    seen_residues: dict[str, set] = {}

    for line in pdb_content.splitlines():
        if not line.startswith("ATOM  "):
            continue
        chain_id = line[21]
        res_name = line[17:20].strip()
        res_seq = line[22:26].strip()
        key = f"{res_name}{res_seq}"

        if chain_id not in chains:
            chains[chain_id] = {"residues": 0, "sequence": ""}
            seen_residues[chain_id] = set()

        if key not in seen_residues[chain_id]:
            seen_residues[chain_id].add(key)
            chains[chain_id]["residues"] += 1
            aa = THREE_TO_ONE.get(res_name, "X")
            chains[chain_id]["sequence"] += aa

    result = {
        chain_id: {
            "residue_count": info["residues"],
            "sequence_preview": info["sequence"][:50] + ("..." if len(info["sequence"]) > 50 else ""),
        }
        for chain_id, info in chains.items()
    }
    return json.dumps(result, indent=2)


class BindingSiteInput(ToolInput):
    pdb_content: str = Field(description="PDB file content as string")
    method: Optional[str] = Field(default="ligand", description="Method: ligand (near ligand) or cavity")


@bioma_tool(
    name="identify_binding_sites",
    description="Identify potential binding sites in a protein structure from PDB content.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=BindingSiteInput,
)
def identify_binding_sites(pdb_content: str, method: str = "ligand") -> str:
    """Identify binding sites near ligands or by geometry."""
    import math

    ligand_atoms: list[tuple[float, float, float, str]] = []
    protein_atoms: list[tuple[float, float, float, str, str, int]] = []

    for line in pdb_content.splitlines():
        if line.startswith("HETATM"):
            res_name = line[17:20].strip()
            if res_name in ("HOH", "WAT"):
                continue
            try:
                x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
                ligand_atoms.append((x, y, z, res_name))
            except ValueError:
                pass
        elif line.startswith("ATOM  "):
            try:
                x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
                res_name = line[17:20].strip()
                chain = line[21]
                res_num = int(line[22:26])
                protein_atoms.append((x, y, z, res_name, chain, res_num))
            except ValueError:
                pass

    if not ligand_atoms:
        return "No ligands found in structure. Cannot identify binding site by ligand proximity."

    # Find protein residues within 5 Å of any ligand atom
    binding_residues: set[tuple[str, str, int]] = set()
    for lx, ly, lz, _ in ligand_atoms:
        for px, py, pz, res_name, chain, res_num in protein_atoms:
            dist = math.sqrt((px-lx)**2 + (py-ly)**2 + (pz-lz)**2)
            if dist <= 5.0:
                binding_residues.add((res_name, chain, res_num))

    result = {
        "ligands_detected": list({ligand[3] for ligand in ligand_atoms}),
        "binding_site_residues": [
            f"{res_name}{res_num}:{chain}" for res_name, chain, res_num
            in sorted(binding_residues, key=lambda x: x[2])
        ]
    }
    return json.dumps(result, indent=2)


class ESMFoldInput(ToolInput):
    sequence: str = Field(description="Protein amino acid sequence")


@bioma_tool(
    name="predict_structure_esmfold",
    description="Predict protein 3D structure from sequence using ESMFold (via ESM Metagenomic Atlas API).",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ESMFoldInput,
    requires_network=True,
)
def predict_structure_esmfold(sequence: str) -> str:
    """Predict structure using ESMFold API."""
    if len(sequence) > 1022:
        raise ToolError(
            "ESMFold supports sequences up to 1022 amino acids.",
            error_type="invalid_input",
            retriable=False,
            backend="ESMFold",
        )

    try:
        resp = requests.post(
            "https://api.esmatlas.com/foldSequence/v1/pdb/",
            data=sequence,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=120,
        )
        resp.raise_for_status()
        pdb_content = resp.text
        atom_count = sum(
            1 for line in pdb_content.splitlines() if line.startswith("ATOM  ")
        )
        # Extract mean pLDDT from B-factor column
        b_factors = []
        for line in pdb_content.splitlines():
            if line.startswith("ATOM  "):
                try:
                    b_factors.append(float(line[60:66]))
                except ValueError:
                    pass
        mean_plddt = round(sum(b_factors) / len(b_factors), 1) if b_factors else None
        return (
            f"ESMFold structure predicted successfully.\n"
            f"ATOM records: {atom_count}, Mean pLDDT: {mean_plddt}\n"
            f"PDB content (first 500 chars):\n{pdb_content[:500]}"
        )
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="ESMFold") from exc


class AMBERInput(ToolInput):
    pdb_content: str = Field(description="PDB file content to relax")
    max_iterations: Optional[int] = Field(default=200, description="Maximum minimization steps")


@bioma_tool(
    name="relax_structure_amber",
    description="Energy-minimize a protein structure using OpenMM AMBER force field.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=AMBERInput,
)
def relax_structure_amber(pdb_content: str, max_iterations: int = 200) -> str:
    """Perform AMBER energy minimization using OpenMM."""
    try:
        import openmm
        from openmm import app, unit
        import io

        pdb = app.PDBFile(io.StringIO(pdb_content))
        forcefield = app.ForceField("amber14-all.xml", "amber14/tip3pfb.xml")
        system = forcefield.createSystem(pdb.topology)
        integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
        simulation = app.Simulation(pdb.topology, system, integrator)
        simulation.context.setPositions(pdb.positions)
        energy_before = simulation.context.getState(getEnergy=True).getPotentialEnergy()
        simulation.minimizeEnergy(maxIterations=max_iterations)
        energy_after = simulation.context.getState(getEnergy=True).getPotentialEnergy()

        return (
            f"AMBER relaxation completed.\n"
            f"Energy before: {energy_before}\n"
            f"Energy after: {energy_after}"
        )
    except ImportError:
        return (
            "OpenMM not installed. Install with: conda install -c conda-forge openmm\n"
            "Relaxation would minimize steric clashes and improve bond geometry."
        )
    except Exception as exc:
        return f"AMBER relaxation error: {exc}"


class StructureEnergyInput(ToolInput):
    pdb_content: str = Field(description="PDB file content")


@bioma_tool(
    name="compute_structure_energy",
    description="Compute the potential energy of a protein structure using AMBER force field.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=StructureEnergyInput,
)
def compute_structure_energy(pdb_content: str) -> str:
    """Compute AMBER potential energy (requires OpenMM)."""
    try:
        import openmm
        from openmm import app, unit
        import io

        pdb = app.PDBFile(io.StringIO(pdb_content))
        forcefield = app.ForceField("amber14-all.xml")
        system = forcefield.createSystem(pdb.topology, nonbondedMethod=app.NoCutoff)
        integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
        sim = app.Simulation(pdb.topology, system, integrator)
        sim.context.setPositions(pdb.positions)
        state = sim.context.getState(getEnergy=True)
        energy = state.getPotentialEnergy()
        return f"Potential energy: {energy}"
    except ImportError:
        return "OpenMM not installed. Install with: conda install -c conda-forge openmm"
