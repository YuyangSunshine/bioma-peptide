"""R7 selection helper tools."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import Field

from bioma.tools.base import ToolInput, bioma_tool


R7_BACKBONE_ID = "r6_6h09"
R7_COMBINATION_FIELDNAMES = [
    "candidate_id",
    "backbone_id",
    "mutations_vs_backbone",
    "design_strategy",
    "construction_notes",
    "risk_flags",
]
R7_DEFAULT_LOAD_QUOTAS = {"3-4": 35, "5-7": 45, "8-9": 20}
MUTATION_RE = re.compile(r"^[A-Z]\d+[A-Z*]$")


@dataclass(frozen=True)
class R7MutationOption:
    mutation: str
    evidence: str = ""
    strategy: str = ""
    risk: str = ""
    combo_suggestion: str = ""


@dataclass(frozen=True)
class R7GeneratedCombination:
    mutations: tuple[str, ...]
    score: float
    strategy: str
    notes: str
    risks: str


class R7CandidateGenerationInput(ToolInput):
    options_yaml_path: str = Field(description="Input R7 mutation options YAML path.")
    output_csv_path: str = Field(
        default="shishiyan/r7_step3_1_mutation_combinations.csv",
        description="Output step 3.1 candidate-combination CSV path.",
    )
    candidate_count: int = Field(default=100, description="Number of unique candidates to generate.")
    min_mutations: int = Field(default=3, description="Minimum added mutations per candidate.")
    max_mutations: int = Field(default=9, description="Maximum added mutations per candidate.")
    strategy_plan_json: Optional[str] = Field(
        default="",
        description=(
            "Optional concise JSON from the agent with biological preferences, e.g. "
            '{"load_quotas":{"3-4":35,"5-7":45,"8-9":20},'
            '"preferred_anchors":["N6D","I65V","H160L"],'
            '"max_medium_risk_per_candidate":1}.'
        ),
    )


@bioma_tool(
    name="generate_r7_candidate_combinations",
    description=(
        "Generate a deduplicated R7 step 3.1 candidate mutation-combination CSV from "
        "the mutation-options YAML. The agent supplies high-level biological preferences; "
        "the tool handles enumeration, forbidden pairings, canonical ordering, and CSV validation."
    ),
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=R7CandidateGenerationInput,
)
def generate_r7_candidate_combinations(
    options_yaml_path: str,
    output_csv_path: str = "shishiyan/r7_step3_1_mutation_combinations.csv",
    candidate_count: int = 100,
    min_mutations: int = 3,
    max_mutations: int = 9,
    strategy_plan_json: str = "",
) -> str:
    """Generate R7 candidate mutation combinations without forcing the LLM to write a large CSV."""
    import json
    from pathlib import Path

    try:
        plan = _parse_generation_plan(strategy_plan_json)
        requested = max(1, int(candidate_count))
        options = _read_generation_options(options_yaml_path)
        backbone_id = _resolve_generation_backbone(options)
        mutations = _load_generation_mutations(options, backbone_id)
        min_n = max(1, int(min_mutations))
        max_n = min(int(max_mutations), len(mutations))
        if max_n < min_n:
            raise ValueError(f"invalid mutation range: min={min_n}, max={max_n}")

        candidates = _enumerate_generation_candidates(
            mutations=mutations,
            blocked=_load_blocked_mutations(options),
            forbidden=_load_forbidden_pairings(options),
            min_n=min_n,
            max_n=max_n,
            plan=plan,
        )
        selected, duplicates_skipped = _select_generation_candidates(
            candidates,
            _generation_count_ranges(min_n, max_n, plan, requested),
            requested,
        )
        rows = _generation_rows(selected, backbone_id, min_n, max_n)
        _write_csv(Path(output_csv_path), R7_COMBINATION_FIELDNAMES, rows)

        return json.dumps({
            "status": "ok",
            "output_csv_path": output_csv_path,
            "candidate_count": len(rows),
            "backbone_id": backbone_id,
            "available_mutations": [item.mutation for item in mutations],
            "load_counts": _load_counts(rows),
            "duplicates_skipped": duplicates_skipped,
            "strategy_plan_used": plan,
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class R7CandidateFilterInput(ToolInput):
    matrix_csv_path: str = Field(description="Input candidate matrix CSV path.")
    output_csv_path: str = Field(description="Output shortlist CSV path.")
    output_summary_md_path: str = Field(description="Output Markdown summary path.")
    stage: Literal["esm", "developability", "structure"] = Field(description="Funnel stage used for ranking.")
    limit: int = Field(default=30, description="Number of candidates to keep.")


@bioma_tool(
    name="filter_r7_candidate_matrix",
    description="Filter and rank an R7 candidate matrix for the ESM, developability, or structure-screening funnel stage.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=R7CandidateFilterInput,
)
def filter_r7_candidate_matrix(
    matrix_csv_path: str,
    output_csv_path: str,
    output_summary_md_path: str,
    stage: Literal["esm", "developability", "structure"],
    limit: int = 30,
) -> str:
    """Write a ranked shortlist CSV from a larger R7 matrix."""
    import csv
    import json
    from pathlib import Path

    matrix_path = Path(matrix_csv_path)
    if not matrix_path.exists():
        return json.dumps({"status": "error", "error": f"matrix not found: {matrix_csv_path}"}, ensure_ascii=False)
    with matrix_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    if not rows:
        return json.dumps({"status": "error", "error": "matrix has no rows"}, ensure_ascii=False)
    _ensure_canonical_fields(rows)
    if "canonical_mutations" not in fieldnames:
        fieldnames.append("canonical_mutations")

    eligible = [row for row in rows if _passes_hard_filters(row)]
    ranked = sorted(eligible, key=lambda row: _rank_score(row, stage), reverse=True)
    selected, duplicates_removed = _unique_ranked_rows(ranked, max(1, int(limit)))
    selected_ids = {str(row.get("candidate_id", "")).strip() for row in selected}
    rejected_source = [row for row in ranked if str(row.get("candidate_id", "")).strip() not in selected_ids]
    rejected, _ = _unique_ranked_rows(rejected_source, 12)

    out_path = Path(output_csv_path)
    _write_csv(out_path, fieldnames, selected)
    Path(output_summary_md_path).write_text(_filter_summary_md(stage, selected, rejected), encoding="utf-8")

    return json.dumps({
        "status": "ok",
        "stage": stage,
        "input_rows": len(rows),
        "eligible_rows": len(eligible),
        "duplicates_removed": duplicates_removed,
        "selected_rows": len(selected),
        "selected_ids": [str(row.get("candidate_id", "")).strip() for row in selected],
        "output_csv_path": output_csv_path,
        "output_summary_md_path": output_summary_md_path,
    }, ensure_ascii=False, indent=2)


class R7AutoTop5Input(ToolInput):
    matrix_csv_path: str = Field(description="Path to final structure-screened matrix CSV.")
    output_top5_csv_path: str = Field(default="shishiyan/r7_step3_5_top5.csv", description="Project-relative Top 5 CSV output path.")
    output_rationale_md_path: str = Field(default="shishiyan/r7_step3_5_top5_rationale.md", description="Project-relative rationale Markdown output path.")
    output_rejected_md_path: str = Field(default="shishiyan/r7_step3_5_rejected_near_misses.md", description="Project-relative rejected near-misses Markdown output path.")


@bioma_tool(
    name="write_r7_top5_auto_artifacts",
    description="Automatically select and write R7 Top 5 artifacts from the final structure-screened matrix.",
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=R7AutoTop5Input,
)
def write_r7_top5_auto_artifacts(
    matrix_csv_path: str,
    output_top5_csv_path: str = "shishiyan/r7_step3_5_top5.csv",
    output_rationale_md_path: str = "shishiyan/r7_step3_5_top5_rationale.md",
    output_rejected_md_path: str = "shishiyan/r7_step3_5_rejected_near_misses.md",
) -> str:
    """Select the final Top 5 by the structure-stage ranking and write artifacts."""
    import csv
    import json
    from pathlib import Path

    matrix_path = Path(matrix_csv_path)
    if not matrix_path.exists():
        return json.dumps({"status": "error", "error": f"matrix not found: {matrix_csv_path}"}, ensure_ascii=False)
    with matrix_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return json.dumps({"status": "error", "error": "matrix has no rows"}, ensure_ascii=False)
    _ensure_canonical_fields(rows)

    ranked = sorted(
        [row for row in rows if _passes_hard_filters(row) and _has_usable_fold_structure(row)],
        key=_final_top5_score,
        reverse=True,
    )
    ranked, _ = _unique_ranked_rows(ranked, len(ranked))
    if len(ranked) < 5:
        return json.dumps({
            "status": "error",
            "error": f"need at least 5 eligible rows with fold_pdb_path and fold_plddt_mean, found {len(ranked)}",
        }, ensure_ascii=False)

    selected_rows = _select_diverse_final_top5(ranked)
    selected_set = {str(row.get("candidate_id", "")).strip() for row in selected_rows}
    rejected_rows = [row for row in ranked if str(row.get("candidate_id", "")).strip() not in selected_set]
    selected_ids = ",".join(str(row.get("candidate_id", "")).strip() for row in selected_rows)
    rejected_ids = ",".join(str(row.get("candidate_id", "")).strip() for row in rejected_rows[:8])
    return write_r7_top5_artifacts(
        matrix_csv_path=matrix_csv_path,
        selected_ids=selected_ids,
        output_top5_csv_path=output_top5_csv_path,
        output_rationale_md_path=output_rationale_md_path,
        output_rejected_md_path=output_rejected_md_path,
        rejected_ids=rejected_ids,
    )


class R7Top5ArtifactInput(ToolInput):
    matrix_csv_path: str = Field(description="Path to the final scored R7 candidate matrix CSV.")
    selected_ids: str = Field(description="Comma-separated candidate IDs in final rank order, exactly five IDs.")
    output_top5_csv_path: str = Field(default="shishiyan/r7_step3_5_top5.csv", description="Project-relative Top 5 CSV output path.")
    output_rationale_md_path: str = Field(default="shishiyan/r7_step3_5_top5_rationale.md", description="Project-relative rationale Markdown output path.")
    output_rejected_md_path: str = Field(default="shishiyan/r7_step3_5_rejected_near_misses.md", description="Project-relative rejected near-misses Markdown output path.")
    selection_notes_json: Optional[str] = Field(
        default="",
        description=(
            "Optional JSON object keyed by candidate_id. Each value may contain "
            "main_advantage, main_risk, and why_not_redundant strings."
        ),
    )
    rejected_ids: Optional[str] = Field(
        default="",
        description="Optional comma-separated rejected near-miss candidate IDs.",
    )


@bioma_tool(
    name="write_r7_top5_artifacts",
    description=(
        "Write R7 Top 5 output artifacts from a scored mutation matrix and a concise "
        "agent-selected candidate ID list. The tool copies full sequences from the CSV "
        "so the agent does not need to place long sequences in tool arguments."
    ),
    category="analysis",
    version="1.0.0",
    author="PredAgent Team",
    args_schema=R7Top5ArtifactInput,
)
def write_r7_top5_artifacts(
    matrix_csv_path: str,
    selected_ids: str,
    output_top5_csv_path: str = "shishiyan/r7_step3_5_top5.csv",
    output_rationale_md_path: str = "shishiyan/r7_step3_5_top5_rationale.md",
    output_rejected_md_path: str = "shishiyan/r7_step3_5_rejected_near_misses.md",
    selection_notes_json: str = "",
    rejected_ids: str = "",
) -> str:
    """Materialize Top 5 CSV and rationale files from selected candidate IDs."""
    import csv
    import json
    from pathlib import Path

    matrix_path = Path(matrix_csv_path)
    if not matrix_path.exists():
        return json.dumps({"status": "error", "error": f"matrix not found: {matrix_csv_path}"}, ensure_ascii=False)

    with matrix_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        return json.dumps({"status": "error", "error": "matrix has no rows"}, ensure_ascii=False)

    _ensure_canonical_fields(rows)
    by_id = {str(row.get("candidate_id", "")).strip(): row for row in rows}
    selected = _parse_id_list(selected_ids)
    if len(selected) != 5:
        return json.dumps({"status": "error", "error": "selected_ids must contain exactly five IDs"}, ensure_ascii=False)
    missing = [candidate_id for candidate_id in selected if candidate_id not in by_id]
    if missing:
        return json.dumps({"status": "error", "error": f"unknown selected IDs: {', '.join(missing)}"}, ensure_ascii=False)

    notes = _parse_notes(selection_notes_json)
    selected_rows = [by_id[candidate_id] for candidate_id in selected]
    rejected = [candidate_id for candidate_id in _parse_id_list(rejected_ids) if candidate_id in by_id and candidate_id not in selected]
    if not rejected:
        rejected = _default_rejected(rows, set(selected))

    top5_fieldnames = [
        "rank",
        "variant_id",
        "backbone_id",
        "mutations_vs_backbone",
        "canonical_mutations",
        "full_sequence",
        "design_strategy",
        "esm_contextual_llr",
        "esm_epistasis_delta",
        "pptstab_tm_c",
        "baseline_pptstab_tm_c",
        "delta_pptstab_tm_c",
        "netsolp_solubility",
        "delta_netsolp_solubility",
        "netsolp_usability",
        "delta_netsolp_usability",
        "deeptp_score",
        "delta_deeptp_score",
        "deeptp_label",
        "ephod_optimal_ph",
        "delta_ephod_target_distance",
        "structure_status",
        "fold_tool",
        "fold_status",
        "fold_structure_url",
        "fold_pdb_path",
        "fold_plddt_mean",
        "baseline_fold_plddt_mean",
        "delta_plddt_mean",
        "fold_plddt_pct_lt50",
        "baseline_fold_plddt_pct_lt50",
        "delta_plddt_pct_lt50",
        "fold_plddt_pct_ge70",
        "baseline_fold_plddt_pct_ge70",
        "delta_plddt_pct_ge70",
        "evoef2_status",
        "evoef2_output_url",
        "evoef2_energy",
        "baseline_evoef2_status",
        "baseline_evoef2_output_url",
        "baseline_evoef2_energy",
        "delta_evoef2_energy",
        "structure_risk_flags",
        "main_advantage",
        "main_risk",
        "why_not_redundant",
    ]
    top5_rows = []
    for rank, row in enumerate(selected_rows, start=1):
        candidate_id = str(row.get("candidate_id", "")).strip()
        note = notes.get(candidate_id, {})
        advantage, risk, nonredundant = _auto_explain(row)
        top5_rows.append({
            "rank": str(rank),
            "variant_id": candidate_id,
            "backbone_id": str(row.get("backbone_id", "")).strip(),
            "mutations_vs_backbone": str(row.get("mutations_vs_backbone", "")).strip(),
            "canonical_mutations": str(row.get("canonical_mutations", "")).strip(),
            "full_sequence": str(row.get("full_sequence", "")).strip(),
            "design_strategy": str(row.get("design_strategy", "")).strip(),
            "esm_contextual_llr": str(row.get("esm_contextual_llr", "")).strip(),
            "esm_epistasis_delta": str(row.get("esm_epistasis_delta", "")).strip(),
            "pptstab_tm_c": str(row.get("pptstab_tm_c", "")).strip(),
            "baseline_pptstab_tm_c": str(row.get("baseline_pptstab_tm_c", "")).strip(),
            "delta_pptstab_tm_c": str(row.get("delta_pptstab_tm_c", "")).strip(),
            "netsolp_solubility": str(row.get("netsolp_solubility", "")).strip(),
            "delta_netsolp_solubility": str(row.get("delta_netsolp_solubility", "")).strip(),
            "netsolp_usability": str(row.get("netsolp_usability", "")).strip(),
            "delta_netsolp_usability": str(row.get("delta_netsolp_usability", "")).strip(),
            "deeptp_score": str(row.get("deeptp_score", "")).strip(),
            "delta_deeptp_score": str(row.get("delta_deeptp_score", "")).strip(),
            "deeptp_label": str(row.get("deeptp_label", "")).strip(),
            "ephod_optimal_ph": str(row.get("ephod_optimal_ph", "")).strip(),
            "delta_ephod_target_distance": str(row.get("delta_ephod_target_distance", "")).strip(),
            "structure_status": str(row.get("structure_status", "")).strip(),
            "fold_tool": str(row.get("fold_tool", "")).strip(),
            "fold_status": str(row.get("fold_status", "")).strip(),
            "fold_structure_url": str(row.get("fold_structure_url", "")).strip(),
            "fold_pdb_path": str(row.get("fold_pdb_path", "")).strip(),
            "fold_plddt_mean": str(row.get("fold_plddt_mean", "")).strip(),
            "baseline_fold_plddt_mean": str(row.get("baseline_fold_plddt_mean", "")).strip(),
            "delta_plddt_mean": str(row.get("delta_plddt_mean", "")).strip(),
            "fold_plddt_pct_lt50": str(row.get("fold_plddt_pct_lt50", "")).strip(),
            "baseline_fold_plddt_pct_lt50": str(row.get("baseline_fold_plddt_pct_lt50", "")).strip(),
            "delta_plddt_pct_lt50": str(row.get("delta_plddt_pct_lt50", "")).strip(),
            "fold_plddt_pct_ge70": str(row.get("fold_plddt_pct_ge70", "")).strip(),
            "baseline_fold_plddt_pct_ge70": str(row.get("baseline_fold_plddt_pct_ge70", "")).strip(),
            "delta_plddt_pct_ge70": str(row.get("delta_plddt_pct_ge70", "")).strip(),
            "evoef2_status": str(row.get("evoef2_status", "")).strip(),
            "evoef2_output_url": str(row.get("evoef2_output_url", "")).strip(),
            "evoef2_energy": str(row.get("evoef2_energy", "")).strip(),
            "baseline_evoef2_status": str(row.get("baseline_evoef2_status", "")).strip(),
            "baseline_evoef2_output_url": str(row.get("baseline_evoef2_output_url", "")).strip(),
            "baseline_evoef2_energy": str(row.get("baseline_evoef2_energy", "")).strip(),
            "delta_evoef2_energy": str(row.get("delta_evoef2_energy", "")).strip(),
            "structure_risk_flags": str(row.get("structure_risk_flags", "")).strip(),
            "main_advantage": str(note.get("main_advantage") or advantage),
            "main_risk": str(note.get("main_risk") or risk),
            "why_not_redundant": str(note.get("why_not_redundant") or nonredundant),
        })

    _write_csv(Path(output_top5_csv_path), top5_fieldnames, top5_rows)
    Path(output_rationale_md_path).write_text(_rationale_md(selected_rows), encoding="utf-8")
    Path(output_rejected_md_path).write_text(_rejected_md([by_id[candidate_id] for candidate_id in rejected]), encoding="utf-8")

    return json.dumps({
        "status": "ok",
        "selected_ids": selected,
        "rejected_ids": rejected,
        "outputs": {
            "top5_csv": output_top5_csv_path,
            "rationale_md": output_rationale_md_path,
            "rejected_md": output_rejected_md_path,
        },
    }, ensure_ascii=False, indent=2)


def _parse_generation_plan(raw: str) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_generation_options(path: str) -> dict[str, Any]:
    from pathlib import Path

    import yaml

    options_path = Path(path)
    if not options_path.exists():
        raise ValueError(f"options YAML not found: {path}")
    data = yaml.safe_load(options_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("options YAML must be a mapping")
    return data


def _resolve_generation_backbone(options: dict[str, Any]) -> str:
    backbones = options.get("backbones")
    if not isinstance(backbones, dict) or R7_BACKBONE_ID not in backbones:
        raise ValueError("R7 generation requires exactly the r6_6h09 backbone in options YAML")
    return R7_BACKBONE_ID


def _load_generation_mutations(options: dict[str, Any], backbone_id: str) -> list[R7MutationOption]:
    blocked = _load_blocked_mutations(options)
    raw = options.get("candidate_mutations")
    if not isinstance(raw, list):
        raise ValueError("options YAML missing candidate_mutations list")

    result: list[R7MutationOption] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        mutation = str(item.get("mutation", "")).strip()
        applicable = item.get("applicable_to") or []
        applicable = [str(value) for value in applicable] if isinstance(applicable, list) else []
        if mutation in seen or mutation in blocked or mutation == "F249L":
            continue
        if not MUTATION_RE.match(mutation) or mutation.endswith("*"):
            continue
        if backbone_id not in applicable:
            continue
        seen.add(mutation)
        result.append(R7MutationOption(
            mutation=mutation,
            evidence=str(item.get("evidence", "")).strip(),
            strategy=str(item.get("strategy", "")).strip(),
            risk=str(item.get("risk", "")).strip(),
            combo_suggestion=str(item.get("combo_suggestion", item.get("combination", ""))).strip(),
        ))
    if not result:
        raise ValueError("no usable candidate mutations for r6_6h09")
    return result


def _load_blocked_mutations(options: dict[str, Any]) -> set[str]:
    raw = options.get("blocked_mutations") or []
    blocked: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                mutation = str(item.get("mutation", "")).strip()
            else:
                mutation = str(item).strip()
            if mutation:
                blocked.add(mutation)
    return blocked


def _load_forbidden_pairings(options: dict[str, Any]) -> list[tuple[str, str]]:
    rules = options.get("combination_rules") or {}
    raw = rules.get("forbidden_pairings") if isinstance(rules, dict) else []
    forbidden: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                forbidden.append((str(item[0]).strip(), str(item[1]).strip()))
    return forbidden


def _generation_count_ranges(
    min_n: int,
    max_n: int,
    plan: dict[str, Any],
    requested: int,
) -> list[tuple[str, int]]:
    raw = plan.get("load_quotas")
    quotas = R7_DEFAULT_LOAD_QUOTAS
    if isinstance(raw, dict):
        parsed: dict[str, int] = {}
        for key in R7_DEFAULT_LOAD_QUOTAS:
            try:
                parsed[key] = max(0, int(raw.get(key, 0)))
            except (TypeError, ValueError):
                parsed[key] = 0
        if sum(parsed.values()) > 0:
            quotas = parsed

    usable: list[tuple[str, int]] = []
    for label, quota in quotas.items():
        if label == "3-4" and max_n >= 3 and min_n <= 4:
            usable.append((label, quota))
        elif label == "5-7" and max_n >= 5 and min_n <= 7:
            usable.append((label, quota))
        elif label == "8-9" and max_n >= 8 and min_n <= 9:
            usable.append((label, quota))
    if not usable:
        return [("all", requested)]

    total = sum(quota for _, quota in usable)
    if total <= 0:
        return [("all", requested)]
    scaled = [(label, max(0, round(requested * quota / total))) for label, quota in usable]
    delta = requested - sum(quota for _, quota in scaled)
    idx = 0
    while delta:
        label, quota = scaled[idx % len(scaled)]
        scaled[idx % len(scaled)] = (label, quota + (1 if delta > 0 else -1))
        delta += -1 if delta > 0 else 1
        idx += 1
    return scaled


def _combo_in_range(count: int, label: str) -> bool:
    if label == "3-4":
        return 3 <= count <= 4
    if label == "5-7":
        return 5 <= count <= 7
    if label == "8-9":
        return 8 <= count <= 9
    return True


def _combo_has_forbidden_pair(combo: tuple[str, ...], forbidden: list[tuple[str, str]]) -> bool:
    values = set(combo)
    return any(left in values and right in values for left, right in forbidden)


def _combo_has_same_position_mutations(combo: tuple[str, ...]) -> bool:
    positions: set[int] = set()
    for mutation in combo:
        match = re.match(r"^[A-Z](\d+)[A-Z*]$", mutation)
        if match is None:
            continue
        position = int(match.group(1))
        if position in positions:
            return True
        positions.add(position)
    return False


def _enumerate_generation_candidates(
    mutations: list[R7MutationOption],
    blocked: set[str],
    forbidden: list[tuple[str, str]],
    min_n: int,
    max_n: int,
    plan: dict[str, Any],
) -> list[R7GeneratedCombination]:
    candidates: list[R7GeneratedCombination] = []
    names = [item.mutation for item in mutations]
    meta = {item.mutation: item for item in mutations}
    for size in range(min_n, max_n + 1):
        for raw_combo in itertools.combinations(names, size):
            if _combo_has_same_position_mutations(raw_combo):
                continue
            if _combo_has_forbidden_pair(raw_combo, forbidden):
                continue
            if any(mutation in blocked or mutation.endswith("*") for mutation in raw_combo):
                continue
            combo = tuple(_canonical_mutations_from_raw(",".join(raw_combo)).split(","))
            candidates.append(R7GeneratedCombination(
                mutations=combo,
                score=_generation_score(combo, meta, plan),
                strategy=_generation_strategy(combo, meta),
                notes=_generation_notes(combo, meta),
                risks=_generation_risks(combo, meta),
            ))
    if not candidates:
        raise ValueError("no valid mutation combinations generated")
    return candidates


def _select_generation_candidates(
    candidates: list[R7GeneratedCombination],
    count_ranges: list[tuple[str, int]],
    requested: int,
) -> tuple[list[R7GeneratedCombination], int]:
    selected: list[R7GeneratedCombination] = []
    seen: set[str] = set()
    duplicates = 0

    for label, quota in count_ranges:
        bucket = [item for item in candidates if _combo_in_range(len(item.mutations), label)]
        kept, skipped = _take_unique(bucket, seen, quota)
        selected.extend(kept)
        duplicates += skipped
        if len(selected) >= requested:
            return selected[:requested], duplicates

    if len(selected) < requested:
        kept, skipped = _take_unique(candidates, seen, requested - len(selected))
        selected.extend(kept)
        duplicates += skipped

    if len(selected) < requested:
        raise ValueError(f"only {len(selected)} unique valid candidates available; requested {requested}")
    return selected[:requested], duplicates


def _take_unique(
    candidates: list[R7GeneratedCombination],
    seen: set[str],
    limit: int,
) -> tuple[list[R7GeneratedCombination], int]:
    selected: list[R7GeneratedCombination] = []
    duplicates = 0
    for item in sorted(candidates, key=lambda candidate: (-candidate.score, candidate.mutations)):
        key = ",".join(item.mutations)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected, duplicates


def _generation_rows(
    candidates: list[R7GeneratedCombination],
    backbone_id: str,
    min_n: int,
    max_n: int,
) -> list[dict[str, str]]:
    rows = []
    for idx, candidate in enumerate(candidates, start=1):
        invalid = [mutation for mutation in candidate.mutations if not MUTATION_RE.match(mutation)]
        if invalid:
            raise ValueError(f"invalid mutation format: {invalid}")
        if not min_n <= len(candidate.mutations) <= max_n:
            raise ValueError(f"invalid mutation count in {','.join(candidate.mutations)}")
        rows.append({
            "candidate_id": f"C{idx:03d}",
            "backbone_id": backbone_id,
            "mutations_vs_backbone": ",".join(candidate.mutations),
            "design_strategy": candidate.strategy,
            "construction_notes": candidate.notes,
            "risk_flags": candidate.risks,
        })
    return rows


def _load_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts = {"3-4": 0, "5-7": 0, "8-9": 0, "other": 0}
    for row in rows:
        count = len(str(row.get("mutations_vs_backbone", "")).split(","))
        if 3 <= count <= 4:
            counts["3-4"] += 1
        elif 5 <= count <= 7:
            counts["5-7"] += 1
        elif 8 <= count <= 9:
            counts["8-9"] += 1
        else:
            counts["other"] += 1
    return counts


def _generation_score(combo: tuple[str, ...], meta: dict[str, R7MutationOption], plan: dict[str, Any]) -> float:
    preferred = plan.get("preferred_anchors")
    preferred_set = {str(item) for item in preferred} if isinstance(preferred, list) else set()
    try:
        max_medium = int(plan.get("max_medium_risk_per_candidate", 2))
    except (TypeError, ValueError):
        max_medium = 2

    score = 0.0
    medium_count = 0
    for mutation in combo:
        info = meta.get(mutation, R7MutationOption(mutation))
        risk = info.risk.lower()
        strategy = info.strategy.lower()
        evidence = info.evidence.lower()
        if "low" in risk:
            score += 3.0
        elif "medium" in risk:
            score += 1.1
            medium_count += 1
        else:
            score += 1.5
        if mutation in preferred_set:
            score += 1.0
        if "tm" in evidence or "highest" in evidence or "top" in evidence:
            score += 0.7
        if "pH".lower() in strategy or mutation.startswith("H160"):
            score += 0.3
        if "charge" in strategy or mutation in {"N6D", "K236E", "E66K"}:
            score += 0.2

    pairs = {tuple(sorted(pair)) for pair in __import__("itertools").combinations(combo, 2)}
    bonuses = [
        ("I65V", "N6D", 1.4),
        ("I65V", "N91T", 1.2),
        ("I65V", "A222V", 0.8),
        ("N6D", "V1I", 1.0),
        ("N6D", "S10T", 1.0),
        ("V1I", "S10T", 0.8),
        ("K236E", "A222V", 1.2),
        ("K236E", "N91T", 0.7),
        ("H160L", "N6D", 0.5),
        ("H160R", "N6D", 0.4),
    ]
    for left, right, bonus in bonuses:
        if tuple(sorted((left, right))) in pairs:
            score += bonus

    if "I174S" in combo:
        score -= 0.6
        if 3 <= len(combo) <= 5:
            score += 1.0
        if "A222V" in combo:
            score -= 1.2
    if "E66K" in combo:
        score -= 0.6
    if medium_count > max_medium:
        score -= 3.0 * (medium_count - max_medium)

    count = len(combo)
    if 5 <= count <= 7:
        score += 0.6
    elif 8 <= count <= 9:
        score += 0.2
    return score


def _generation_strategy(combo: tuple[str, ...], meta: dict[str, R7MutationOption]) -> str:
    if len(combo) >= 8:
        return "high_load_exploration"
    if "I174S" in combo:
        return "I174S_controlled_epistasis"
    if "E66K" in combo or "K236E" in combo:
        return "surface_charge_compensation"
    if any(mutation.startswith("H160") for mutation in combo):
        return "pH_stability_focus"
    if len(combo) <= 4 and all("medium" not in meta.get(m, R7MutationOption(m)).risk.lower() for m in combo):
        return "conservative_enhancement"
    return "balanced_combination"


def _generation_notes(combo: tuple[str, ...], meta: dict[str, R7MutationOption]) -> str:
    notes: list[str] = []
    if "I174S" in combo:
        notes.append("controlled_I174S_epistasis_test")
    if any(mutation.startswith("H160") for mutation in combo):
        notes.append("tests_H160_pH_sensitive_site")
    if "K236E" in combo or "A222V" in combo:
        notes.append("C_terminal_satellite_component")
    if "V1I" in combo or "S10T" in combo or "N6D" in combo:
        notes.append("N_terminal_satellite_component")
    if "E66K" in combo:
        notes.append("E66K_kept_separate_from_I65V")
    risk_notes = [
        f"{mutation}:{meta[mutation].risk}"
        for mutation in combo
        if mutation in meta and meta[mutation].risk
    ]
    notes.extend(risk_notes[:3])
    return ";".join(item for item in notes if item) or "balanced_yaml_supported_combination"


def _generation_risks(combo: tuple[str, ...], meta: dict[str, R7MutationOption]) -> str:
    risks: list[str] = []
    medium = [
        mutation for mutation in combo
        if "medium" in meta.get(mutation, R7MutationOption(mutation)).risk.lower()
    ]
    if medium:
        risks.append("medium_risk:" + "+".join(medium))
    if "I174S" in combo:
        risks.append("branch_dependent_epistasis_probe")
    if "E66K" in combo:
        risks.append("charge_reversal_probe")
    if len(combo) >= 8:
        risks.append("high_mutation_load")
    if not risks:
        risks.append("low")
    return ";".join(risks)


def _parse_id_list(raw: str) -> list[str]:
    import re

    return [item.strip() for item in re.split(r"[,;\s]+", str(raw or "")) if item.strip()]


def _parse_notes(raw: str) -> dict[str, dict[str, str]]:
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            parsed[str(key)] = {str(k): str(v) for k, v in value.items()}
    return parsed


def _write_csv(path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = __import__("csv").DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _float_value(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        value = str(row.get(key, "")).strip()
        return float(value) if value else default
    except ValueError:
        return default


def _mutation_count(row: dict[str, str]) -> int:
    import re

    raw = str(row.get("mutations_vs_backbone", "")).strip()
    if not raw or raw.lower() in {"none", "na", "n/a", "-"}:
        return 0
    return len([item for item in re.split(r"[,;\s]+", raw) if item.strip()])


def _canonical_mutations_from_raw(raw: str) -> str:
    import re

    mutation_re = re.compile(r"^([A-Z])(\d+)([A-Z*])$")
    text = str(raw or "").strip()
    if not text or text.lower() in {"none", "na", "n/a", "-"}:
        return ""
    mutations = [item.strip() for item in re.split(r"[,;]\s*|\s+", text) if item.strip()]

    def sort_key(mutation: str) -> tuple[int, str]:
        match = mutation_re.match(mutation)
        if match is None:
            return (10**9, mutation)
        return (int(match.group(2)), mutation)

    return ",".join(sorted(dict.fromkeys(mutations), key=sort_key))


def _ensure_canonical_fields(rows: list[dict[str, str]]) -> None:
    for row in rows:
        canonical = str(row.get("canonical_mutations", "")).strip()
        if not canonical:
            canonical = _canonical_mutations_from_raw(str(row.get("mutations_vs_backbone", "")))
            row["canonical_mutations"] = canonical
        if canonical:
            row["mutations_vs_backbone"] = canonical


def _unique_ranked_rows(rows: list[dict[str, str]], limit: int) -> tuple[list[dict[str, str]], int]:
    selected: list[dict[str, str]] = []
    seen_mutations: set[str] = set()
    seen_sequences: set[str] = set()
    duplicates = 0
    for row in rows:
        canonical = str(row.get("canonical_mutations", "")).strip()
        sequence = str(row.get("full_sequence", "")).strip()
        mutation_key = canonical or str(row.get("mutations_vs_backbone", "")).strip()
        if (mutation_key and mutation_key in seen_mutations) or (sequence and sequence in seen_sequences):
            duplicates += 1
            continue
        if mutation_key:
            seen_mutations.add(mutation_key)
        if sequence:
            seen_sequences.add(sequence)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected, duplicates


def _has_usable_fold_structure(row: dict[str, str]) -> bool:
    return (
        str(row.get("fold_pdb_path", "")).strip()
        and str(row.get("fold_plddt_mean", "")).strip()
    )


def _select_diverse_final_top5(ranked: list[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    available_non_i174s = any("I174S" not in str(row.get("canonical_mutations", row.get("mutations_vs_backbone", ""))) for row in ranked)
    max_i174s = 4 if available_non_i174s else 5
    max_high_load = 1 if any("high_load" not in str(row.get("design_strategy", "")).lower() for row in ranked) else 5
    available_low_risk = any("medium" not in str(row.get("risk_flags", "")).lower() for row in ranked)
    min_low_risk = 1 if available_low_risk else 0

    def can_add(row: dict[str, str]) -> bool:
        mutations = str(row.get("canonical_mutations", row.get("mutations_vs_backbone", "")))
        strategy = str(row.get("design_strategy", "")).lower()
        if "I174S" in mutations and sum("I174S" in str(item.get("canonical_mutations", item.get("mutations_vs_backbone", ""))) for item in selected) >= max_i174s:
            return False
        if "high_load" in strategy and sum("high_load" in str(item.get("design_strategy", "")).lower() for item in selected) >= max_high_load:
            return False
        return True

    for row in ranked:
        if can_add(row):
            selected.append(row)
        if len(selected) == 5:
            break

    if min_low_risk and selected and not any("medium" not in str(row.get("risk_flags", "")).lower() for row in selected):
        low_risk = next((row for row in ranked if "medium" not in str(row.get("risk_flags", "")).lower()), None)
        if low_risk is not None and low_risk not in selected:
            selected[-1] = low_risk

    if len(selected) == 5:
        return selected

    for row in ranked:
        if row not in selected:
            selected.append(row)
        if len(selected) == 5:
            return selected
    return selected


def _final_top5_score(row: dict[str, str]) -> float:
    """Final R7 score: sequence and developability first, weak structure as a guardrail."""
    if str(row.get("esm_status", "")).strip() != "available":
        return -1000.0

    contextual = _float_value(row, "esm_contextual_llr", 0.0)
    epistasis = _float_value(row, "esm_epistasis_delta", 0.0)
    delta_tm = _float_value(row, "delta_pptstab_tm_c", 0.0)
    delta_sol = _float_value(row, "delta_netsolp_solubility", 0.0)
    delta_usability = _float_value(row, "delta_netsolp_usability", 0.0)
    delta_deeptp = _float_value(row, "delta_deeptp_score", 0.0)
    delta_evoef2_raw = str(row.get("delta_evoef2_energy", "")).strip()
    delta_evoef2 = _float_value(row, "delta_evoef2_energy", 0.0)
    risk_text = f"{row.get('risk_flags', '')} {row.get('design_strategy', '')}".lower()

    score = min(3.2, max(-2.0, contextual))
    if epistasis >= 0.10:
        score += 1.2
    elif epistasis > 0.0:
        score += 0.7
    elif epistasis <= -0.20:
        score -= 1.0
    elif epistasis <= -0.10:
        score -= 0.7
    elif str(row.get("esm_epistasis_delta", "")).strip():
        score -= 0.25

    if delta_tm >= 0.0:
        score += min(2.0, 0.30 * delta_tm)
    else:
        score -= min(2.0, abs(delta_tm))

    if delta_sol >= 0.05:
        score += 1.0
    elif delta_sol > 0.0:
        score += 0.45
    elif delta_sol <= -0.05:
        score -= 1.0
    elif delta_sol < 0.0:
        score -= 0.35

    if delta_usability >= 0.02:
        score += 0.5
    elif delta_usability > 0.0:
        score += 0.2
    elif delta_usability < 0.0:
        score -= 0.25
    score += max(-0.4, min(0.4, delta_deeptp))

    if delta_evoef2_raw:
        if delta_evoef2 <= -100.0:
            score += 0.8
        elif delta_evoef2 < 0.0:
            score += 0.4
        else:
            score -= 1.2
    else:
        score -= 0.5

    plddt = _float_value(row, "fold_plddt_mean", 0.0)
    delta_plddt = _float_value(row, "delta_plddt_mean", 0.0)
    if plddt and plddt < 50.0:
        score -= 0.3
    if delta_plddt < -2.0:
        score -= 0.4
    elif delta_plddt > 2.0:
        score += 0.2

    if "medium" in risk_text:
        score -= 0.6
    if "high_load" in risk_text:
        score -= 0.6
    if "charge_reversal" in risk_text:
        score -= 0.3
    return score


def _passes_hard_filters(row: dict[str, str]) -> bool:
    mutations = str(row.get("mutations_vs_backbone", ""))
    sequence = str(row.get("full_sequence", "")).strip()
    blocked = {"S47C", "G134C", "T200C", "K242*", "Q99A", "A96P", "K100N", "T95R", "I103L", "K104E"}
    if not sequence or "*" in sequence or "*" in mutations:
        return False
    if any(item in {part.strip() for part in mutations.replace(";", ",").split(",")} for item in blocked):
        return False
    count = _mutation_count(row)
    return 3 <= count <= 9


def _rank_score(row: dict[str, str], stage: str) -> float:
    score = _esm_rank_score(row)
    count = _mutation_count(row)

    if stage in {"developability", "structure"}:
        if 3 <= count <= 5:
            score += 0.5
        elif 6 <= count <= 7:
            score += 0.3

        delta_tm = _float_value(row, "delta_pptstab_tm_c", 0.0)
        if delta_tm >= 1.0:
            score += min(4.0, 1.4 * delta_tm)
        elif delta_tm <= -1.5:
            score -= min(5.0, 1.8 * abs(delta_tm))
        elif delta_tm < -0.5:
            score += 1.0 * delta_tm

        sol = _float_value(row, "netsolp_solubility", 0.0)
        delta_sol = _float_value(row, "delta_netsolp_solubility", 0.0)
        usability = _float_value(row, "netsolp_usability", 0.0)
        delta_usability = _float_value(row, "delta_netsolp_usability", 0.0)
        if sol and sol < 0.4:
            score -= 2.0
        if usability and usability < 0.4:
            score -= 1.5
        if delta_sol < -0.2:
            score -= 3.0
        elif delta_sol < -0.1:
            score -= 1.5
        elif delta_sol > 0.05:
            score += min(0.8, 2.0 * delta_sol)
        if delta_usability < -0.2:
            score -= 2.0
        elif delta_usability < -0.1:
            score -= 1.0
        elif delta_usability > 0.05:
            score += min(0.6, 1.5 * delta_usability)

        delta_deeptp = _float_value(row, "delta_deeptp_score", 0.0)
        score += max(-0.8, min(0.8, delta_deeptp))
        score += 0.2 * _float_value(row, "deeptp_score", 0.0)

        ph = _float_value(row, "ephod_optimal_ph", 0.0)
        delta_ph_distance = _float_value(row, "delta_ephod_target_distance", 0.0)
        if delta_ph_distance < -0.5:
            score += min(1.0, abs(delta_ph_distance))
        elif delta_ph_distance > 0.5:
            score -= min(1.5, delta_ph_distance)
        if ph:
            score -= min(1.0, max(0.0, abs(ph - 7.5) - 1.5) / 2.0)

    if stage == "structure":
        structure_status = str(row.get("structure_status", "")).lower()
        risk = str(row.get("structure_risk_flags", "")).lower()
        if structure_status == "ok":
            score += 1.0
        elif structure_status == "partial":
            score += 0.3
        elif structure_status == "error":
            score -= 1.0
        if "fold_failed" in risk:
            score -= 1.0
        if "evoef2_failed" in risk:
            score -= 0.5
        delta_evoef2 = _float_value(row, "delta_evoef2_energy", 0.0)
        if str(row.get("delta_evoef2_energy", "")).strip():
            score += max(-1.0, min(1.0, -delta_evoef2 / 500.0))
        else:
            score -= 0.5

        plddt = _float_value(row, "fold_plddt_mean", 0.0)
        delta_plddt = _float_value(row, "delta_plddt_mean", 0.0)
        pct_lt50 = _float_value(row, "fold_plddt_pct_lt50", 0.0)
        delta_pct_lt50 = _float_value(row, "delta_plddt_pct_lt50", 0.0)
        pct_ge70 = _float_value(row, "fold_plddt_pct_ge70", 0.0)
        if plddt:
            if plddt < 50.0:
                score -= 5.0
            elif plddt < 70.0:
                score -= 2.0
            else:
                score += min(2.0, (plddt - 70.0) / 10.0)
        if delta_plddt < -5.0:
            score -= min(3.0, abs(delta_plddt) / 5.0)
        elif delta_plddt > 5.0:
            score += min(2.0, delta_plddt / 5.0)
        if pct_lt50 > 50.0:
            score -= 2.0
        if delta_pct_lt50 > 10.0:
            score -= min(2.0, delta_pct_lt50 / 10.0)
        if pct_ge70 > 25.0:
            score += min(1.0, pct_ge70 / 50.0)

    risk_text = f"{row.get('risk_flags', '')} {row.get('design_strategy', '')}".lower()
    if "medium" in risk_text:
        score -= 0.8
    if "charge_reversal" in risk_text:
        score -= 0.5
    return score


def _esm_rank_score(row: dict[str, str]) -> float:
    if str(row.get("esm_status", "")).strip() != "available":
        return -1000.0
    contextual = _float_value(row, "esm_contextual_llr", 0.0)
    epistasis = _float_value(row, "esm_epistasis_delta", 0.0)
    score = 2.0 * contextual + 3.0 * epistasis
    if contextual < 0.0:
        score += contextual
    if epistasis < -0.1:
        score += 2.0 * epistasis
    if epistasis > 0.0:
        score += min(0.5, epistasis)
    return score


def _filter_summary_md(stage: str, selected: list[dict[str, str]], rejected: list[dict[str, str]]) -> str:
    lines = [
        f"# R7 {stage.capitalize()} Shortlist",
        "",
        "This file is generated from CSV values only. Scores are model predictions and screening proxies, not experimental measurements.",
        "",
        "## Selected",
        "",
    ]
    for rank, row in enumerate(selected, start=1):
        lines.append(_summary_line(rank, row, stage))
    lines.extend(["", "## Near Misses", ""])
    for rank, row in enumerate(rejected, start=1):
        lines.append(_summary_line(rank, row, stage))
    return "\n".join(lines)


def _summary_line(rank: int, row: dict[str, str], stage: str) -> str:
    candidate_id = str(row.get("candidate_id", "")).strip()
    mutations = str(row.get("mutations_vs_backbone", "")).strip()
    return (
        f"- {rank}. {candidate_id}: {mutations}; n={_mutation_count(row)}; "
        f"rank_score={_rank_score(row, stage):.3f}; "
        f"ESM={row.get('esm_contextual_llr', '')}/{row.get('esm_epistasis_delta', '') or 'NA'}; "
        f"Tm={row.get('pptstab_tm_c', '') or 'NA'}; dTm={row.get('delta_pptstab_tm_c', '') or 'NA'}; "
        f"sol={row.get('netsolp_solubility', '') or 'NA'}; dSol={row.get('delta_netsolp_solubility', '') or 'NA'}; "
        f"structure={row.get('structure_status', '') or 'NA'}"
    )


def _auto_explain(row: dict[str, str]) -> tuple[str, str, str]:
    mutations = str(row.get("mutations_vs_backbone", "")).strip()
    strategy = str(row.get("design_strategy", "")).strip()
    contextual = _float_value(row, "esm_contextual_llr")
    delta_raw = str(row.get("esm_epistasis_delta", "")).strip()
    risk_flags = str(row.get("risk_flags", "")).strip()
    str(row.get("pptstab_tm_c", "")).strip()
    delta_tm = str(row.get("delta_pptstab_tm_c", "")).strip()
    str(row.get("netsolp_solubility", "")).strip()
    delta_sol = str(row.get("delta_netsolp_solubility", "")).strip()
    str(row.get("netsolp_usability", "")).strip()
    delta_usability = str(row.get("delta_netsolp_usability", "")).strip()
    str(row.get("deeptp_score", "")).strip() or str(row.get("deeptp_label", "")).strip()
    delta_deeptp = str(row.get("delta_deeptp_score", "")).strip()
    str(row.get("ephod_optimal_ph", "")).strip()
    delta_ph_distance = str(row.get("delta_ephod_target_distance", "")).strip()
    str(row.get("structure_status", "")).strip()
    str(row.get("fold_status", "")).strip()
    str(row.get("fold_structure_url", "")).strip()
    fold_pdb_path = str(row.get("fold_pdb_path", "")).strip()
    plddt = str(row.get("fold_plddt_mean", "")).strip()
    baseline_plddt = str(row.get("baseline_fold_plddt_mean", "")).strip()
    str(row.get("delta_plddt_mean", "")).strip()
    str(row.get("fold_plddt_pct_lt50", "")).strip()
    str(row.get("fold_plddt_pct_ge70", "")).strip()
    evoef2 = str(row.get("evoef2_status", "")).strip()
    str(row.get("evoef2_energy", "")).strip()
    baseline_evoef2 = str(row.get("baseline_evoef2_status", "")).strip()
    str(row.get("baseline_evoef2_energy", "")).strip()
    delta_evoef2 = str(row.get("delta_evoef2_energy", "")).strip()
    structure_risk_flags = str(row.get("structure_risk_flags", "")).strip()

    if contextual >= 2.5:
        esm_phrase = f"strong ESM context support (contextual LLR={contextual:.4g})"
    elif contextual >= 1.0:
        esm_phrase = f"acceptable ESM context support (contextual LLR={contextual:.4g})"
    elif contextual < -0.5:
        esm_phrase = f"ESM context penalty (contextual LLR={contextual:.4g})"
    else:
        esm_phrase = f"near-neutral ESM context (contextual LLR={contextual:.4g})"
    if delta_raw:
        delta = _float_value(row, "esm_epistasis_delta")
        if delta >= 0.1:
            epi_phrase = f"positive epistasis support (delta={delta:.4g})"
        elif delta > 0:
            epi_phrase = f"mild positive epistasis support (delta={delta:.4g})"
        elif delta <= -0.1:
            epi_phrase = f"negative epistasis caution (delta={delta:.4g})"
        else:
            epi_phrase = f"near-neutral epistasis (delta={delta:.4g})"
    else:
        epi_phrase = "single-mutation candidate"

    structure_quality = _structure_quality_phrase(row)
    developability_parts = []
    if delta_tm:
        developability_parts.append(f"predicted Tm improves by {delta_tm} C")
    if delta_sol:
        developability_parts.append(f"NetSolP solubility delta {delta_sol}")
    if delta_usability:
        developability_parts.append(f"usability delta {delta_usability}")
    if delta_deeptp:
        developability_parts.append(f"DeepTP delta {delta_deeptp}")
    if delta_ph_distance:
        developability_parts.append(f"pH target-distance delta {delta_ph_distance}")
    developability = "; ".join(developability_parts) or "developability fields are limited"

    structure_parts = []
    if fold_pdb_path:
        structure_parts.append(f"local PDB saved at {fold_pdb_path}")
    if plddt:
        structure_parts.append(f"pLDDT mean {plddt} vs baseline {baseline_plddt or 'NA'}")
    if delta_evoef2:
        structure_parts.append(f"EvoEF2 delta {delta_evoef2} (lower energy is better)")
    elif evoef2 or baseline_evoef2:
        structure_parts.append("EvoEF2 evidence incomplete")
    structure_parts.append(structure_quality)
    structure_summary = "; ".join(structure_parts)

    advantage = (
        f"{mutations} represents the {strategy} branch; {esm_phrase}; {epi_phrase}; "
        f"{developability}; {structure_summary}."
    )
    risk_parts = [
        f"risk_flags={risk_flags or 'none'}",
        f"structure_risk_flags={structure_risk_flags or 'none'}",
        "low pLDDT means fold evidence is weak" if _float_value(row, "fold_plddt_mean", 999.0) < 50.0 else "",
        "EvoEF2 delta is unavailable" if not delta_evoef2 else "",
        "ESM and developability scores are model predictions, not real Tm or ΔΔG",
    ]
    risk = "; ".join(part for part in risk_parts if part) + "."
    nonredundant = f"Distinct mutation set and strategy label: {strategy}."
    return advantage, risk, nonredundant


def _structure_quality_phrase(row: dict[str, str]) -> str:
    plddt = _float_value(row, "fold_plddt_mean", 0.0)
    pct_lt50 = _float_value(row, "fold_plddt_pct_lt50", 0.0)
    if not plddt:
        return "structure_confidence=unavailable"
    if plddt < 50.0 or pct_lt50 > 50.0:
        return "structure_confidence=weak_low_pLDDT"
    if plddt < 70.0:
        return "structure_confidence=moderate_pLDDT"
    return "structure_confidence=higher_pLDDT"


def _rationale_md(selected_rows: list[dict[str, str]]) -> str:
    lines = [
        "# R7 Top 5 Rationale",
        "",
        "## Selection Rules",
        "",
        "- Input truth source: final structure-screened matrix CSV.",
        "- All selected variants use the physical `r6_6h09` / R6-6H09 backbone.",
        "- ESM fields are sequence-model evidence only; they are not real Tm, ΔΔG, expression, or activity measurements.",
        "- `esm_contextual_llr` is better when higher; `esm_epistasis_delta` supports multi-mutant compatibility when positive and warns against conflicts when negative.",
        "- Developability fields are model/API predictions compared with the inferred R6-6H09 baseline: positive `delta_pptstab_tm_c` supports predicted thermal improvement; negative NetSolP deltas are guardrail risks; negative `delta_ephod_target_distance` means pH moved closer to the target.",
        "- Final candidates must have a saved PDB path and `fold_plddt_mean`; pLDDT is read from the predicted PDB B-factor column and compared with the predicted R6-6H09 baseline. Low pLDDT (<50) is weak fold evidence, not high structure confidence.",
        "- EvoEF2 energy is model-derived structure evidence and is only comparable within the same modeling setup; `delta_evoef2_energy` compares candidate minus R6-6H09 baseline. Missing EvoEF2 delta is treated as an evidence gap, not as a usable score.",
        "- Final ranking prioritizes ESM context, multi-mutant epistasis, predicted Tm, NetSolP guardrails, and risk balance. Structure metrics are used as sanity checks because the current pLDDT values are low.",
        "- At least one lower-risk/non-I174S backup is preferred when available; high-load variants are kept as exploration candidates rather than allowed to dominate the whole Top 5.",
        "",
        "## Selected Variants",
        "",
    ]
    for row in selected_rows:
        advantage, risk, nonredundant = _auto_explain(row)
        lines.extend([
            f"### {row.get('candidate_id', '')}: {row.get('mutations_vs_backbone', '')}",
            "",
            f"- Strategy: {row.get('design_strategy', '')}",
            f"- ESM contextual LLR: {row.get('esm_contextual_llr', '')}",
            f"- ESM epistasis delta: {row.get('esm_epistasis_delta', '') or 'NA'}",
            f"- PPTStab predicted Tm C: {row.get('pptstab_tm_c', '') or 'NA'}",
            f"- PPTStab delta vs backbone C: {row.get('delta_pptstab_tm_c', '') or 'NA'}",
            f"- NetSolP solubility/usability: {row.get('netsolp_solubility', '') or 'NA'} / {row.get('netsolp_usability', '') or 'NA'}",
            f"- NetSolP delta solubility/usability: {row.get('delta_netsolp_solubility', '') or 'NA'} / {row.get('delta_netsolp_usability', '') or 'NA'}",
            f"- DeepTP score/label: {row.get('deeptp_score', '') or 'NA'} / {row.get('deeptp_label', '') or 'NA'}",
            f"- DeepTP delta score: {row.get('delta_deeptp_score', '') or 'NA'}",
            f"- EpHod optimal pH: {row.get('ephod_optimal_ph', '') or 'NA'}",
            f"- EpHod target-distance delta: {row.get('delta_ephod_target_distance', '') or 'NA'}",
            f"- Structure status: {row.get('structure_status', '') or 'NA'}",
            f"- Fold structure URL (optional): {row.get('fold_structure_url', '') or 'NA'}",
            f"- Fold PDB path: {row.get('fold_pdb_path', '') or 'NA'}",
            f"- Fold pLDDT mean: {row.get('fold_plddt_mean', '') or 'NA'}",
            f"- Baseline fold pLDDT mean: {row.get('baseline_fold_plddt_mean', '') or 'NA'}",
            f"- Delta pLDDT mean vs baseline: {row.get('delta_plddt_mean', '') or 'NA'}",
            f"- pLDDT <50 / >=70 percent: {row.get('fold_plddt_pct_lt50', '') or 'NA'} / {row.get('fold_plddt_pct_ge70', '') or 'NA'}",
            f"- Fold / EvoEF2: {row.get('fold_status', '') or 'NA'} / {row.get('evoef2_status', '') or 'NA'}",
            f"- EvoEF2 energy / baseline / delta: {row.get('evoef2_energy', '') or 'NA'} / {row.get('baseline_evoef2_energy', '') or 'NA'} / {row.get('delta_evoef2_energy', '') or 'NA'}",
            f"- Main advantage: {advantage}",
            f"- Main risk: {risk}",
            f"- Non-redundancy: {nonredundant}",
            "",
        ])
    return "\n".join(lines)


def _default_rejected(rows: list[dict[str, str]], selected: set[str]) -> list[str]:
    candidates = [row for row in rows if str(row.get("candidate_id", "")).strip() not in selected]
    candidates.sort(
        key=lambda row: (
            _float_value(row, "esm_contextual_llr"),
            _float_value(row, "esm_epistasis_delta", -999.0),
        ),
        reverse=True,
    )
    return [str(row.get("candidate_id", "")).strip() for row in candidates[:8]]


def _rejected_md(rows: list[dict[str, str]]) -> str:
    lines = [
        "# Rejected Near-Misses",
        "",
        "These variants were close alternatives but were not selected because the final set must balance score, risk, ESM compatibility, and strategy diversity.",
        "",
    ]
    for row in rows:
        candidate_id = str(row.get("candidate_id", "")).strip()
        mutations = str(row.get("mutations_vs_backbone", "")).strip()
        strategy = str(row.get("design_strategy", "")).strip()
        contextual = str(row.get("esm_contextual_llr", "")).strip()
        delta = str(row.get("esm_epistasis_delta", "")).strip() or "NA"
        risk = str(row.get("risk_flags", "")).strip()
        reason = _rejection_reason(row)
        lines.extend([
            f"## {candidate_id}: {mutations}",
            "",
            f"- Strategy: {strategy}",
            f"- ESM contextual LLR: {contextual}",
            f"- ESM epistasis delta: {delta}",
            f"- PPTStab predicted Tm C: {row.get('pptstab_tm_c', '') or 'NA'}",
            f"- PPTStab delta vs backbone C: {row.get('delta_pptstab_tm_c', '') or 'NA'}",
            f"- NetSolP solubility/usability: {row.get('netsolp_solubility', '') or 'NA'} / {row.get('netsolp_usability', '') or 'NA'}",
            f"- NetSolP delta solubility/usability: {row.get('delta_netsolp_solubility', '') or 'NA'} / {row.get('delta_netsolp_usability', '') or 'NA'}",
            f"- Structure status: {row.get('structure_status', '') or 'NA'}",
            f"- Risk flags: {risk}",
            f"- Reason not selected: {reason}",
            "",
        ])
    return "\n".join(lines)


def _rejection_reason(row: dict[str, str]) -> str:
    risk = str(row.get("risk_flags", "")).lower()
    delta = _float_value(row, "esm_epistasis_delta", 0.0)
    contextual = _float_value(row, "esm_contextual_llr", 0.0)
    strategy = str(row.get("design_strategy", "")).strip()
    delta_sol = _float_value(row, "delta_netsolp_solubility", 0.0)
    delta_evoef2 = _float_value(row, "delta_evoef2_energy", 0.0)
    if str(row.get("esm_epistasis_delta", "")).strip() and delta < -0.1:
        return "Negative ESM epistasis signal suggests weaker multi-mutant compatibility."
    if delta_sol < 0.0:
        return "Developability guardrail is weaker because predicted NetSolP solubility drops versus the R6-6H09 baseline."
    if str(row.get("delta_evoef2_energy", "")).strip() and delta_evoef2 > 0.0:
        return "EvoEF2 delta is unfavorable; lower energy is better within the same structure workflow."
    if "medium" in risk:
        return "Higher risk flag or strategy overlap; useful as a controlled test but not preferred over the selected balanced set."
    if contextual < -0.5:
        return "ESM contextual LLR penalizes this sequence context."
    return f"Near-miss mainly due to strategy overlap or lower priority relative to selected {strategy} alternatives."
