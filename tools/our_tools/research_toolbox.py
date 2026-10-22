"""
Research toolbox adapters ported from predagent_research into BioAgent.

The original research toolbox used a third-party tool decorator. This module
re-exposes those capabilities with BioAgent's native @bioma_tool registry and
keeps heavyweight imports lazy so optional dependencies do not break discovery.
"""

from __future__ import annotations

import json
import os
import re
import time
import csv
from io import StringIO
from pathlib import Path
from typing import Any, Literal, Optional

import requests
from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import ToolError, requests_tool_error


_MODEL_CACHE: dict[str, Any] = {}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _get_device():
    import torch

    device_name = os.getenv("BIOAGENT_ESM_DEVICE", "cuda:0")
    if device_name.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"ESM requested device {device_name}, but CUDA is not available")
    return torch.device(device_name)


def _load_esm2_t33():
    if "esm2_t33" not in _MODEL_CACHE:
        import esm

        model, alphabet = esm.pretrained.esm2_t33_650M_UR50D()
        model = model.to(_get_device())
        model.eval()
        _MODEL_CACHE["esm2_t33"] = model
        _MODEL_CACHE["esm2_t33_alphabet"] = alphabet
    return _MODEL_CACHE["esm2_t33"], _MODEL_CACHE["esm2_t33_alphabet"]


def _validate_protein_sequence(sequence: str) -> str:
    seq = re.sub(r"\s+", "", sequence or "").upper()
    if not seq or not re.fullmatch(r"[A-Z]+", seq):
        raise ValueError("sequence must contain only amino-acid letters")
    return seq


class ESM2EmbeddingInput(ToolInput):
    sequence: str = Field(description="Protein amino acid sequence.")


@bioma_tool(
    name="extract_protein_embeddings_with_esm2",
    description="Extract an ESM-2 t33 mean protein embedding for a sequence.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ESM2EmbeddingInput,
    tags=["protein", "esm2", "embedding"],
)
def extract_protein_embeddings_with_esm2(sequence: str) -> str:
    """Extract ESM-2 sequence-level embedding metadata."""
    try:
        import torch

        seq = _validate_protein_sequence(sequence)
        model, alphabet = _load_esm2_t33()
        batch_converter = alphabet.get_batch_converter()
        _, _, tokens = batch_converter([("protein_sequence", seq)])
        tokens = tokens.to(next(model.parameters()).device)

        with torch.no_grad():
            results = model(tokens, repr_layers=[33], return_contacts=False)

        sequence_repr = results["representations"][33][0, 1 : len(seq) + 1].mean(0)
        return _json({
            "status": "ok",
            "model": "esm2_t33_650M_UR50D",
            "sequence_length": len(seq),
            "embedding_dim": int(sequence_repr.shape[0]),
            "note": "Embedding tensor was computed but not returned inline to keep tool output small.",
        })
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"ESM-2 dependency unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class BiopythonPDBSeqInput(ToolInput):
    pdb_string: str = Field(description="PDB-format structure content.")
    chain_id: Optional[str] = Field(default=None, description="Optional chain ID to extract.")


@bioma_tool(
    name="get_sequence_from_pdb_with_biopython",
    description="Extract chain sequences from PDB content using Biopython.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=BiopythonPDBSeqInput,
    tags=["protein", "structure", "biopython"],
)
def get_sequence_from_pdb_with_biopython(pdb_string: str, chain_id: str | None = None) -> str:
    """Parse PDB content and extract chain sequence(s)."""
    try:
        from Bio.PDB import PDBParser
        from Bio.SeqUtils import seq1

        parser = PDBParser(QUIET=True)
        structure = parser.get_structure("protein", StringIO(pdb_string))
        chains = list(structure.get_chains())
        chain_ids = [chain.id for chain in chains]
        if chain_id and chain_id not in chain_ids:
            return _json({"status": "error", "error": f"chain {chain_id!r} not found", "available_chains": chain_ids})

        selected = [chain for chain in chains if not chain_id or chain.id == chain_id]
        results = []
        for chain in selected:
            sequence = seq1("".join(res.get_resname() for res in chain if "CA" in res), custom_map={"MSE": "M"})
            results.append({"chain_id": chain.id, "sequence": sequence, "length": len(sequence)})
        return _json({"status": "ok", "chains": results})
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"Biopython unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class BiopythonBindingSiteInput(ToolInput):
    pdb_string: str = Field(description="PDB-format structure content.")
    chain_id: str = Field(description="Protein chain ID.")
    center_residue_id: int = Field(description="Residue number used as the search center.")
    radius: float = Field(default=5.0, description="Search radius in Angstrom.")


@bioma_tool(
    name="find_binding_site_residues_with_biopython",
    description="Find residues near a center residue in a PDB structure using Biopython.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=BiopythonBindingSiteInput,
    tags=["protein", "structure", "binding-site"],
)
def find_binding_site_residues_with_biopython(
    pdb_string: str,
    chain_id: str,
    center_residue_id: int,
    radius: float = 5.0,
) -> str:
    """Search residues within a radius of a center residue."""
    try:
        from Bio.PDB import NeighborSearch, PDBParser

        parser = PDBParser(QUIET=True)
        structure = parser.get_structure("protein", StringIO(pdb_string))
        model = structure[0]
        if chain_id not in [chain.id for chain in model.get_chains()]:
            return _json({"status": "error", "error": f"chain {chain_id!r} not found"})
        chain = model[chain_id]
        if (" ", center_residue_id, " ") not in chain:
            return _json({"status": "error", "error": f"residue {center_residue_id} not found in chain {chain_id}"})

        center = chain[(" ", center_residue_id, " ")]
        ns = NeighborSearch(list(structure.get_atoms()))
        nearby = set()
        for atom in center.get_atoms():
            for residue in ns.search(atom.get_coord(), radius, level="R"):
                parent_chain = residue.get_parent().id
                resnum = residue.get_id()[1]
                if not (parent_chain == chain_id and resnum == center_residue_id):
                    nearby.add(f"{parent_chain}{resnum}")
        return _json({
            "status": "ok",
            "center": f"{chain_id}{center_residue_id}",
            "radius_angstrom": radius,
            "nearby_residues": sorted(nearby),
        })
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"Biopython unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class RDKitBasicPropsInput(ToolInput):
    smiles_string: str = Field(description="Small-molecule SMILES string.")


def _load_rdkit():
    from rdkit import Chem
    from rdkit.Chem import AllChem, DataStructs, Descriptors

    return Chem, Descriptors, AllChem, DataStructs


@bioma_tool(
    name="calculate_basic_mol_properties_with_rdkit",
    description="Calculate RDKit molecular properties: MW, LogP, HBD, HBA, TPSA.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=RDKitBasicPropsInput,
    tags=["small-molecule", "rdkit"],
)
def calculate_basic_mol_properties_with_rdkit(smiles_string: str) -> str:
    """Calculate basic molecular properties with RDKit."""
    try:
        Chem, Descriptors, _, _ = _load_rdkit()
        mol = Chem.MolFromSmiles(smiles_string)
        if mol is None:
            return _json({"status": "error", "error": "invalid SMILES"})
        return _json({
            "status": "ok",
            "smiles": smiles_string,
            "molecular_weight": round(Descriptors.MolWt(mol), 4),
            "logp": round(Descriptors.MolLogP(mol), 4),
            "h_bond_donors": Descriptors.NumHDonors(mol),
            "h_bond_acceptors": Descriptors.NumHAcceptors(mol),
            "tpsa": round(Descriptors.TPSA(mol), 4),
        })
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"RDKit unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class RDKitSimilarityInput(ToolInput):
    query_smiles: str = Field(description="Query molecule SMILES.")
    candidate_smiles_list: list[str] = Field(description="Candidate molecule SMILES strings.")
    threshold: float = Field(default=0.7, description="Tanimoto similarity threshold.")


@bioma_tool(
    name="find_similar_molecules_with_rdkit",
    description="Find molecules with Morgan-fingerprint Tanimoto similarity above a threshold.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=RDKitSimilarityInput,
    tags=["small-molecule", "rdkit", "similarity"],
)
def find_similar_molecules_with_rdkit(
    query_smiles: str,
    candidate_smiles_list: list[str],
    threshold: float = 0.7,
) -> str:
    """Find similar molecules with RDKit Morgan fingerprints."""
    try:
        Chem, _, AllChem, DataStructs = _load_rdkit()
        query = Chem.MolFromSmiles(query_smiles)
        if query is None:
            return _json({"status": "error", "error": "invalid query SMILES"})
        query_fp = AllChem.GetMorganFingerprintAsBitVect(query, 2, nBits=2048)
        hits = []
        for smiles in candidate_smiles_list:
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                continue
            sim = DataStructs.TanimotoSimilarity(query_fp, AllChem.GetMorganFingerprintAsBitVect(mol, 2, nBits=2048))
            if sim >= threshold:
                hits.append({"smiles": smiles, "similarity": round(float(sim), 4)})
        hits.sort(key=lambda item: item["similarity"], reverse=True)
        return _json({"status": "ok", "query_smiles": query_smiles, "threshold": threshold, "hits": hits})
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"RDKit unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class RDKitSubstructureInput(ToolInput):
    smiles_list: list[str] = Field(description="Molecule SMILES strings to search.")
    smarts_pattern: str = Field(description="SMARTS pattern.")


@bioma_tool(
    name="perform_substructure_search_with_rdkit",
    description="Search molecules containing a SMARTS substructure.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=RDKitSubstructureInput,
    tags=["small-molecule", "rdkit", "substructure"],
)
def perform_substructure_search_with_rdkit(smiles_list: list[str], smarts_pattern: str) -> str:
    """Perform SMARTS substructure search."""
    try:
        Chem, _, _, _ = _load_rdkit()
        query = Chem.MolFromSmarts(smarts_pattern)
        if query is None:
            return _json({"status": "error", "error": "invalid SMARTS"})
        hits = [smiles for smiles in smiles_list if (mol := Chem.MolFromSmiles(smiles)) is not None and mol.HasSubstructMatch(query)]
        return _json({"status": "ok", "smarts_pattern": smarts_pattern, "hits": hits})
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"RDKit unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class RDKit3DConformerInput(ToolInput):
    smiles_string: str = Field(description="Molecule SMILES string.")


@bioma_tool(
    name="generate_3d_conformation_with_rdkit",
    description="Generate and MMFF-optimize a 3D conformation from a SMILES string.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=RDKit3DConformerInput,
    tags=["small-molecule", "rdkit", "3d"],
)
def generate_3d_conformation_with_rdkit(smiles_string: str) -> str:
    """Generate an RDKit 3D conformer and return SDF content."""
    try:
        Chem, _, AllChem, _ = _load_rdkit()
        mol = Chem.MolFromSmiles(smiles_string)
        if mol is None:
            return _json({"status": "error", "error": "invalid SMILES"})
        mol = Chem.AddHs(mol)
        embed_status = AllChem.EmbedMolecule(mol, AllChem.ETKDG())
        if embed_status != 0:
            return _json({"status": "error", "error": f"embedding failed with status {embed_status}"})
        AllChem.MMFFOptimizeMolecule(mol)
        return _json({"status": "ok", "smiles": smiles_string, "sdf": Chem.MolToMolBlock(mol)})
    except ImportError as exc:
        return _json({"status": "unavailable", "error": f"RDKit unavailable: {exc}"})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


class FastaInput(ToolInput):
    fasta_content: str = Field(description="FASTA formatted content.")


@bioma_tool(
    name="extract_sequence_from_fasta",
    description="Extract a clean sequence string from FASTA content.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=FastaInput,
    tags=["sequence", "fasta"],
)
def extract_sequence_from_fasta(fasta_content: str) -> str:
    """Extract sequence letters from FASTA content."""
    lines = [line.strip() for line in (fasta_content or "").splitlines() if line.strip() and not line.startswith(">")]
    seq = re.sub(r"[^A-Za-z]", "", "".join(lines)).upper()
    if not seq:
        return _json({"status": "error", "error": "no sequence found"})
    return _json({"status": "ok", "sequence": seq, "length": len(seq)})


class FetchPDBByIdInput(ToolInput):
    pdb_id: str = Field(description="4-character PDB ID, e.g. 1A2C.")


@bioma_tool(
    name="fetch_pdb_by_id",
    description="Fetch a PDB file from RCSB by PDB ID.",
    category="database",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=FetchPDBByIdInput,
    requires_network=True,
    tags=["protein", "structure", "pdb"],
)
def fetch_pdb_by_id(pdb_id: str) -> str:
    """Fetch raw PDB text by PDB ID."""
    pdb_id_clean = pdb_id.strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{4}", pdb_id_clean):
        raise ToolError(
            "PDB ID must be four alphanumeric characters.",
            error_type="invalid_input",
            retriable=False,
            backend="RCSB PDB",
        )
    try:
        response = requests.get(f"https://files.rcsb.org/download/{pdb_id_clean}.pdb", timeout=300)
        if response.status_code == 404:
            raise ToolError(
                f"PDB entry {pdb_id_clean} was not found.",
                error_type="not_found",
                retriable=False,
                backend="RCSB PDB",
            )
        response.raise_for_status()
        return _json({
            "status": "ok",
            "pdb_id": pdb_id_clean,
            "line_count": response.text.count("\n"),
            "pdb_content": response.text,
        })
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="RCSB PDB") from exc


DRYLABS_API_BASE = os.getenv("PREDAGENT_DRYLABS_API_BASE", "http://119.191.58.66:8310")
DRYLABS_CALC_URL = os.getenv("PREDAGENT_DRYLABS_CALC_URL", f"{DRYLABS_API_BASE}/drylabs/calc")
DRYLABS_RESULT_URL = os.getenv("PREDAGENT_DRYLABS_RESULT_URL", f"{DRYLABS_API_BASE}/drylabs/result")


def _submit_and_poll_job(model_name: str, form_data: dict[str, Any], timeout: int, poll_interval: int) -> str:
    calc_payload = {"name": model_name, "form": json.dumps(form_data, ensure_ascii=False)}
    try:
        calc_response = requests.post(
            DRYLABS_CALC_URL,
            json=calc_payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        calc_response.raise_for_status()
        calc_data = calc_response.json()
        job_id = calc_data.get("job_id")
        if not job_id:
            raise ToolError(
                f"{model_name} did not return a job ID.",
                error_type="unavailable",
                retriable=True,
                backend=model_name,
            )

        result_response = requests.post(
            DRYLABS_RESULT_URL,
            json={"model": model_name, "job_id": job_id, "timeout": timeout, "poll_interval": poll_interval},
            headers={"Content-Type": "application/json"},
            timeout=timeout + 30,
        )
        result_response.raise_for_status()
        result_data = result_response.json()
        if result_data.get("status") != 0:
            raise ToolError(
                f"{model_name} job failed.",
                error_type="unavailable",
                retriable=False,
                backend=model_name,
            )
        return _json({"status": "ok", "model": model_name, "job_id": job_id, "result": result_data})
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend=model_name) from exc
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"{model_name} returned invalid JSON.",
            error_type="internal",
            retriable=False,
            backend=model_name,
        ) from exc


class ESMFPredictionInput(ToolInput):
    sequence: str = Field(description="Protein amino acid sequence.")
    num_recycles: int = Field(default=6, description="ESMFold recycle count.")
    timeout: int = Field(default=300, description="Maximum result wait time in seconds.")
    poll_interval: int = Field(default=10, description="Polling interval in seconds.")


@bioma_tool(
    name="esmf_predict_protein_structure",
    description="Call the configured ESMFold API to predict protein structure.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ESMFPredictionInput,
    tags=["protein", "structure", "esmf"],
    requires_network=True,
)
def esmf_predict_protein_structure(sequence: str, num_recycles: int = 6, timeout: int = 300, poll_interval: int = 10) -> str:
    seq = _validate_protein_sequence(sequence)
    form = {
        "Input Sequences": {"name": "输入序列 (Input Sequences)", "type": "str", "value": seq, "optional": False, "sequence": "aa"},
        "Number Recycles": {"name": "循环次数 (Number Recycles)", "type": "int", "value": num_recycles, "min": 1, "optional": True},
    }
    return _submit_and_poll_job("ESMFold", form, timeout, poll_interval)


class MMSEQS2MSAInput(ToolInput):
    sequence: str = Field(description="Protein amino acid sequence.")
    mode: Literal["unpaired", "paired", "unpaired_paired"] = Field(default="unpaired", description="MSA generation mode.")
    coverage: int = Field(default=50, ge=0, le=100, description="Minimum coverage percentage.")
    identity_threshold: int = Field(default=90, ge=0, le=100, description="Minimum identity percentage.")
    max_msa: int = Field(default=2048, description="Maximum number of MSA sequences.")
    timeout: int = Field(default=300, description="Maximum result wait time in seconds.")
    poll_interval: int = Field(default=10, description="Polling interval in seconds.")


@bioma_tool(
    name="mmseqs2_generate_msa",
    description="Call the configured MMseqs2 API to generate a multiple sequence alignment.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=MMSEQS2MSAInput,
    tags=["protein", "msa", "mmseqs2"],
    requires_network=True,
)
def mmseqs2_generate_msa(
    sequence: str,
    mode: Literal["unpaired", "paired", "unpaired_paired"] = "unpaired",
    coverage: int = 50,
    identity_threshold: int = 90,
    max_msa: int = 2048,
    timeout: int = 300,
    poll_interval: int = 10,
) -> str:
    seq = _validate_protein_sequence(sequence)
    form = {
        "Input Sequences": {"name": "输入序列 (Input Sequences)", "type": "str", "value": seq, "optional": False, "sequence": "aa"},
        "Mode": {"name": "模式 (Mode)", "type": "option", "value": mode, "options": "unpaired|paired|unpaired_paired", "optional": False},
        "Coverage": {"name": "覆盖率 (Coverage)", "type": "int", "value": coverage, "min": 0, "max": 100, "optional": True},
        "Identity threshold": {"name": "序列相似度阈值 (Identity threshold)", "type": "int", "value": identity_threshold, "min": 0, "max": 100, "optional": True},
        "Max MSA": {"name": "最大 MSA (Max MSA)", "type": "int", "value": max_msa, "optional": True},
    }
    return _submit_and_poll_job("mmseqs2 MSA Generation", form, timeout, poll_interval)


class SaProtPredictionInput(ToolInput):
    structure_url: str = Field(description="URL to a PDB structure file.")
    mode: Literal["Thermostability", "Metal Ion Binding", "Stability", "Binding Site"] = Field(description="SaProt prediction mode.")
    sequence: str = Field(default="", description="Protein sequence, required for Stability mode.")
    timeout: int = Field(default=300, description="Maximum result wait time in seconds.")
    poll_interval: int = Field(default=10, description="Polling interval in seconds.")


@bioma_tool(
    name="saprot_predict_function",
    description="Call SaProt API for structure-aware thermostability, stability, binding-site, or metal-binding prediction.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=SaProtPredictionInput,
    tags=["protein", "structure", "saprot", "stability"],
    requires_network=True,
)
def saprot_predict_function(
    structure_url: str,
    mode: Literal["Thermostability", "Metal Ion Binding", "Stability", "Binding Site"],
    sequence: str = "",
    timeout: int = 300,
    poll_interval: int = 10,
) -> str:
    form = {
        "Input Structure": {"name": "输入结构 (Input Structure)", "type": "file", "value": structure_url, "optional": False, "suffix": "pdb"},
        "Mode": {"name": "模式 (Mode)", "type": "option", "value": mode, "options": "Thermostability|Metal Ion Binding|Stability|Binding Site", "optional": False},
        "Stability Input Sequences": {"name": "Stability Input Sequences", "type": "str", "value": sequence, "optional": mode != "Stability", "sequence": "aa"},
    }
    return _submit_and_poll_job("SaProt", form, timeout, poll_interval)


class AMBERRelaxationInput(ToolInput):
    structure_url: str = Field(description="URL to a PDB or CIF structure file.")
    mode: Literal["default", "with_cif"] = Field(default="default", description="Relaxation input mode.")
    timeout: int = Field(default=600, description="Maximum result wait time in seconds.")
    poll_interval: int = Field(default=15, description="Polling interval in seconds.")


@bioma_tool(
    name="amber_relax_structure",
    description="Call AMBER Relaxation API to energy-minimize a protein structure.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=AMBERRelaxationInput,
    tags=["protein", "structure", "amber"],
    requires_network=True,
)
def amber_relax_structure(structure_url: str, mode: Literal["default", "with_cif"] = "default", timeout: int = 600, poll_interval: int = 15) -> str:
    form = {
        "Input Structure": {"name": "输入结构 (Input Structure)", "type": "file", "value": structure_url, "optional": False, "suffix": "pdb"},
        "Mode": {"name": "模式 (Mode)", "type": "option", "value": mode, "options": "default|with_cif", "optional": False},
    }
    return _submit_and_poll_job("AMBER Relaxation", form, timeout, poll_interval)


class EvoEF2EnergyCalculationInput(ToolInput):
    input_structure_pdb_url: str = Field(description="URL to a PDB structure file.")
    mode: Literal["Stability", "Binding"] = Field(description="EvoEF2 mode.")
    binding_chain_groups: str = Field(default="", description="Required for Binding mode, e.g. A,BC.")
    timeout: int = Field(default=300, description="Maximum result wait time in seconds.")
    poll_interval: int = Field(default=10, description="Polling interval in seconds.")


@bioma_tool(
    name="evoef2_calculate_energy",
    description="Call EvoEF2 API to calculate protein stability or binding energy.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=EvoEF2EnergyCalculationInput,
    tags=["protein", "structure", "evoef2", "energy"],
    requires_network=True,
)
def evoef2_calculate_energy(
    input_structure_pdb_url: str,
    mode: Literal["Stability", "Binding"],
    binding_chain_groups: str = "",
    timeout: int = 300,
    poll_interval: int = 10,
) -> str:
    if mode == "Binding" and not binding_chain_groups:
        raise ToolError(
            "binding_chain_groups is required for Binding mode.",
            error_type="invalid_input",
            retriable=False,
            backend="EvoEF2",
        )
    form = {
        "Input Structure": {"name": "输入结构 (Input Structure)", "type": "file", "value": input_structure_pdb_url, "optional": False, "suffix": "pdb"},
        "Mode": {"name": "模式 (Mode)", "type": "option", "value": mode, "options": "Stability|Binding", "optional": False},
        "Chain Groups for Binding Analysis (Optional)": {"name": "Chain Groups for Binding Analysis (Optional)", "type": "str", "value": binding_chain_groups, "optional": True},
    }
    return _submit_and_poll_job("EvoEF2", form, timeout, poll_interval)


class OmegaFoldPredictionInput(ToolInput):
    sequence: str = Field(description="Protein amino acid sequence.")
    number_cycles: int = Field(default=10, description="OmegaFold recycling cycles.")
    model_version: Literal["Version 2", "Version 1"] = Field(default="Version 2", description="OmegaFold model version.")
    timeout: int = Field(default=300, description="Maximum result wait time in seconds.")
    poll_interval: int = Field(default=10, description="Polling interval in seconds.")


@bioma_tool(
    name="omegafold_predict_structure",
    description="Call OmegaFold API to predict a protein 3D structure from sequence.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=OmegaFoldPredictionInput,
    tags=["protein", "structure", "omegafold"],
    requires_network=True,
)
def omegafold_predict_structure(
    sequence: str,
    number_cycles: int = 10,
    model_version: Literal["Version 2", "Version 1"] = "Version 2",
    timeout: int = 300,
    poll_interval: int = 10,
) -> str:
    seq = _validate_protein_sequence(sequence)
    form = {
        "Input Sequence": {"name": "输入序列 (Input Sequence)", "type": "str", "value": seq, "optional": False, "sequence": "aa"},
        "Number Cycles": {"name": "循环次数 (Number Cycles)", "type": "int", "value": number_cycles, "optional": False},
        "Model Version": {"name": "模型版本 (Model Version)", "type": "option", "value": model_version, "options": "Version 2|Version 1", "optional": True},
    }
    return _submit_and_poll_job("OmegaFold", form, timeout, poll_interval)


DEEPTP_API_BASE = os.getenv("PREDAGENT_DEEPTP_API_BASE", "http://219.223.200.129:8002")
EPHOD_API_BASE = os.getenv("PREDAGENT_EPHOD_API_BASE", "http://219.223.200.129:8003")
NETSOLP_API_BASE = os.getenv("PREDAGENT_NETSOLP_API_BASE", "http://219.223.200.129:8004")
PPTSTAB_API_BASE = os.getenv("PREDAGENT_PPTSTAB_API_BASE", "http://219.223.200.129:8005")


class DeepTPPredictionInput(ToolInput):
    uniprot_id: str = Field(description="Protein identifier.")
    sequence: str = Field(description="Protein amino acid sequence.")


@bioma_tool(
    name="deeptp_predict_protein_properties",
    description="Call DeepTP API for thermophilic protein prediction.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=DeepTPPredictionInput,
    tags=["protein", "thermostability", "deeptp"],
    requires_network=True,
)
def deeptp_predict_protein_properties(uniprot_id: str, sequence: str) -> str:
    seq = _validate_protein_sequence(sequence)
    try:
        response = requests.post(
            f"{DEEPTP_API_BASE}/predict_json",
            json={"data": [{"UniProt_id": uniprot_id, "Sequence": seq}]},
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        response.raise_for_status()
        return _json({"status": "ok", "tool": "DeepTP", "result": response.json()})
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="DeepTP") from exc


class EpHodPredictionInput(ToolInput):
    uniprot_id: str = Field(description="Protein identifier.")
    sequence: str = Field(description="Protein amino acid sequence.")


@bioma_tool(
    name="ephod_predict_optimal_ph",
    description="Call EpHod API to predict optimal pH for a protein sequence.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=EpHodPredictionInput,
    tags=["protein", "ph", "ephod"],
    requires_network=True,
)
def ephod_predict_optimal_ph(uniprot_id: str, sequence: str) -> str:
    seq = _validate_protein_sequence(sequence)
    try:
        response = requests.post(
            f"{EPHOD_API_BASE}/predict_sequences",
            json=[{"id": uniprot_id, "sequence": seq}],
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        response.raise_for_status()
        return _json({"status": "ok", "tool": "EpHod", "result": response.json()})
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="EpHod") from exc


class NetSolPPredictionInput(ToolInput):
    uniprot_id: str = Field(description="Protein identifier.")
    sequence: str = Field(description="Protein amino acid sequence.")


@bioma_tool(
    name="netsolp_predict_solubility",
    description="Call NetSolP API to predict protein solubility and usability.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=NetSolPPredictionInput,
    tags=["protein", "solubility", "netsolp"],
    requires_network=True,
)
def netsolp_predict_solubility(uniprot_id: str, sequence: str) -> str:
    seq = _validate_protein_sequence(sequence)
    payload = [{"id": uniprot_id, "sequence": seq}, {"id": "dummy_placeholder", "sequence": seq}]
    try:
        response = requests.post(
            f"{NETSOLP_API_BASE}/predict",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        target = next((item for item in data if item.get("id") == uniprot_id), None) if isinstance(data, list) else None
        return _json({"status": "ok", "tool": "NetSolP", "target": target, "raw_result": data})
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="NetSolP") from exc


class PPTStabPredictionInput(ToolInput):
    uniprot_id: str = Field(description="Protein identifier.")
    sequence: str = Field(description="Protein amino acid sequence.")


@bioma_tool(
    name="pptstab_predict_tm",
    description="Call PPTStab API to predict protein melting temperature Tm in Celsius.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=PPTStabPredictionInput,
    tags=["protein", "tm", "thermal-stability", "pptstab"],
    requires_network=True,
)
def pptstab_predict_tm(uniprot_id: str, sequence: str) -> str:
    seq = _validate_protein_sequence(sequence)
    try:
        response = requests.post(
            f"{PPTSTAB_API_BASE}/predict",
            json={"sequences": [seq]},
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        tm = None
        if isinstance(data, dict) and data.get("status") == "success" and data.get("results"):
            tm = data["results"][0].get("tm")
        return _json({"status": "ok", "tool": "PPTStab", "uniprot_id": uniprot_id, "predicted_tm_c": tm, "raw_result": data})
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="PPTStab") from exc


class DevelopabilityMatrixInput(ToolInput):
    matrix_csv_path: str = Field(description="Path to a candidate matrix CSV containing candidate_id and full_sequence.")
    output_csv_path: str = Field(default="", description="Optional output CSV path. Can be identical to matrix_csv_path.")
    tools_csv: str = Field(
        default="pptstab,netsolp,deeptp,ephod",
        description="Comma-separated sequence-level tools to run: pptstab,netsolp,deeptp,ephod.",
    )
    include_raw_json: bool = Field(default=False, description="Whether to include full raw tool JSON in the output CSV.")
    request_delay_seconds: float = Field(default=0.0, description="Optional delay between candidates to avoid overloading APIs.")
    target_ph: float = Field(default=7.5, description="Target assay/operation pH used to interpret EpHod optimal pH.")


@bioma_tool(
    name="evaluate_mutation_matrix_with_developability_tools",
    description=(
        "Batch-score a protein candidate matrix with sequence-level developability tools: "
        "PPTStab Tm, NetSolP solubility/usability, DeepTP thermophilic tendency, and EpHod optimal pH."
    ),
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=DevelopabilityMatrixInput,
    tags=["protein", "developability", "tm", "solubility", "ph"],
    requires_network=True,
)
def evaluate_mutation_matrix_with_developability_tools(
    matrix_csv_path: str,
    output_csv_path: str = "",
    tools_csv: str = "pptstab,netsolp,deeptp,ephod",
    include_raw_json: bool = False,
    request_delay_seconds: float = 0.0,
    target_ph: float = 7.5,
) -> str:
    """Run real sequence-level developability APIs for every matrix row and write an enriched CSV."""
    matrix_path = Path(matrix_csv_path)
    if not matrix_path.exists():
        return _json({"status": "error", "error": f"matrix_csv_path not found: {matrix_csv_path}"})

    with matrix_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        input_fieldnames = reader.fieldnames or []

    required = {"candidate_id", "full_sequence"}
    missing = sorted(required - set(input_fieldnames))
    if missing:
        return _json({"status": "error", "error": f"matrix CSV missing required columns: {', '.join(missing)}"})
    if not rows:
        return _json({"status": "error", "error": "matrix CSV has no rows"})

    requested = [item.strip().lower() for item in re.split(r"[,;\s]+", tools_csv or "") if item.strip()]
    valid_tools = {"pptstab", "netsolp", "deeptp", "ephod"}
    unknown = sorted(set(requested) - valid_tools)
    if unknown:
        return _json({"status": "error", "error": f"unknown developability tools: {', '.join(unknown)}"})
    if not requested:
        requested = ["pptstab", "netsolp", "deeptp", "ephod"]

    try:
        baseline_id, baseline_sequence = _infer_developability_baseline(rows)
    except ValueError as exc:
        return _json({"status": "error", "error": f"failed to infer backbone baseline: {exc}"})

    baseline = _predict_developability_metrics(
        sequence_id=baseline_id,
        sequence=baseline_sequence,
        requested=requested,
        target_ph=target_ph,
    )

    output_rows: list[dict[str, str]] = []
    compact_results: list[dict[str, Any]] = []
    status_counts = {"ok": 0, "partial": 0, "error": 0}

    for index, row in enumerate(rows, start=1):
        out = dict(row)
        candidate_id = str(row.get("candidate_id", "")).strip() or f"candidate_{index}"
        sequence = str(row.get("full_sequence", "")).strip().upper()
        raw: dict[str, Any] = {}
        compact: dict[str, Any] = {"candidate_id": candidate_id}

        if not sequence:
            _set_developability_error(out, "missing full_sequence")
            status_counts["error"] += 1
            output_rows.append(out)
            compact_results.append({"candidate_id": candidate_id, "status": "error", "error": "missing full_sequence"})
            continue

        if "pptstab" in requested:
            payload = _call_json_tool(pptstab_predict_tm, uniprot_id=candidate_id, sequence=sequence)
            raw["pptstab"] = payload
            out["pptstab_status"] = _status_from_payload(payload)
            out["pptstab_tm_c"] = _stringify_number(_extract_number(payload, preferred_keys=["predicted_tm_c"], include_terms=["tm"]))
            out["baseline_pptstab_tm_c"] = baseline.get("pptstab_tm_c", "")
            out["delta_pptstab_tm_c"] = _delta_string(out["pptstab_tm_c"], out["baseline_pptstab_tm_c"])
            compact["pptstab_tm_c"] = out["pptstab_tm_c"]
            compact["delta_pptstab_tm_c"] = out["delta_pptstab_tm_c"]
            compact["pptstab_status"] = out["pptstab_status"]

        if "netsolp" in requested:
            payload = _call_json_tool(netsolp_predict_solubility, uniprot_id=candidate_id, sequence=sequence)
            raw["netsolp"] = payload
            out["netsolp_status"] = _status_from_payload(payload)
            out["netsolp_solubility"] = _stringify_number(_extract_number(payload, include_terms=["solubility", "soluble"]))
            out["netsolp_usability"] = _stringify_number(_extract_number(payload, include_terms=["usability", "useability", "usable"]))
            out["baseline_netsolp_solubility"] = baseline.get("netsolp_solubility", "")
            out["delta_netsolp_solubility"] = _delta_string(out["netsolp_solubility"], out["baseline_netsolp_solubility"])
            out["baseline_netsolp_usability"] = baseline.get("netsolp_usability", "")
            out["delta_netsolp_usability"] = _delta_string(out["netsolp_usability"], out["baseline_netsolp_usability"])
            compact["netsolp_solubility"] = out["netsolp_solubility"]
            compact["delta_netsolp_solubility"] = out["delta_netsolp_solubility"]
            compact["netsolp_usability"] = out["netsolp_usability"]
            compact["delta_netsolp_usability"] = out["delta_netsolp_usability"]
            compact["netsolp_status"] = out["netsolp_status"]

        if "deeptp" in requested:
            payload = _call_json_tool(deeptp_predict_protein_properties, uniprot_id=candidate_id, sequence=sequence)
            raw["deeptp"] = payload
            out["deeptp_status"] = _status_from_payload(payload)
            out["deeptp_score"] = _stringify_number(_extract_number(payload, include_terms=["score", "probability", "confidence"]))
            out["deeptp_label"] = _extract_scalar_text(payload, include_terms=["label", "class", "prediction"])
            out["baseline_deeptp_score"] = baseline.get("deeptp_score", "")
            out["delta_deeptp_score"] = _delta_string(out["deeptp_score"], out["baseline_deeptp_score"])
            out["baseline_deeptp_label"] = baseline.get("deeptp_label", "")
            compact["deeptp_score"] = out["deeptp_score"]
            compact["delta_deeptp_score"] = out["delta_deeptp_score"]
            compact["deeptp_label"] = out["deeptp_label"]
            compact["deeptp_status"] = out["deeptp_status"]

        if "ephod" in requested:
            payload = _call_json_tool(ephod_predict_optimal_ph, uniprot_id=candidate_id, sequence=sequence)
            raw["ephod"] = payload
            out["ephod_status"] = _status_from_payload(payload)
            out["ephod_optimal_ph"] = _stringify_number(_extract_number(payload, include_terms=["ph"]))
            out["baseline_ephod_optimal_ph"] = baseline.get("ephod_optimal_ph", "")
            out["delta_ephod_optimal_ph"] = _delta_string(out["ephod_optimal_ph"], out["baseline_ephod_optimal_ph"])
            out["ephod_target_ph"] = _stringify_number(target_ph)
            out["ephod_target_distance"] = _distance_string(out["ephod_optimal_ph"], target_ph)
            out["baseline_ephod_target_distance"] = baseline.get("ephod_target_distance", "")
            out["delta_ephod_target_distance"] = _delta_string(
                out["ephod_target_distance"],
                out["baseline_ephod_target_distance"],
            )
            compact["ephod_optimal_ph"] = out["ephod_optimal_ph"]
            compact["delta_ephod_target_distance"] = out["delta_ephod_target_distance"]
            compact["ephod_status"] = out["ephod_status"]

        statuses = [
            str(out.get(f"{tool}_status", "")).strip()
            for tool in requested
            if str(out.get(f"{tool}_status", "")).strip()
        ]
        if statuses and all(status == "ok" for status in statuses):
            developability_status = "ok"
        elif any(status == "ok" for status in statuses):
            developability_status = "partial"
        else:
            developability_status = "error"
        status_counts[developability_status] += 1
        out["developability_status"] = developability_status
        out["developability_baseline_id"] = baseline_id
        out["developability_note"] = (
            "Properties are compared against the inferred R6-6H09 baseline. "
            "delta_pptstab_tm_c > 0 supports predicted thermal improvement; "
            "negative NetSolP deltas are developability guardrail risks; "
            "delta_ephod_target_distance < 0 means candidate pH is closer to target pH."
        )
        out["developability_raw_json"] = json.dumps(raw if include_raw_json else compact, ensure_ascii=False, separators=(",", ":"))

        output_rows.append(out)
        compact["developability_status"] = developability_status
        compact_results.append(compact)

        if request_delay_seconds > 0 and index < len(rows):
            time.sleep(request_delay_seconds)

    extra_fieldnames = [
        "developability_status",
        "developability_baseline_id",
        "pptstab_status",
        "pptstab_tm_c",
        "baseline_pptstab_tm_c",
        "delta_pptstab_tm_c",
        "netsolp_status",
        "netsolp_solubility",
        "baseline_netsolp_solubility",
        "delta_netsolp_solubility",
        "netsolp_usability",
        "baseline_netsolp_usability",
        "delta_netsolp_usability",
        "deeptp_status",
        "deeptp_score",
        "baseline_deeptp_score",
        "delta_deeptp_score",
        "deeptp_label",
        "baseline_deeptp_label",
        "ephod_status",
        "ephod_optimal_ph",
        "baseline_ephod_optimal_ph",
        "delta_ephod_optimal_ph",
        "ephod_target_ph",
        "ephod_target_distance",
        "baseline_ephod_target_distance",
        "delta_ephod_target_distance",
        "developability_note",
        "developability_raw_json",
    ]
    fieldnames = list(input_fieldnames)
    for name in extra_fieldnames:
        if name not in fieldnames:
            fieldnames.append(name)

    out_path = Path(output_csv_path or matrix_csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)

    overall = "ok" if status_counts["error"] == 0 and status_counts["partial"] == 0 else "partial" if status_counts["ok"] or status_counts["partial"] else "error"
    return _json({
        "status": overall,
        "rows_scored": len(output_rows),
        "requested_tools": requested,
        "baseline_id": baseline_id,
        "target_ph": target_ph,
        "baseline_metrics": baseline,
        "status_counts": status_counts,
        "output_csv_path": str(out_path),
        "csv_columns_added": extra_fieldnames,
        "compact_results": compact_results,
        "note": "These are model/API predictions compared against the inferred R6-6H09 baseline, not experimental Tm, expression, solubility, pH, or ΔΔG measurements.",
    })


def _set_developability_error(row: dict[str, str], error: str) -> None:
    row["developability_status"] = "error"
    row["developability_note"] = error
    for key in [
        "developability_baseline_id",
        "pptstab_status",
        "pptstab_tm_c",
        "baseline_pptstab_tm_c",
        "delta_pptstab_tm_c",
        "netsolp_status",
        "netsolp_solubility",
        "baseline_netsolp_solubility",
        "delta_netsolp_solubility",
        "netsolp_usability",
        "baseline_netsolp_usability",
        "delta_netsolp_usability",
        "deeptp_status",
        "deeptp_score",
        "baseline_deeptp_score",
        "delta_deeptp_score",
        "deeptp_label",
        "baseline_deeptp_label",
        "ephod_status",
        "ephod_optimal_ph",
        "baseline_ephod_optimal_ph",
        "delta_ephod_optimal_ph",
        "ephod_target_ph",
        "ephod_target_distance",
        "baseline_ephod_target_distance",
        "delta_ephod_target_distance",
        "developability_raw_json",
    ]:
        row[key] = ""


def _infer_developability_baseline(rows: list[dict[str, str]]) -> tuple[str, str]:
    mutation_re = re.compile(r"^([A-Z])(\d+)([A-Z])$")
    baseline_id = str(rows[0].get("backbone_id", "r6_6h09")).strip() or "r6_6h09"
    inferred: str | None = None
    for index, row in enumerate(rows, start=2):
        sequence = str(row.get("full_sequence", "")).strip().upper()
        if not sequence:
            continue
        mutations = _parse_developability_mutations(row.get("mutations_vs_backbone", ""), mutation_re)
        candidate_baseline = _revert_mutations(sequence, mutations, mutation_re)
        if inferred is None:
            inferred = candidate_baseline
        elif inferred != candidate_baseline:
            raise ValueError(f"row {index}: inconsistent inferred baseline sequence")
    if inferred is None:
        raise ValueError("no usable full_sequence found")
    return baseline_id, inferred


def _parse_developability_mutations(raw: str, mutation_re) -> list[str]:
    text = str(raw or "").strip()
    if not text or text.lower() in {"none", "na", "n/a", "-"}:
        return []
    mutations = [item.strip() for item in re.split(r"[,;]\s*|\s+", text) if item.strip()]
    for mutation in mutations:
        if mutation_re.match(mutation) is None:
            raise ValueError(f"invalid mutation format: {mutation}")
    return mutations


def _revert_mutations(sequence: str, mutations: list[str], mutation_re) -> str:
    seq = list(sequence)
    for mutation in mutations:
        match = mutation_re.match(mutation)
        if match is None:
            raise ValueError(f"invalid mutation format: {mutation}")
        wt_aa, pos_text, mut_aa = match.groups()
        idx = int(pos_text) - 1
        if idx < 0 or idx >= len(seq):
            raise ValueError(f"{mutation} position out of range for sequence length {len(seq)}")
        if seq[idx] != mut_aa:
            raise ValueError(f"{mutation} mutant mismatch: full_sequence has {seq[idx]}{idx + 1}, not {mut_aa}{idx + 1}")
        seq[idx] = wt_aa
    return "".join(seq)


def _predict_developability_metrics(
    sequence_id: str,
    sequence: str,
    requested: list[str],
    target_ph: float,
) -> dict[str, str]:
    metrics: dict[str, str] = {}
    if "pptstab" in requested:
        payload = _call_json_tool(pptstab_predict_tm, uniprot_id=f"{sequence_id}_baseline", sequence=sequence)
        metrics["pptstab_status"] = _status_from_payload(payload)
        metrics["pptstab_tm_c"] = _stringify_number(_extract_number(payload, preferred_keys=["predicted_tm_c"], include_terms=["tm"]))
    if "netsolp" in requested:
        payload = _call_json_tool(netsolp_predict_solubility, uniprot_id=f"{sequence_id}_baseline", sequence=sequence)
        metrics["netsolp_status"] = _status_from_payload(payload)
        metrics["netsolp_solubility"] = _stringify_number(_extract_number(payload, include_terms=["solubility", "soluble"]))
        metrics["netsolp_usability"] = _stringify_number(_extract_number(payload, include_terms=["usability", "useability", "usable"]))
    if "deeptp" in requested:
        payload = _call_json_tool(deeptp_predict_protein_properties, uniprot_id=f"{sequence_id}_baseline", sequence=sequence)
        metrics["deeptp_status"] = _status_from_payload(payload)
        metrics["deeptp_score"] = _stringify_number(_extract_number(payload, include_terms=["score", "probability", "confidence"]))
        metrics["deeptp_label"] = _extract_scalar_text(payload, include_terms=["label", "class", "prediction"])
    if "ephod" in requested:
        payload = _call_json_tool(ephod_predict_optimal_ph, uniprot_id=f"{sequence_id}_baseline", sequence=sequence)
        metrics["ephod_status"] = _status_from_payload(payload)
        metrics["ephod_optimal_ph"] = _stringify_number(_extract_number(payload, include_terms=["ph"]))
        metrics["ephod_target_distance"] = _distance_string(metrics["ephod_optimal_ph"], target_ph)
    return metrics


def _call_json_tool(func, **kwargs: Any) -> Any:
    try:
        result = func(**kwargs)
        return json.loads(result) if isinstance(result, str) else result
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"invalid JSON from {func.__name__}: {exc}"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _status_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        status = str(payload.get("status", "")).strip()
        return status or "unknown"
    return "unknown"


def _iter_scalars(payload: Any, path: tuple[str, ...] = ()):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _iter_scalars(value, (*path, str(key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from _iter_scalars(value, (*path, str(index)))
    else:
        yield path, payload


def _extract_number(payload: Any, preferred_keys: list[str] | None = None, include_terms: list[str] | None = None) -> float | None:
    preferred = {key.lower() for key in (preferred_keys or [])}
    terms = [term.lower() for term in (include_terms or [])]
    fallback: float | None = None
    for path, value in _iter_scalars(payload):
        key = path[-1].lower() if path else ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if preferred and key in preferred:
            return number
        joined = "_".join(item.lower() for item in path)
        if terms and any(term in joined for term in terms):
            if fallback is None:
                fallback = number
    return fallback


def _extract_scalar_text(payload: Any, include_terms: list[str]) -> str:
    terms = [term.lower() for term in include_terms]
    for path, value in _iter_scalars(payload):
        if not isinstance(value, str):
            continue
        joined = "_".join(item.lower() for item in path)
        if any(term in joined for term in terms) and len(value) <= 80:
            return value
    return ""


def _stringify_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _number_from_string(value: str) -> float | None:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except ValueError:
        return None


def _delta_string(candidate: str, baseline: str) -> str:
    candidate_value = _number_from_string(candidate)
    baseline_value = _number_from_string(baseline)
    if candidate_value is None or baseline_value is None:
        return ""
    return _stringify_number(candidate_value - baseline_value)


def _distance_string(value: str, target: float) -> str:
    number = _number_from_string(value)
    if number is None:
        return ""
    return _stringify_number(abs(number - target))



class FoldStructureMatrixInput(ToolInput):
    matrix_csv_path: str = Field(description="Path to a candidate matrix CSV containing candidate_id and full_sequence.")
    output_csv_path: str = Field(description="Output CSV path for folded/downloaded structures.")
    structure_output_dir: str = Field(description="Directory for downloaded baseline and candidate PDB files.")
    fold_tool: Literal["esmf", "omegafold"] = Field(default="esmf", description="Structure prediction tool to use.")
    timeout: int = Field(default=600, description="Maximum result wait time in seconds for each remote fold job.")
    poll_interval: int = Field(default=15, description="Polling interval in seconds for remote jobs.")


@bioma_tool(
    name="fold_mutation_matrix_structures",
    description="Fold a small protein candidate shortlist and download baseline/candidate PDB files.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=FoldStructureMatrixInput,
    tags=["protein", "structure", "fold", "pdb"],
    requires_network=True,
)
def fold_mutation_matrix_structures(
    matrix_csv_path: str,
    output_csv_path: str,
    structure_output_dir: str,
    fold_tool: Literal["esmf", "omegafold"] = "esmf",
    timeout: int = 600,
    poll_interval: int = 15,
) -> str:
    """Freshly fold baseline and candidates, then persist downloaded PDB files."""
    matrix_path = Path(matrix_csv_path)
    if not matrix_path.exists():
        return _json({"status": "error", "error": f"matrix_csv_path not found: {matrix_csv_path}"})

    with matrix_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        input_fieldnames = reader.fieldnames or []
    if "candidate_id" not in input_fieldnames or "full_sequence" not in input_fieldnames:
        return _json({"status": "error", "error": "matrix CSV must contain candidate_id and full_sequence"})
    if not rows:
        return _json({"status": "error", "error": "matrix CSV has no rows"})

    out_path = Path(output_csv_path)
    pdb_dir = Path(structure_output_dir)
    pdb_dir.mkdir(parents=True, exist_ok=True)

    extra_fieldnames = [
        "fold_tool",
        "fold_status",
        "fold_structure_url",
        "fold_pdb_path",
        "baseline_fold_structure_url",
        "baseline_fold_pdb_path",
        "fold_note",
        "fold_raw_json",
    ]
    fieldnames = list(input_fieldnames)
    for name in extra_fieldnames:
        if name not in fieldnames:
            fieldnames.append(name)

    input_ids = [str(row.get("candidate_id", "")).strip() or f"candidate_{index}" for index, row in enumerate(rows, start=1)]
    output_by_id: dict[str, dict[str, str]] = {}

    try:
        baseline_id, baseline_sequence = _infer_developability_baseline(rows)
    except Exception as exc:
        return _json({"status": "error", "error": f"failed to infer R6-6H09 baseline sequence: {exc}"})
    baseline_pdb_path = str(pdb_dir / f"{_safe_filename(baseline_id)}_baseline.pdb")
    baseline_payload: Any
    baseline_url = ""
    baseline_raw: dict[str, Any] = {}
    try:
        baseline_payload = _call_fold_tool(fold_tool, baseline_sequence, timeout, poll_interval)
        baseline_url = _extract_url(baseline_payload)
        baseline_raw["fold"] = baseline_payload
        if baseline_url:
            try:
                _download_url_to_path(baseline_url, Path(baseline_pdb_path))
            except Exception as exc:
                baseline_raw["download_error"] = str(exc)
    except Exception as exc:
        baseline_payload = {"status": "error", "error": str(exc)}
        baseline_raw["fold_error"] = str(exc)

    def candidate_pdb_path(candidate_id: str) -> str:
        return str(pdb_dir / f"{_safe_filename(candidate_id)}.pdb")

    def fold_row_complete(row: dict[str, str]) -> bool:
        path = str(row.get("fold_pdb_path", "")).strip()
        return bool(path and Path(path).exists())

    def flush_output() -> None:
        ordered_rows = [output_by_id[candidate_id] for candidate_id in input_ids if candidate_id in output_by_id]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(ordered_rows)

    compact: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        candidate_id = str(row.get("candidate_id", "")).strip() or f"candidate_{index}"
        out = dict(row)
        sequence = str(row.get("full_sequence", "")).strip().upper()
        structure_url = ""
        fold_status = "error"
        fold_pdb_path = candidate_pdb_path(candidate_id)
        raw: dict[str, Any] = {}

        if not sequence:
            raw["error"] = "missing full_sequence"
        else:
            try:
                fold_payload = _call_fold_tool(fold_tool, sequence, timeout, poll_interval)
                raw["fold"] = fold_payload
                structure_url = _extract_url(fold_payload)
                if structure_url:
                    try:
                        _download_url_to_path(structure_url, Path(fold_pdb_path))
                    except Exception as exc:
                        raw["download_error"] = str(exc)
                fold_status = "ok" if _status_from_payload(fold_payload) == "ok" and Path(fold_pdb_path).exists() else "error"
            except Exception as exc:
                raw["fold_error"] = str(exc)

        out.update({
            "fold_tool": fold_tool,
            "fold_status": fold_status,
            "fold_structure_url": structure_url,
            "fold_pdb_path": fold_pdb_path if Path(fold_pdb_path).exists() else "",
            "baseline_fold_structure_url": baseline_url,
            "baseline_fold_pdb_path": baseline_pdb_path if Path(baseline_pdb_path).exists() else "",
            "fold_note": "Fold/download only. pLDDT and EvoEF2 are computed in step 3.6.",
            "fold_raw_json": json.dumps({
                "candidate_id": candidate_id,
                "fold_status": fold_status,
                "fold_structure_url": structure_url,
                "fold_pdb_path": fold_pdb_path if Path(fold_pdb_path).exists() else "",
                "baseline_fold_structure_url": baseline_url,
                "baseline_fold_pdb_path": baseline_pdb_path if Path(baseline_pdb_path).exists() else "",
                "baseline_fold_status": _status_from_payload(baseline_payload),
                "raw": _compact_payload({"candidate": raw, "baseline": baseline_raw}),
            }, ensure_ascii=False, separators=(",", ":")),
        })
        output_by_id[candidate_id] = out
        compact.append({
            "candidate_id": candidate_id,
            "fold_status": fold_status,
            "fold_structure_url": structure_url,
            "fold_pdb_path": out["fold_pdb_path"],
        })
        flush_output()

    final_rows = [output_by_id[candidate_id] for candidate_id in input_ids if candidate_id in output_by_id]
    completed_final = [row for row in final_rows if fold_row_complete(row)]
    baseline_fold_status = "ok" if Path(baseline_pdb_path).exists() else "error"
    return _json({
        "status": "ok" if len(completed_final) == len(rows) and baseline_fold_status == "ok" else "partial",
        "rows_total": len(rows),
        "rows_folded": len(completed_final),
        "rows_processed_this_call": len(rows),
        "rows_remaining": len(rows) - len(completed_final),
        "structure_output_dir": str(pdb_dir),
        "baseline": {
            "baseline_id": baseline_id,
            "fold_status": baseline_fold_status,
            "fold_structure_url": baseline_url,
            "fold_pdb_path": baseline_pdb_path if Path(baseline_pdb_path).exists() else "",
        },
        "output_csv_path": str(out_path),
        "compact_results": compact,
    })


class ScoreFoldedStructureMatrixInput(ToolInput):
    folded_csv_path: str = Field(description="Path to step 3.5 folded-structure CSV.")
    output_csv_path: str = Field(description="Output CSV path for structure-scored candidates.")
    structure_output_dir: str = Field(description="Directory containing downloaded baseline and candidate PDB files.")
    run_evoef2: bool = Field(default=True, description="Run EvoEF2 stability energy when a fold_structure_url is available. If the URL is missing or expired, keep pLDDT results and record EvoEF2 as unavailable/error.")
    max_candidates: int = Field(default=0, description="Maximum number of not-yet-scored candidate rows to process in this call. 0 means all remaining rows.")
    resume: bool = Field(default=True, description="Reuse existing output rows with pLDDT and, when possible, EvoEF2 results.")
    timeout: int = Field(default=600, description="Maximum result wait time in seconds for each remote EvoEF2 job.")
    poll_interval: int = Field(default=15, description="Polling interval in seconds for remote jobs.")


@bioma_tool(
    name="score_folded_mutation_matrix_structures",
    description="Score already-folded candidate PDBs by pLDDT and optional EvoEF2 without refolding.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ScoreFoldedStructureMatrixInput,
    tags=["protein", "structure", "plddt", "evoef2"],
    requires_network=True,
)
def score_folded_mutation_matrix_structures(
    folded_csv_path: str,
    output_csv_path: str,
    structure_output_dir: str,
    run_evoef2: bool = True,
    max_candidates: int = 0,
    resume: bool = True,
    timeout: int = 600,
    poll_interval: int = 15,
) -> str:
    """Score local folded PDB files without calling fold tools."""
    folded_path = Path(folded_csv_path)
    if not folded_path.exists():
        return _json({"status": "error", "error": f"folded_csv_path not found: {folded_csv_path}"})

    with folded_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        input_fieldnames = reader.fieldnames or []
    if "candidate_id" not in input_fieldnames:
        return _json({"status": "error", "error": "folded CSV must contain candidate_id"})
    if not rows:
        return _json({"status": "error", "error": "folded CSV has no rows"})

    out_path = Path(output_csv_path)
    pdb_dir = Path(structure_output_dir)
    extra_fieldnames = [
        "structure_status",
        "fold_plddt_ca_count",
        "fold_plddt_mean",
        "fold_plddt_min",
        "fold_plddt_max",
        "fold_plddt_pct_lt50",
        "fold_plddt_pct_ge70",
        "baseline_fold_plddt_ca_count",
        "baseline_fold_plddt_mean",
        "baseline_fold_plddt_min",
        "baseline_fold_plddt_max",
        "baseline_fold_plddt_pct_lt50",
        "baseline_fold_plddt_pct_ge70",
        "delta_plddt_mean",
        "delta_plddt_pct_lt50",
        "delta_plddt_pct_ge70",
        "evoef2_status",
        "evoef2_output_url",
        "evoef2_energy",
        "baseline_evoef2_status",
        "baseline_evoef2_output_url",
        "baseline_evoef2_energy",
        "delta_evoef2_energy",
        "structure_risk_flags",
        "structure_note",
        "structure_raw_json",
    ]
    fieldnames = list(input_fieldnames)
    for name in extra_fieldnames:
        if name not in fieldnames:
            fieldnames.append(name)

    input_ids = [str(row.get("candidate_id", "")).strip() or f"candidate_{index}" for index, row in enumerate(rows, start=1)]
    output_by_id: dict[str, dict[str, str]] = {}
    if resume and out_path.exists():
        with out_path.open(newline="", encoding="utf-8-sig") as handle:
            for loaded in csv.DictReader(handle):
                candidate_id = str(loaded.get("candidate_id", "")).strip()
                if candidate_id:
                    output_by_id[candidate_id] = dict(loaded)

    baseline_pdb_path = ""
    baseline_url = ""
    for row in list(output_by_id.values()) + rows:
        baseline_url = baseline_url or str(row.get("baseline_fold_structure_url", "")).strip()
        candidate_path = str(row.get("baseline_fold_pdb_path", "")).strip()
        if candidate_path and Path(candidate_path).exists():
            baseline_pdb_path = candidate_path
            break
    if not baseline_pdb_path:
        default_baseline_path = pdb_dir / "r6_6h09_baseline.pdb"
        if default_baseline_path.exists():
            baseline_pdb_path = str(default_baseline_path)
    baseline_plddt = _empty_plddt_metrics()
    if baseline_pdb_path:
        baseline_plddt = _plddt_metrics_from_pdb(Path(baseline_pdb_path).read_text(encoding="utf-8", errors="replace"))

    baseline_evoef2_status = ""
    baseline_evoef2_output_url = ""
    baseline_evoef2_energy = ""
    baseline_evoef2_raw: dict[str, Any] = {}
    for row in output_by_id.values():
        baseline_evoef2_status = baseline_evoef2_status or str(row.get("baseline_evoef2_status", "")).strip()
        baseline_evoef2_output_url = baseline_evoef2_output_url or str(row.get("baseline_evoef2_output_url", "")).strip()
        baseline_evoef2_energy = baseline_evoef2_energy or str(row.get("baseline_evoef2_energy", "")).strip()
    if run_evoef2:
        baseline_evoef2_status, baseline_evoef2_output_url, baseline_evoef2_energy = _resolve_evoef2_energy(
            structure_url=baseline_url,
            existing_status=baseline_evoef2_status,
            existing_output_url=baseline_evoef2_output_url,
            existing_energy=baseline_evoef2_energy,
            timeout=timeout,
            poll_interval=poll_interval,
            raw=baseline_evoef2_raw,
        )

    def candidate_pdb_path(candidate_id: str) -> str:
        return str(pdb_dir / f"{_safe_filename(candidate_id)}.pdb")

    def score_row_complete(row: dict[str, str]) -> bool:
        if not str(row.get("fold_pdb_path", "")).strip() or not str(row.get("fold_plddt_mean", "")).strip():
            return False
        if run_evoef2:
            baseline_status = str(row.get("baseline_evoef2_status", "")).strip()
            baseline_energy = str(row.get("baseline_evoef2_energy", "")).strip()
            if not baseline_status:
                return False
            if baseline_status == "ok" and not baseline_energy:
                return False
            evoef2_status = str(row.get("evoef2_status", "")).strip()
            evoef2_energy = str(row.get("evoef2_energy", "")).strip()
            if not evoef2_status:
                return False
            if evoef2_status == "ok" and not evoef2_energy:
                return False
        return True

    completed_ids = {candidate_id for candidate_id, row in output_by_id.items() if score_row_complete(row)}
    rows_to_process = [
        row for index, row in enumerate(rows, start=1)
        if (str(row.get("candidate_id", "")).strip() or f"candidate_{index}") not in completed_ids
    ]
    if max_candidates > 0:
        rows_to_process = rows_to_process[:max_candidates]

    def flush_output() -> None:
        ordered_rows = [output_by_id[candidate_id] for candidate_id in input_ids if candidate_id in output_by_id]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(ordered_rows)

    compact: list[dict[str, Any]] = []
    for index, row in enumerate(rows_to_process, start=1):
        candidate_id = str(row.get("candidate_id", "")).strip() or f"candidate_{index}"
        existing = output_by_id.get(candidate_id, {})
        out = dict(row)
        out.update({key: value for key, value in existing.items() if key in fieldnames and value})
        fold_pdb_path = str(out.get("fold_pdb_path", "")).strip()
        if not fold_pdb_path or not Path(fold_pdb_path).exists():
            fold_pdb_path = candidate_pdb_path(candidate_id)
        structure_url = str(out.get("fold_structure_url", "")).strip()
        risk_flags: list[str] = []
        raw: dict[str, Any] = {}

        fold_plddt = _empty_plddt_metrics()
        if fold_pdb_path and Path(fold_pdb_path).exists():
            fold_plddt = _plddt_metrics_from_pdb(Path(fold_pdb_path).read_text(encoding="utf-8", errors="replace"))
            plddt_mean = _number_from_string(fold_plddt["plddt_mean"])
            if plddt_mean is not None and plddt_mean < 50:
                risk_flags.append("low_plddt_mean")
        else:
            risk_flags.append("missing_fold_pdb")

        evoef2_status = str(out.get("evoef2_status", "")).strip()
        evoef2_output_url = str(out.get("evoef2_output_url", "")).strip()
        evoef2_energy = str(out.get("evoef2_energy", "")).strip()
        if run_evoef2:
            evoef2_status, evoef2_output_url, evoef2_energy = _resolve_evoef2_energy(
                structure_url=structure_url,
                existing_status=evoef2_status,
                existing_output_url=evoef2_output_url,
                existing_energy=evoef2_energy,
                timeout=timeout,
                poll_interval=poll_interval,
                raw=raw,
            )
            if evoef2_status == "unavailable_missing_url":
                risk_flags.append("evoef2_unavailable_missing_url")
            elif evoef2_status == "unavailable_expired_url":
                risk_flags.append("evoef2_unavailable_expired_url")
            elif evoef2_status != "ok":
                risk_flags.append("evoef2_failed")
            elif not evoef2_energy:
                risk_flags.append("evoef2_energy_not_parsed")

        delta_evoef2_energy = _delta_string(evoef2_energy, baseline_evoef2_energy)
        if run_evoef2 and baseline_evoef2_status == "ok" and not baseline_evoef2_energy:
            risk_flags.append("baseline_evoef2_energy_not_parsed")
        if run_evoef2 and evoef2_energy and baseline_evoef2_energy and not delta_evoef2_energy:
            risk_flags.append("delta_evoef2_not_computed")

        if not baseline_pdb_path:
            risk_flags.append("missing_baseline_pdb")

        has_plddt = bool(fold_plddt["plddt_mean"])
        if has_plddt and not risk_flags:
            structure_status = "ok"
        elif has_plddt:
            structure_status = "partial"
        else:
            structure_status = "error"

        out.update({
            "structure_status": structure_status,
            "fold_pdb_path": fold_pdb_path if Path(fold_pdb_path).exists() else "",
            "baseline_fold_structure_url": baseline_url,
            "baseline_fold_pdb_path": baseline_pdb_path,
            **_prefixed_plddt_fields("fold", fold_plddt),
            **_prefixed_plddt_fields("baseline_fold", baseline_plddt),
            **_delta_plddt_fields(fold_plddt, baseline_plddt),
            "evoef2_status": evoef2_status,
            "evoef2_output_url": evoef2_output_url,
            "evoef2_energy": evoef2_energy,
            "baseline_evoef2_status": baseline_evoef2_status,
            "baseline_evoef2_output_url": baseline_evoef2_output_url,
            "baseline_evoef2_energy": baseline_evoef2_energy,
            "delta_evoef2_energy": delta_evoef2_energy,
            "structure_risk_flags": ";".join(risk_flags) if risk_flags else "none",
            "structure_note": (
                "Step 3.6 scores already-downloaded fold PDBs. "
                "pLDDT is read from the PDB B-factor field and compared with the inferred R6-6H09 baseline. "
                "EvoEF2 is run for the baseline and candidates through fold_structure_url when URLs are available; missing or expired URLs do not block pLDDT scoring."
            ),
            "structure_raw_json": json.dumps({
                "candidate_id": candidate_id,
                "fold_structure_url": structure_url,
                "fold_pdb_path": fold_pdb_path if Path(fold_pdb_path).exists() else "",
                "fold_plddt": fold_plddt,
                "baseline_fold_structure_url": baseline_url,
                "baseline_fold_pdb_path": baseline_pdb_path,
                "baseline_fold_plddt": baseline_plddt,
                "evoef2_status": evoef2_status,
                "evoef2_output_url": evoef2_output_url,
                "evoef2_energy": evoef2_energy,
                "baseline_evoef2_status": baseline_evoef2_status,
                "baseline_evoef2_output_url": baseline_evoef2_output_url,
                "baseline_evoef2_energy": baseline_evoef2_energy,
                "delta_evoef2_energy": delta_evoef2_energy,
                "risk_flags": risk_flags,
                "raw": _compact_payload({**raw, "baseline": baseline_evoef2_raw}),
            }, ensure_ascii=False, separators=(",", ":")),
        })
        output_by_id[candidate_id] = out
        compact.append({
            "candidate_id": candidate_id,
            "structure_status": structure_status,
            "fold_structure_url": structure_url,
            "fold_pdb_path": out["fold_pdb_path"],
            "fold_plddt_mean": fold_plddt["plddt_mean"],
            "delta_plddt_mean": _delta_string(fold_plddt["plddt_mean"], baseline_plddt["plddt_mean"]),
            "evoef2_status": evoef2_status,
            "evoef2_energy": evoef2_energy,
            "baseline_evoef2_status": baseline_evoef2_status,
            "baseline_evoef2_energy": baseline_evoef2_energy,
            "delta_evoef2_energy": delta_evoef2_energy,
            "risk_flags": risk_flags,
        })
        flush_output()

    final_rows = [output_by_id[candidate_id] for candidate_id in input_ids if candidate_id in output_by_id]
    completed_final = [row for row in final_rows if score_row_complete(row)]
    status_counts = {"ok": 0, "partial": 0, "error": 0}
    for row in final_rows:
        status = str(row.get("structure_status", "error")).strip()
        status_counts[status if status in status_counts else "error"] += 1

    return _json({
        "status": "ok" if len(completed_final) == len(rows) else "partial",
        "rows_total": len(rows),
        "rows_scored": len(completed_final),
        "rows_processed_this_call": len(rows_to_process),
        "rows_remaining": len(rows) - len(completed_final),
        "run_evoef2": run_evoef2,
        "structure_output_dir": str(pdb_dir),
        "baseline": {
            "fold_structure_url": baseline_url,
            "fold_pdb_path": baseline_pdb_path,
            "plddt": baseline_plddt,
            "evoef2_status": baseline_evoef2_status,
            "evoef2_output_url": baseline_evoef2_output_url,
            "evoef2_energy": baseline_evoef2_energy,
        },
        "status_counts": status_counts,
        "output_csv_path": str(out_path),
        "compact_results": compact,
    })


def _extract_url(payload: Any) -> str:
    candidates: list[tuple[int, str]] = []
    for path, value in _iter_scalars(payload):
        if not isinstance(value, str):
            continue
        joined_path = "_".join(item.lower() for item in path)
        for match in re.findall(r"https?://[^\s\"'<>),]+", value.strip()):
            url = match.rstrip(".,;]")
            lowered = url.lower()
            score = 0
            if any(term in joined_path for term in ["url", "link", "file", "download", "output", "result"]):
                score += 3
            if any(term in lowered for term in [".pdb", ".cif", "pdb", "cif", "structure"]):
                score += 3
            candidates.append((score, url))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _resolve_evoef2_energy(
    structure_url: str,
    existing_status: str,
    existing_output_url: str,
    existing_energy: str,
    timeout: int,
    poll_interval: int,
    raw: dict[str, Any],
) -> tuple[str, str, str]:
    """Return EvoEF2 status, output URL, and parsed Total energy.

    A completed EvoEF2 job only becomes usable after its output_Stability.json
    Total field has been parsed. Status=ok with an empty energy is retried.
    """
    status = str(existing_status or "").strip()
    output_url = str(existing_output_url or "").strip()
    energy = str(existing_energy or "").strip()

    if energy:
        return status or "ok", output_url, energy
    if output_url:
        energy = _download_evoef2_energy(output_url, raw, "evoef2_result")
        if energy:
            return status or "ok", output_url, energy

    if not structure_url:
        return "unavailable_missing_url", output_url, energy

    payload = _call_json_tool(
        evoef2_calculate_energy,
        input_structure_pdb_url=structure_url,
        mode="Stability",
        timeout=timeout,
        poll_interval=poll_interval,
    )
    raw["evoef2"] = payload
    status = _status_from_payload(payload)
    output_url = _extract_url(payload) or output_url
    energy = _stringify_number(_extract_number(payload, preferred_keys=["Total"], include_terms=["energy", "stability", "total"]))
    if not energy and output_url:
        energy = _download_evoef2_energy(output_url, raw, "evoef2_result")

    if status != "ok":
        error_text = json.dumps(payload, ensure_ascii=False)
        if "expired" in error_text.lower() or "not found" in error_text.lower() or "404" in error_text:
            status = "unavailable_expired_url"
    return status, output_url, energy


def _download_evoef2_energy(url: str, raw: dict[str, Any], key: str) -> str:
    try:
        result = json.loads(_download_url_text(url))
        raw[key] = result
        return _stringify_number(_extract_number(result, preferred_keys=["Total"], include_terms=["energy", "stability", "total"]))
    except Exception as exc:
        raw[f"{key}_download_error"] = str(exc)
        return ""


def _compact_payload(payload: Any, max_text: int = 500) -> Any:
    if isinstance(payload, dict):
        return {str(key): _compact_payload(value, max_text=max_text) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_compact_payload(value, max_text=max_text) for value in payload[:20]]
    if isinstance(payload, str):
        text = payload.strip()
        return text if len(text) <= max_text else f"{text[:max_text]}...<truncated>"
    return payload


def _call_fold_tool(fold_tool: str, sequence: str, timeout: int, poll_interval: int) -> Any:
    if fold_tool == "omegafold":
        return _call_json_tool(
            omegafold_predict_structure,
            sequence=sequence,
            timeout=timeout,
            poll_interval=poll_interval,
        )
    return _call_json_tool(
        esmf_predict_protein_structure,
        sequence=sequence,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    return safe or "sequence"


def _download_url_to_path(url: str, path: Path) -> None:
    data = _download_url_bytes(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _download_url_text(url: str) -> str:
    return _download_url_bytes(url).decode("utf-8", errors="replace")


def _download_url_bytes(url: str) -> bytes:
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="artifact download") from exc


def _empty_plddt_metrics() -> dict[str, str]:
    return {
        "plddt_ca_count": "",
        "plddt_mean": "",
        "plddt_min": "",
        "plddt_max": "",
        "plddt_pct_lt50": "",
        "plddt_pct_ge70": "",
    }


def _plddt_metrics_from_pdb(pdb_text: str) -> dict[str, str]:
    values: list[float] = []
    for line in pdb_text.splitlines():
        if not line.startswith("ATOM") or line[12:16].strip() != "CA":
            continue
        try:
            values.append(float(line[60:66]))
        except ValueError:
            continue
    if not values:
        return _empty_plddt_metrics()
    total = len(values)
    return {
        "plddt_ca_count": str(total),
        "plddt_mean": _stringify_number(sum(values) / total),
        "plddt_min": _stringify_number(min(values)),
        "plddt_max": _stringify_number(max(values)),
        "plddt_pct_lt50": _stringify_number(100.0 * sum(value < 50.0 for value in values) / total),
        "plddt_pct_ge70": _stringify_number(100.0 * sum(value >= 70.0 for value in values) / total),
    }


def _prefixed_plddt_fields(prefix: str, metrics: dict[str, str]) -> dict[str, str]:
    return {f"{prefix}_{key}": value for key, value in metrics.items()}


def _unprefixed_plddt_fields(prefix: str, row: dict[str, str]) -> dict[str, str]:
    return {
        key: str(row.get(f"{prefix}_{key}", "")).strip()
        for key in _empty_plddt_metrics()
    }


def _delta_plddt_fields(candidate: dict[str, str], baseline: dict[str, str]) -> dict[str, str]:
    return {
        "delta_plddt_mean": _delta_string(candidate.get("plddt_mean", ""), baseline.get("plddt_mean", "")),
        "delta_plddt_pct_lt50": _delta_string(candidate.get("plddt_pct_lt50", ""), baseline.get("plddt_pct_lt50", "")),
        "delta_plddt_pct_ge70": _delta_string(candidate.get("plddt_pct_ge70", ""), baseline.get("plddt_pct_ge70", "")),
    }


def _structure_row_complete(row: dict[str, str]) -> bool:
    return bool(
        str(row.get("fold_structure_url", "")).strip()
        and str(row.get("fold_pdb_path", "")).strip()
        and str(row.get("fold_plddt_mean", "")).strip()
    )


_STOP_CODONS = {"TAA", "TAG", "TGA"}
_ENZYME_DB = {
    "EcoRI": {"site": "GAATTC", "cut": 1},
    "BamHI": {"site": "GGATCC", "cut": 1},
    "HindIII": {"site": "AAGCTT", "cut": 1},
    "XhoI": {"site": "CTCGAG", "cut": 1},
    "NotI": {"site": "GCGGCCGC", "cut": 2},
    "SmaI": {"site": "CCCGGG", "cut": 3},
    "KpnI": {"site": "GGTACC", "cut": 5},
    "PstI": {"site": "CTGCAG", "cut": 5},
    "XbaI": {"site": "TCTAGA", "cut": 1},
    "NheI": {"site": "GCTAGC", "cut": 1},
    "SpeI": {"site": "ACTAGT", "cut": 1},
}
_CODON_USAGE_WEIGHTS = {
    "TTT": 0.58, "TTC": 1.00, "TTA": 0.14, "TTG": 0.13,
    "TCT": 0.19, "TCC": 0.23, "TCA": 0.14, "TCG": 0.15,
    "TAT": 0.57, "TAC": 1.00, "TGT": 0.46, "TGC": 1.00,
    "CTT": 0.12, "CTC": 0.10, "CTA": 0.04, "CTG": 1.00,
    "CCT": 0.18, "CCC": 0.13, "CCA": 0.20, "CCG": 1.00,
    "CAT": 0.58, "CAC": 1.00, "CAA": 0.34, "CAG": 1.00,
    "CGT": 1.00, "CGC": 0.41, "CGA": 0.09, "CGG": 0.15,
    "ATT": 0.49, "ATC": 1.00, "ATA": 0.15, "ATG": 1.00,
    "ACT": 0.19, "ACC": 1.00, "ACA": 0.17, "ACG": 0.33,
    "AAT": 0.47, "AAC": 1.00, "AAA": 0.76, "AAG": 1.00,
    "AGT": 0.16, "AGC": 0.28, "AGA": 0.07, "AGG": 0.04,
    "GTT": 0.72, "GTC": 0.47, "GTA": 0.42, "GTG": 1.00,
    "GCT": 0.42, "GCC": 1.00, "GCA": 0.71, "GCG": 0.57,
    "GAT": 0.63, "GAC": 1.00, "GAA": 1.00, "GAG": 0.42,
    "GGT": 0.58, "GGC": 1.00, "GGA": 0.49, "GGG": 0.43,
}
_CODON_TABLE = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L", "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*", "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L", "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q", "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M", "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K", "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V", "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E", "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}
_AA_NAMES = {
    "A": "Alanine", "R": "Arginine", "N": "Asparagine", "D": "Aspartate", "C": "Cysteine",
    "Q": "Glutamine", "E": "Glutamate", "G": "Glycine", "H": "Histidine", "I": "Isoleucine",
    "L": "Leucine", "K": "Lysine", "M": "Methionine", "F": "Phenylalanine", "P": "Proline",
    "S": "Serine", "T": "Threonine", "W": "Tryptophan", "Y": "Tyrosine", "V": "Valine", "*": "Stop",
}


def _dna_result(result: Any, explanation: str, started_at: float, status: str = "ok") -> str:
    return _json({"status": status, "result": result, "explanation": explanation, "runtime_ms": int((time.time() - started_at) * 1000)})


def _normalize_dna(sequence: str) -> str:
    return re.sub(r"\s+", "", sequence or "").upper().replace("U", "T")


def _validate_dna(sequence: str) -> str:
    seq = _normalize_dna(sequence)
    if not seq or re.search(r"[^ACGT]", seq):
        raise ValueError("sequence must contain only A/C/G/T or U for RNA")
    return seq


def _reverse_complement(sequence: str) -> str:
    return _validate_dna(sequence).translate(str.maketrans("ACGT", "TGCA"))[::-1]


def _translate_coding_sequence(sequence: str) -> str:
    seq = _validate_dna(sequence)
    trim = len(seq) % 3
    coding = seq[:-trim] if trim else seq
    protein = "".join(_CODON_TABLE.get(coding[i:i + 3], "X") for i in range(0, len(coding), 3))
    return protein[:-1] if protein.endswith("*") else protein


def _oriented_coordinates(strand: str, start_zero: int, end_exclusive: int, sequence_length: int) -> tuple[int, int]:
    if strand == "+":
        return start_zero + 1, end_exclusive
    return sequence_length - end_exclusive + 1, sequence_length - start_zero


def _scan_complete_orfs(oriented_sequence: str, strand: str, original_length: int) -> list[dict[str, Any]]:
    orfs: list[dict[str, Any]] = []
    for frame in range(3):
        i = frame
        while i <= len(oriented_sequence) - 3:
            if oriented_sequence[i:i + 3] != "ATG":
                i += 3
                continue
            j = i + 3
            while j <= len(oriented_sequence) - 3:
                if oriented_sequence[j:j + 3] in _STOP_CODONS:
                    end_exclusive = j + 3
                    orf_seq = oriented_sequence[i:end_exclusive]
                    protein = _translate_coding_sequence(orf_seq)
                    start, end = _oriented_coordinates(strand, i, end_exclusive, original_length)
                    orfs.append({
                        "start": start,
                        "end": end,
                        "strand": strand,
                        "frame": frame + 1,
                        "oriented_start": i + 1,
                        "oriented_end": end_exclusive,
                        "orf_sequence": orf_seq,
                        "protein_sequence": protein,
                        "nt_length": len(orf_seq),
                        "aa_length": len(protein),
                    })
                    break
                j += 3
            i += 3
    return orfs


def _find_all_orfs(sequence: str) -> list[dict[str, Any]]:
    seq = _validate_dna(sequence)
    orfs = _scan_complete_orfs(seq, "+", len(seq))
    orfs.sort(key=lambda item: (-item["nt_length"], item["start"], item["frame"]))
    return orfs


def _find_all_orfs_six_frame(sequence: str) -> list[dict[str, Any]]:
    seq = _validate_dna(sequence)
    rc_seq = _reverse_complement(seq)
    orfs = _scan_complete_orfs(seq, "+", len(seq))
    orfs.extend(_scan_complete_orfs(rc_seq, "-", len(seq)))
    orfs.sort(key=lambda item: (-item["nt_length"], item["strand"], item["start"], item["frame"]))
    return orfs


def _resolve_enzyme_site(name: str) -> dict[str, Any]:
    if name not in _ENZYME_DB:
        raise ValueError(f"unsupported enzyme: {name}")
    return _ENZYME_DB[name]


def _wallace_tm(sequence: str) -> int:
    seq = _validate_dna(sequence)
    return 2 * (seq.count("A") + seq.count("T")) + 4 * (seq.count("G") + seq.count("C"))


class FindLongestORFInput(ToolInput):
    sequence: str = Field(description="DNA sequence consisting of A/C/G/T.")


@bioma_tool(
    name="find_longest_orf",
    description="Find the longest ATG-start, stop-terminated ORF across the three forward reading frames.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=FindLongestORFInput,
    tags=["dna", "orf"],
)
def find_longest_orf(sequence: str) -> str:
    started = time.time()
    orfs = _find_all_orfs(sequence)
    if not orfs:
        return _dna_result({}, "No complete ORF was found.", started, status="no_orf")
    return _dna_result(orfs[0], "The longest complete ORF was selected across the three forward reading frames.", started)


class TranslateDNAInput(ToolInput):
    sequence: str = Field(description="DNA coding sequence in frame.")


@bioma_tool(
    name="translate_dna",
    description="Translate a DNA coding sequence into its amino acid sequence.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=TranslateDNAInput,
    tags=["dna", "translation"],
)
def translate_dna(sequence: str) -> str:
    started = time.time()
    seq = _validate_dna(sequence)
    protein = _translate_coding_sequence(seq)
    return _dna_result({"coding_sequence": seq, "protein_sequence": protein, "aa_length": len(protein)}, "The DNA sequence was translated with the standard genetic code.", started)


class TranslationEfficiencyInput(ToolInput):
    sequence: str = Field(description="DNA coding sequence in frame.")


@bioma_tool(
    name="calc_translation_efficiency",
    description="Estimate translation efficiency from simple codon-usage weights.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=TranslationEfficiencyInput,
    tags=["dna", "codon"],
)
def calc_translation_efficiency(sequence: str) -> str:
    started = time.time()
    seq = _validate_dna(sequence)
    coding = seq[: len(seq) - (len(seq) % 3)]
    codons = [coding[i:i + 3] for i in range(0, len(coding), 3)]
    if codons and codons[-1] in _STOP_CODONS:
        codons = codons[:-1]
    weights = [_CODON_USAGE_WEIGHTS.get(codon, 0.01) for codon in codons]
    score = sum(weights) / len(weights) if weights else 0.0
    return _dna_result({"codon_count": len(codons), "efficiency_score": round(score, 6), "codons": codons}, "The score is the mean codon-usage weight over the coding sequence.", started)


class AminoAcidAtPositionInput(ToolInput):
    orf_sequence: str = Field(description="DNA coding sequence for an ORF.")
    position: int = Field(description="1-based amino-acid position.")


@bioma_tool(
    name="amino_acid_at_position",
    description="Translate an ORF and return the amino acid at a 1-based position.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=AminoAcidAtPositionInput,
    tags=["dna", "translation"],
)
def amino_acid_at_position(orf_sequence: str, position: int) -> str:
    started = time.time()
    if position < 1:
        return _dna_result({"position": position}, "Position must be >= 1.", started, status="error")
    protein = _translate_coding_sequence(orf_sequence)
    aa = protein[position - 1] if position <= len(protein) else ""
    return _dna_result(
        {"position": position, "amino_acid": aa, "amino_acid_name": _AA_NAMES.get(aa, ""), "protein_sequence": protein, "aa_length": len(protein)},
        "The requested amino acid was read from the translated ORF." if aa else "The requested position is outside the translated ORF.",
        started,
        status="ok" if aa else "out_of_range",
    )


class AminoAcidInLongestORFInput(ToolInput):
    sequence: str = Field(description="DNA sequence consisting of A/C/G/T.")
    position: int = Field(description="1-based amino-acid position in the longest ORF.")


@bioma_tool(
    name="amino_acid_in_longest_orf",
    description="Find the longest ORF and return the amino acid at a requested position.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=AminoAcidInLongestORFInput,
    tags=["dna", "orf", "translation"],
)
def amino_acid_in_longest_orf(sequence: str, position: int) -> str:
    started = time.time()
    if position < 1:
        return _dna_result({"position": position}, "Position must be >= 1.", started, status="error")
    orfs = _find_all_orfs(sequence)
    if not orfs:
        return _dna_result({"position": position, "amino_acid": ""}, "No valid longest ORF was available.", started, status="no_orf")
    best = orfs[0]
    protein = best["protein_sequence"]
    aa = protein[position - 1] if position <= len(protein) else ""
    result = dict(best)
    result.update({"position": position, "amino_acid": aa, "amino_acid_name": _AA_NAMES.get(aa, "")})
    return _dna_result(result, "The amino acid was read from the translated longest ORF." if aa else "The requested position is outside the translated longest ORF.", started, status="ok" if aa else "out_of_range")


@bioma_tool(
    name="amino_acid_in_longest_orf_six_frame",
    description="Find the longest complete ORF across forward and reverse-complement reading frames and return the amino acid at a requested position.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=AminoAcidInLongestORFInput,
    tags=["dna", "orf", "translation"],
)
def amino_acid_in_longest_orf_six_frame(sequence: str, position: int) -> str:
    started = time.time()
    if position < 1:
        return _dna_result({"position": position}, "Position must be >= 1.", started, status="error")
    orfs = _find_all_orfs_six_frame(sequence)
    if not orfs:
        return _dna_result({"position": position, "amino_acid": ""}, "No valid longest ORF was available.", started, status="no_orf")
    best = orfs[0]
    protein = best["protein_sequence"]
    aa = protein[position - 1] if position <= len(protein) else ""
    result = dict(best)
    result.update({"position": position, "amino_acid": aa, "amino_acid_name": _AA_NAMES.get(aa, "")})
    explanation = (
        "The amino acid was read from the translated longest ORF across all six reading frames."
        if aa
        else "The requested position is outside the translated longest ORF across all six reading frames."
    )
    return _dna_result(result, explanation, started, status="ok" if aa else "out_of_range")


class RestrictionDigestInput(ToolInput):
    sequence: str = Field(description="Linear DNA sequence consisting of A/C/G/T.")
    enzymes: list[str] = Field(description="Restriction enzyme names such as EcoRI or HindIII.")


@bioma_tool(
    name="restriction_digest",
    description="Digest a linear DNA sequence with common restriction enzymes.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=RestrictionDigestInput,
    tags=["dna", "restriction"],
)
def restriction_digest(sequence: str, enzymes: list[str]) -> str:
    started = time.time()
    seq = _validate_dna(sequence)
    cut_positions = []
    sites = []
    for enzyme_name in enzymes:
        enzyme = _resolve_enzyme_site(enzyme_name)
        for match in re.finditer(f"(?={enzyme['site']})", seq):
            cut_pos = match.start() + enzyme["cut"]
            cut_positions.append(cut_pos)
            sites.append({"enzyme": enzyme_name, "recognition_site": enzyme["site"], "cut_position": cut_pos})
    cuts = sorted(set(pos for pos in cut_positions if 0 < pos < len(seq)))
    boundaries = [0, *cuts, len(seq)]
    fragments = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]
    return _dna_result({"cut_positions": cuts, "sites": sites, "fragment_lengths": fragments, "num_fragments": len(fragments)}, "Fragments were computed for a linear DNA molecule.", started)


class PCRPrimerDesignInput(ToolInput):
    target_sequence: str = Field(description="Target DNA sequence to amplify.")
    product_len: int = Field(default=120, description="Desired amplicon length in bp.")
    enzyme_sites: list[str] = Field(default_factory=list, description="Optional enzyme names to prepend as 5' overhangs.")


@bioma_tool(
    name="design_pcr_primers",
    description="Design simple PCR primers with optional restriction-site overhangs.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=PCRPrimerDesignInput,
    tags=["dna", "pcr", "primer"],
)
def design_pcr_primers(target_sequence: str, product_len: int = 120, enzyme_sites: Optional[list[str]] = None) -> str:
    started = time.time()
    seq = _validate_dna(target_sequence)
    enzyme_sites = enzyme_sites or []
    product_len = min(max(product_len, 40), len(seq))
    amplicon = seq[:product_len]

    def pick_primer(fragment: str) -> str:
        best = fragment[:20]
        for size in range(18, min(28, len(fragment)) + 1):
            candidate = fragment[:size]
            if 58 <= _wallace_tm(candidate) <= 64:
                return candidate
        return best

    forward_core = pick_primer(amplicon[:32])
    reverse_core = _reverse_complement(pick_primer(_reverse_complement(amplicon[-32:])))
    overhang = "".join(_resolve_enzyme_site(name)["site"] for name in enzyme_sites)
    return _dna_result({
        "amplicon_length": len(amplicon),
        "forward_primer": overhang + forward_core,
        "reverse_primer": overhang + reverse_core,
        "forward_core": forward_core,
        "reverse_core": reverse_core,
        "forward_tm": _wallace_tm(forward_core),
        "reverse_tm": _wallace_tm(reverse_core),
        "enzyme_overhang": overhang,
    }, "Primers were chosen from the amplicon ends using a Wallace-rule Tm heuristic.", started)
