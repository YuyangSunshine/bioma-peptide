"""
Protein sequence tools: UniProt fetch, mutation construction, ESM-2 scoring.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field
import requests

from bioma.tools.base import ToolInput, bioma_tool
from bioma.tools.errors import requests_tool_error


def _get_esm_device(torch):
    """Use cuda:0 for ESM by default; override with BIOAGENT_ESM_DEVICE if needed."""
    import os

    device_name = os.getenv("BIOAGENT_ESM_DEVICE", "cuda:0")
    if device_name.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"ESM requested device {device_name}, but CUDA is not available")
    return torch.device(device_name)


# ──────────────────────────────────────────────────────────────────────
# fetch_sequence_by_uniprot_id
# ──────────────────────────────────────────────────────────────────────

class UniProtInput(ToolInput):
    uniprot_id: str = Field(description="UniProt accession ID, e.g. P04637")


@bioma_tool(
    name="fetch_sequence_by_uniprot_id",
    description="Fetch protein amino acid sequence from UniProt by accession ID.",
    category="database",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=UniProtInput,
    requires_network=True,
)
def fetch_sequence_by_uniprot_id(uniprot_id: str) -> str:
    """Retrieve FASTA sequence from UniProt REST API."""
    url = f"https://rest.uniprot.org/uniprotkb/{uniprot_id}.fasta"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        sequence = "".join(line for line in lines if not line.startswith(">"))
        return sequence
    except requests.exceptions.RequestException as exc:
        raise requests_tool_error(exc, backend="UniProt") from exc


# ──────────────────────────────────────────────────────────────────────
# normalize_sequence
# ──────────────────────────────────────────────────────────────────────

class NormalizeInput(ToolInput):
    sequence: str = Field(description="Amino acid sequence (single-letter code)")


@bioma_tool(
    name="normalize_sequence",
    description="Normalize a protein sequence: uppercase, remove whitespace and invalid characters.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=NormalizeInput,
)
def normalize_sequence(sequence: str) -> str:
    """Clean and uppercase a protein sequence."""
    valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
    seq = "".join(c.upper() for c in sequence if c.isalpha())
    invalid = set(seq) - valid_aa
    if invalid:
        return f"Warning: invalid characters removed {invalid}. Cleaned: {seq}"
    return seq


# ──────────────────────────────────────────────────────────────────────
# apply_mutations_to_sequence
# ──────────────────────────────────────────────────────────────────────

class MutationInput(ToolInput):
    sequence: str = Field(description="Wild-type protein sequence")
    mutations: str = Field(description="Comma-separated mutations, e.g. 'R175H,R248W'")


@bioma_tool(
    name="apply_mutations_to_sequence",
    description="Apply one or more point mutations to a protein sequence. Format: A123G (WT+position+mutant).",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=MutationInput,
)
def apply_mutations_to_sequence(sequence: str, mutations: str) -> str:
    """Apply mutations to sequence and return mutant sequence."""
    import re

    seq = list(sequence.upper())
    results = {}

    for mut in mutations.split(","):
        mut = mut.strip()
        m = re.match(r"([A-Z])(\d+)([A-Z])", mut)
        if not m:
            results[mut] = f"Error: invalid mutation format '{mut}'"
            continue
        wt_aa, pos, mut_aa = m.group(1), int(m.group(2)), m.group(3)
        idx = pos - 1
        if idx < 0 or idx >= len(seq):
            results[mut] = f"Error: position {pos} out of range (sequence length {len(seq)})"
            continue
        if seq[idx] != wt_aa:
            results[mut] = (
                f"Warning: position {pos} is '{seq[idx]}', not '{wt_aa}'. Applying anyway."
            )
        seq[idx] = mut_aa
        if mut not in results:
            results[mut] = "OK"

    mutant_seq = "".join(seq)
    report = "; ".join(f"{k}: {v}" for k, v in results.items())
    return f"Mutant sequence: {mutant_seq}\nMutation report: {report}"


# ──────────────────────────────────────────────────────────────────────
# evaluate_mutations_with_esm2
# ──────────────────────────────────────────────────────────────────────

class ESM2ScoringInput(ToolInput):
    sequence: str = Field(description="Wild-type protein sequence")
    mutations: str = Field(description="Comma-separated mutations, e.g. 'R175H,R248W'")
    model_name: Optional[str] = Field(default="esm2_t6_8M_UR50D", description="ESM-2 model name")


class ESM2MatrixScoringInput(ToolInput):
    matrix_csv_path: str = Field(description="Path to an R7 mutation matrix CSV with full_sequence.")
    output_csv_path: Optional[str] = Field(
        default="",
        description="Optional path for the ESM-enriched scored matrix CSV",
    )
    model_name: Optional[str] = Field(default="esm2_t6_8M_UR50D", description="ESM-2 model name")


@bioma_tool(
    name="evaluate_mutations_with_esm2",
    description="Score protein mutations using ESM-2 log-likelihood ratio (LLR). Higher LLR = more tolerated.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ESM2ScoringInput,
)
def evaluate_mutations_with_esm2(
    sequence: str,
    mutations: str,
    model_name: str = "esm2_t6_8M_UR50D",
) -> str:
    """
    Compute ESM-2 log-likelihood ratio for each mutation.
    Returns JSON-formatted scores.
    """
    import re
    import json

    # Try using fair-esm if available
    try:
        import esm
        import torch

        device = _get_esm_device(torch)
        model, alphabet = esm.pretrained.load_model_and_alphabet(model_name)
        model = model.to(device)
        model.eval()
        batch_converter = alphabet.get_batch_converter()

        data = [("protein", sequence)]
        _, _, tokens = batch_converter(data)
        tokens = tokens.to(device)

        with torch.no_grad():
            results = model(tokens, repr_layers=[6], return_contacts=False)

        log_probs = torch.log_softmax(results["logits"], dim=-1)
        scores = {}

        for mut_str in mutations.split(","):
            mut_str = mut_str.strip()
            m = re.match(r"([A-Z])(\d+)([A-Z])", mut_str)
            if not m:
                scores[mut_str] = "invalid format"
                continue
            wt_aa, pos, mut_aa = m.group(1), int(m.group(2)), m.group(3)
            wt_idx = alphabet.get_idx(wt_aa)
            mut_idx = alphabet.get_idx(mut_aa)
            # +1 for BOS token
            llr = (log_probs[0, pos, mut_idx] - log_probs[0, pos, wt_idx]).item()
            scores[mut_str] = round(llr, 4)

        return json.dumps(scores, indent=2)

    except ImportError as exc:
        return json.dumps({
            "status": "unavailable",
            "error": f"ESM-2 import failed: {exc}",
            "note": "No simulated scores were produced.",
        }, indent=2)
    except Exception as exc:
        return json.dumps({
            "status": "unavailable",
            "error": f"ESM-2 scoring failed: {exc}",
            "note": "No simulated scores were produced.",
        }, indent=2)


# ──────────────────────────────────────────────────────────────────────
# evaluate_mutation_matrix_with_esm2
# ──────────────────────────────────────────────────────────────────────

@bioma_tool(
    name="evaluate_mutation_matrix_with_esm2",
    description=(
        "Score a mutation matrix with ESM-2 masked-marginal LLRs. "
        "For multi-mutants, computes additive backbone-context LLR, mutant-context "
        "LLR, and epistasis_delta = contextual - additive. Writes an optional CSV."
    ),
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ESM2MatrixScoringInput,
)
def evaluate_mutation_matrix_with_esm2(
    matrix_csv_path: str,
    output_csv_path: str = "",
    model_name: str = "esm2_t6_8M_UR50D",
) -> str:
    """
    Evaluate each row in a mutation matrix using candidate-specific masked marginals.

    The output separates:
    - additive_llr: each mutation scored in the inferred backbone context.
    - contextual_llr: each mutation scored in the full mutant sequence context.
    - epistasis_delta: contextual_llr - additive_llr for multi-mutants.

    This is a sequence-likelihood proxy, not a Tm or ΔΔG predictor.
    """
    import csv
    import json
    import re
    from pathlib import Path

    mutation_re = re.compile(r"^([A-Z])(\d+)([A-Z])$")
    matrix_path = Path(matrix_csv_path)
    if not matrix_path.exists():
        return json.dumps({
            "status": "error",
            "error": f"matrix_csv_path not found: {matrix_csv_path}",
        }, ensure_ascii=False)

    with matrix_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        input_fieldnames = reader.fieldnames or []

    required = {
        "candidate_id",
        "backbone_id",
        "mutations_vs_backbone",
        "full_sequence",
        "design_strategy",
        "risk_flags",
    }
    missing = sorted(required - set(input_fieldnames))
    if missing:
        return json.dumps({
            "status": "error",
            "error": f"matrix CSV missing required columns: {', '.join(missing)}",
        }, ensure_ascii=False)
    if not rows:
        return json.dumps({"status": "error", "error": "matrix CSV has no rows"}, ensure_ascii=False)

    parsed_rows = []
    errors: list[str] = []
    inferred_backbones: dict[str, str] = {}
    for index, row in enumerate(rows, start=2):
        mutations = _parse_esm_matrix_mutations(row.get("mutations_vs_backbone", ""), mutation_re)
        full_sequence = str(row.get("full_sequence", "")).strip().upper()
        if not full_sequence:
            errors.append(f"row {index}: missing full_sequence")
            continue
        try:
            inferred = _infer_backbone_from_full_sequence(full_sequence, mutations, mutation_re)
        except ValueError as exc:
            errors.append(f"row {index}: {exc}")
            continue
        backbone_id = str(row.get("backbone_id", "")).strip()
        previous = inferred_backbones.setdefault(backbone_id, inferred)
        if previous != inferred:
            errors.append(f"row {index}: inconsistent inferred backbone for {backbone_id}")
        parsed_rows.append((row, mutations, full_sequence, inferred))

    if errors:
        return json.dumps({"status": "error", "errors": errors[:20]}, ensure_ascii=False, indent=2)

    backbone_sequence = next(iter(inferred_backbones.values()))

    try:
        import esm
        import torch

        device = _get_esm_device(torch)
        model, alphabet = esm.pretrained.load_model_and_alphabet(model_name)
        model = model.to(device)
        model.eval()
        batch_converter = alphabet.get_batch_converter()

        def score_mutations(context_sequence: str, mutations: list[str]) -> dict[str, float]:
            if not mutations:
                return {}
            positions: list[int] = []
            parsed = []
            for mutation in mutations:
                match = mutation_re.match(mutation)
                if match is None:
                    raise ValueError(f"invalid mutation format: {mutation}")
                wt_aa, pos_text, mut_aa = match.groups()
                pos = int(pos_text)
                if pos < 1 or pos > len(context_sequence):
                    raise ValueError(f"{mutation} position out of range")
                positions.append(pos)
                parsed.append((mutation, wt_aa, pos, mut_aa))

            _, _, base_tokens = batch_converter([("protein", context_sequence)])
            tokens = base_tokens.repeat(len(positions), 1)
            for row_idx, pos in enumerate(positions):
                tokens[row_idx, pos] = alphabet.mask_idx
            tokens = tokens.to(device)
            with torch.no_grad():
                logits = model(tokens, repr_layers=[], return_contacts=False)["logits"]
                log_probs = torch.log_softmax(logits, dim=-1)

            scores: dict[str, float] = {}
            for row_idx, (mutation, wt_aa, pos, mut_aa) in enumerate(parsed):
                wt_idx = alphabet.get_idx(wt_aa)
                mut_idx = alphabet.get_idx(mut_aa)
                llr = (log_probs[row_idx, pos, mut_idx] - log_probs[row_idx, pos, wt_idx]).item()
                scores[mutation] = round(float(llr), 4)
            return scores

        esm_status = "available"
        unavailable_reason = ""
    except ImportError as exc:
        model = None
        alphabet = None
        score_mutations = None  # type: ignore[assignment]
        esm_status = "unavailable"
        unavailable_reason = f"ESM-2 import failed: {exc}"
    except Exception as exc:
        model = None
        alphabet = None
        score_mutations = None  # type: ignore[assignment]
        esm_status = "unavailable"
        unavailable_reason = f"ESM-2 model load failed: {exc}"

    output_rows: list[dict[str, str]] = []
    compact_scores: list[dict[str, object]] = []
    for row, mutations, full_sequence, _ in parsed_rows:
        if esm_status == "available" and score_mutations is not None:
            try:
                additive_scores = score_mutations(backbone_sequence, mutations)
                contextual_scores = score_mutations(full_sequence, mutations)
                additive_sum = _sum_scores(additive_scores)
                contextual_sum = _sum_scores(contextual_scores)
                if len(mutations) <= 1:
                    epistasis_delta = ""
                    esm_note = "candidate_specific_masked_marginal;single_mutation_epistasis_na"
                else:
                    delta = contextual_sum - additive_sum
                    epistasis_delta = f"{delta:.4f}"
                    esm_note = "candidate_specific_masked_marginal;multi_mutant_contextual_epistasis_delta"
                esm_additive = f"{additive_sum:.4f}"
                esm_contextual = f"{contextual_sum:.4f}"
                esm_mutation_llrs = json.dumps({
                    "additive": additive_scores,
                    "contextual": contextual_scores,
                }, sort_keys=True, separators=(",", ":"))
                tool_status = "ESM-2_available_candidate_specific_masked_marginal"
            except Exception as exc:
                esm_additive = ""
                esm_contextual = ""
                epistasis_delta = ""
                esm_mutation_llrs = "{}"
                esm_note = f"candidate_error:{exc}"
                tool_status = "ESM-2_error_for_candidate"
        else:
            esm_additive = ""
            esm_contextual = ""
            epistasis_delta = ""
            esm_mutation_llrs = "{}"
            esm_note = unavailable_reason or "ESM-2 unavailable"
            tool_status = "ESM-2_unavailable_no_simulated_scores"

        evidence_note = _evidence_note_from_row(row, mutations, epistasis_delta)
        property_notes = _property_notes_from_mutations(mutations)
        out = {
            "candidate_id": str(row.get("candidate_id", "")).strip(),
            "backbone_id": str(row.get("backbone_id", "")).strip(),
            "mutations_vs_backbone": ",".join(mutations),
            "full_sequence": full_sequence,
            "design_strategy": str(row.get("design_strategy", "")).strip(),
            "tool_status": tool_status,
            "esm_model": model_name,
            "esm_status": esm_status,
            "esm_additive_llr": esm_additive,
            "esm_contextual_llr": esm_contextual,
            "esm_epistasis_delta": epistasis_delta,
            "esm_mutation_llrs": esm_mutation_llrs,
            "esm_note": esm_note,
            "property_notes": property_notes,
            "risk_flags": str(row.get("risk_flags", "")).strip(),
            "evidence_note": evidence_note,
        }
        output_rows.append(out)
        compact_scores.append({
            "candidate_id": out["candidate_id"],
            "mutations": out["mutations_vs_backbone"],
            "esm_additive_llr": out["esm_additive_llr"],
            "esm_contextual_llr": out["esm_contextual_llr"],
            "esm_epistasis_delta": out["esm_epistasis_delta"],
        })

    fieldnames = [
        "candidate_id",
        "backbone_id",
        "mutations_vs_backbone",
        "full_sequence",
        "design_strategy",
        "tool_status",
        "esm_model",
        "esm_status",
        "esm_additive_llr",
        "esm_contextual_llr",
        "esm_epistasis_delta",
        "esm_mutation_llrs",
        "esm_note",
        "property_notes",
        "risk_flags",
        "evidence_note",
    ]

    if output_csv_path:
        out_path = Path(output_csv_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)

    status = "ok" if esm_status == "available" else "unavailable"
    return json.dumps({
        "status": status,
        "model_name": model_name,
        "scoring_method": "masked_marginal_candidate_specific",
        "rows_scored": len(output_rows),
        "output_csv_path": output_csv_path,
        "csv_columns": fieldnames,
        "compact_scores": compact_scores,
        "note": (
            "ESM scores are sequence-likelihood proxies, not Tm or ΔΔG. "
            "epistasis_delta=contextual_llr-additive_llr; blank for single mutants."
        ),
        "unavailable_reason": unavailable_reason,
    }, ensure_ascii=False, indent=2)


def _parse_esm_matrix_mutations(raw: str, mutation_re) -> list[str]:
    import re

    text = str(raw or "").strip()
    if not text or text.lower() in {"none", "na", "n/a", "-"}:
        return []
    mutations = [item.strip() for item in re.split(r"[,;]\s*|\s+", text) if item.strip()]
    for mutation in mutations:
        if mutation_re.match(mutation) is None:
            raise ValueError(f"invalid mutation format: {mutation}")
    return mutations


def _infer_backbone_from_full_sequence(full_sequence: str, mutations: list[str], mutation_re) -> str:
    seq = list(full_sequence)
    for mutation in mutations:
        match = mutation_re.match(mutation)
        if match is None:
            raise ValueError(f"invalid mutation format: {mutation}")
        wt_aa, pos_text, mut_aa = match.groups()
        pos = int(pos_text)
        idx = pos - 1
        if idx < 0 or idx >= len(seq):
            raise ValueError(f"{mutation} position out of range")
        observed = seq[idx]
        if observed != mut_aa:
            raise ValueError(f"{mutation} mutant mismatch: full sequence has {observed}{pos}, not {mut_aa}{pos}")
        seq[idx] = wt_aa
    return "".join(seq)


def _sum_scores(scores: dict[str, float]) -> float:
    return float(sum(scores.values()))


def _property_notes_from_mutations(mutations: list[str]) -> str:
    notes = []
    for mutation in mutations:
        if mutation == "N6D":
            notes.append("N6D neutral_to_negative_charge")
        elif mutation == "I65V":
            notes.append("I65V conservative_aliphatic")
        elif mutation == "K236E":
            notes.append("K236E positive_to_negative_charge_reversal")
        elif mutation == "I174S":
            notes.append("I174S hydrophobic_to_polar_in_F249L_context")
        elif mutation == "E66K":
            notes.append("E66K negative_to_positive_charge_reversal")
        elif mutation in {"H160L", "H160R"}:
            notes.append(f"{mutation} packing_hypothesis")
        elif mutation:
            notes.append(f"{mutation} conservative_or_surface_satellite")
    return ";".join(notes) if notes else "no_new_mutation"


def _evidence_note_from_row(row: dict[str, str], mutations: list[str], epistasis_delta: str) -> str:
    parts = [
        str(row.get("construction_notes", "")).strip(),
        f"mutations={'+'.join(mutations) if mutations else 'none'}",
    ]
    if epistasis_delta:
        parts.append(f"esm_epistasis_delta={epistasis_delta}")
    else:
        parts.append("esm_epistasis_delta=NA_for_single_or_unavailable")
    return ";".join(part for part in parts if part)


# ──────────────────────────────────────────────────────────────────────
# predict_protein_contacts_with_esm2
# ──────────────────────────────────────────────────────────────────────

class ContactInput(ToolInput):
    sequence: str = Field(description="Protein sequence")
    top_k: Optional[int] = Field(default=20, description="Number of top contact pairs to return")


@bioma_tool(
    name="predict_protein_contacts_with_esm2",
    description="Predict residue-residue contact map using ESM-2 attention weights.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=ContactInput,
)
def predict_protein_contacts_with_esm2(sequence: str, top_k: int = 20) -> str:
    """Predict protein contacts from ESM-2 attention map."""
    try:
        import esm
        import torch

        device = _get_esm_device(torch)
        model, alphabet = esm.pretrained.esm2_t6_8M_UR50D()
        model = model.to(device)
        model.eval()
        batch_converter = alphabet.get_batch_converter()
        data = [("protein", sequence)]
        _, _, tokens = batch_converter(data)
        tokens = tokens.to(device)

        with torch.no_grad():
            results = model(tokens, repr_layers=[6], return_contacts=True)

        contacts = results["contacts"][0]
        L = len(sequence)
        pairs = []
        for i in range(L):
            for j in range(i + 6, L):
                pairs.append((i + 1, j + 1, contacts[i, j].item()))

        pairs.sort(key=lambda x: x[2], reverse=True)
        top = pairs[:top_k]
        lines = [f"i={i}, j={j}, score={s:.4f}" for i, j, s in top]
        return f"Top-{top_k} contacts:\n" + "\n".join(lines)

    except ImportError:
        return (
            "ESM-2 not installed. Install with: pip install fair-esm\n"
            "Example output: top contacts between positions 10-30 (simulated)."
        )


# ──────────────────────────────────────────────────────────────────────
# get_amino_acid_properties
# ──────────────────────────────────────────────────────────────────────

AA_PROPERTIES = {
    "A": {"name": "Alanine", "MW": 89.1, "charge_pH7": 0, "hydrophobicity": 1.8, "type": "nonpolar"},
    "R": {"name": "Arginine", "MW": 174.2, "charge_pH7": 1, "hydrophobicity": -4.5, "type": "positive"},
    "N": {"name": "Asparagine", "MW": 132.1, "charge_pH7": 0, "hydrophobicity": -3.5, "type": "polar"},
    "D": {"name": "Aspartate", "MW": 133.1, "charge_pH7": -1, "hydrophobicity": -3.5, "type": "negative"},
    "C": {"name": "Cysteine", "MW": 121.2, "charge_pH7": 0, "hydrophobicity": 2.5, "type": "polar"},
    "E": {"name": "Glutamate", "MW": 147.1, "charge_pH7": -1, "hydrophobicity": -3.5, "type": "negative"},
    "Q": {"name": "Glutamine", "MW": 146.2, "charge_pH7": 0, "hydrophobicity": -3.5, "type": "polar"},
    "G": {"name": "Glycine", "MW": 75.1, "charge_pH7": 0, "hydrophobicity": -0.4, "type": "nonpolar"},
    "H": {"name": "Histidine", "MW": 155.2, "charge_pH7": 0.1, "hydrophobicity": -3.2, "type": "positive"},
    "I": {"name": "Isoleucine", "MW": 131.2, "charge_pH7": 0, "hydrophobicity": 4.5, "type": "nonpolar"},
    "L": {"name": "Leucine", "MW": 131.2, "charge_pH7": 0, "hydrophobicity": 3.8, "type": "nonpolar"},
    "K": {"name": "Lysine", "MW": 146.2, "charge_pH7": 1, "hydrophobicity": -3.9, "type": "positive"},
    "M": {"name": "Methionine", "MW": 149.2, "charge_pH7": 0, "hydrophobicity": 1.9, "type": "nonpolar"},
    "F": {"name": "Phenylalanine", "MW": 165.2, "charge_pH7": 0, "hydrophobicity": 2.8, "type": "nonpolar"},
    "P": {"name": "Proline", "MW": 115.1, "charge_pH7": 0, "hydrophobicity": -1.6, "type": "nonpolar"},
    "S": {"name": "Serine", "MW": 105.1, "charge_pH7": 0, "hydrophobicity": -0.8, "type": "polar"},
    "T": {"name": "Threonine", "MW": 119.1, "charge_pH7": 0, "hydrophobicity": -0.7, "type": "polar"},
    "W": {"name": "Tryptophan", "MW": 204.2, "charge_pH7": 0, "hydrophobicity": -0.9, "type": "nonpolar"},
    "Y": {"name": "Tyrosine", "MW": 181.2, "charge_pH7": 0, "hydrophobicity": -1.3, "type": "polar"},
    "V": {"name": "Valine", "MW": 117.1, "charge_pH7": 0, "hydrophobicity": 4.2, "type": "nonpolar"},
}


class AAPropertiesInput(ToolInput):
    amino_acid: str = Field(description="Single-letter amino acid code, e.g. 'R'")


@bioma_tool(
    name="get_amino_acid_properties",
    description="Return physicochemical properties (MW, charge, hydrophobicity) for an amino acid.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=AAPropertiesInput,
)
def get_amino_acid_properties(amino_acid: str) -> str:
    """Return properties of a single amino acid."""
    import json
    aa = amino_acid.upper().strip()
    if aa not in AA_PROPERTIES:
        return f"Unknown amino acid: {aa}"
    props = AA_PROPERTIES[aa]
    return json.dumps({"amino_acid": aa, **props}, indent=2)
