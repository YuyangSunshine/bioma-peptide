from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from bioma.backends.analysis.services.llm_json_service import (
    HumanMessage,
    SystemMessage,
    make_json_llm,
    tool,
)
from bioma.backends.analysis.chart_skills import all_chart_skills


def _format_chart_skills_for_prompt() -> str:
    lines: List[str] = []
    for skill in all_chart_skills():
        lines.append(
            "\n".join(
                [
                    f"- chart_key: {skill.key}",
                    f"  name: {skill.display_name}",
                    f"  instrument_family: {skill.instrument_family}",
                    f"  required_roles: {', '.join(skill.required_roles)}",
                    f"  optional_roles: {', '.join(skill.optional_roles) or 'none'}",
                    f"  trigger_terms: {', '.join(skill.trigger_terms)}",
                    f"  planning_hint: {skill.planning_hint}",
                ]
            )
        )
    return "\n\n".join(lines)


_CHART_SKILLS_PROMPT = _format_chart_skills_for_prompt()


PLAN_ANALYSIS_SYSTEM_PROMPT = """
You are a scientific experiment analysis planner.

You receive a structural dataset_bundle (tables with columns and axis_role_hints)
and a stable_requirement (what the user asked for). You must produce an
analysis-first open-vocabulary plan: first define WHAT scientific/data question
to answer and WHAT to compute, then define WHAT to plot as evidence for that
analysis. Charts are supporting evidence, not the analysis objective itself.
The plan must be executable by downstream LLM code-generators.

You MUST output STRICT JSON only — no markdown, no prose outside the JSON.

==== How to read dataset_bundle ====
- JSON tables carry source_ref.json_source with an immutable source path, SHA256,
  JSON Pointer and projection. A properties view represents one source object;
  determine from context whether it is metadata or an observation. Record arrays retain one row per source object; nested objects/arrays
  remain JSON strings in their parent cells. Never assume separate arrays are
  aligned or join them by row position. Use explicit identifiers and the request.
  The original JSON is authoritative for types, nulls and missing keys.
- tables[i].columns[j] has {name, dtype_hint, numeric_ratio, n_unique,
  sample_values, axis_role_hints}.
- axis_role_hints are *hints*, not a closed vocabulary. Common labels:
    temperature_axis / cycle_axis / time_axis       — likely X for curves
    well_id / sample_id / group_id / target_id      — likely grouping keys
    generic_signal / fluorescence_signal / ct_signal / absorbance_signal
                                                    — likely Y for plots/fits
    concentration_axis / size_bp_axis               — likely X for curves/gels
- layout_type: "long" | "wide" | "matrix" | "unknown".
- You may use any column by its real `name`. Do NOT invent columns.

==== No-laziness rule — critical ====
If the user's requirement mentions a derived metric (Tm, melting temperature,
retention rate, 保留率, 保留百分比, pass/fail count, classification, slope,
half-life, IC50, Ct, peak height, AUC, ratio, normalized value, …) and the
raw data contains the *ingredients* to compute it, you MUST plan analysis
steps to compute it. Do NOT refuse or defer by saying "raw data does not
contain this metric" — derived metrics almost never appear as literal
columns; they are the POINT of the analysis.

Ingredient → derivation recipes (common cases — adapt as needed):
- Tm from melting data: compute -dF/dT per well from (temperature, signal),
  take argmax temperature; OR if a negative-derivative table is supplied,
  take argmax directly.
- Retention % at T_end: F(T_end) / F(T_start) * 100 per well, from the raw
  fluorescence table (use earliest and latest temperature rows per well).
- Classification against thresholds: merge computed metrics per well and
  apply the threshold from the requirement.
- Rate / slope: linear fit or local derivative over the appropriate window.

If ingredients are genuinely missing (e.g. a required column is absent),
still emit the analysis step with the best-effort method_hint and record
an `issues[]` entry (severity: medium) describing the gap — do NOT silently
omit the step.

==== Typical experiment patterns (guides, not rules) ====
- melt_curve_analysis (fluorescence vs temperature, per well):
    analyses: (1) compute -dF/dT per well, (2) take argmax temperature as Tm.
    charts: line plot of value~temperature grouped by well;
            optionally overlay Tm vertical lines per well.
- standard_curve_quantitation (endpoint signal vs concentration):
    analyses: linear/log fit on standards; back-calc sample concentrations.
    charts: scatter + fitted line of signal~concentration.
- amplification_curve_qc (fluorescence vs cycle, per well):
    analyses: shape / monotonicity check.
    charts: line of fluor~cycle grouped by well.
- electropherogram (intensity vs size_bp or migration_time):
    analyses: peak detection, range summary.
    charts: line of intensity vs size_bp.

==== Available chart skills ====
Use these predefined chart skills whenever the user's request and table columns match.
When a chart skill applies, set `charts[i].chart_key` to the exact key below.
Keep `chart_spec.kind` as the low-level rendering geometry (for example "bar" or "line").

{chart_skills}

When correction feedback is provided, fix each validation error using the real
schema and return a complete replacement plan. Preserve the requested analyses
and charts; do not remove user requirements just to pass validation. Declare
computed columns in analysis outputs before referring to them in a chart.

==== Output requirements ====
- experiment_kind: one short snake_case string. Use "unknown_or_generic" if unclear.
- analyses: ordered list, 1–5 items. Each item describes an analysis step in
  natural language; a future code-gen will compile it to Python.
- charts: ordered list, 1–3 items. Keep it minimal: DO NOT propose extra charts
  the user did not ask for.
- Every chart must support at least one analysis goal. In chart rationale, state
  which analysis result or scientific question the chart helps verify.
- If the user only asks for a chart/curve by name, infer the underlying analysis
  objective from the chart type and data columns, then include at least one
  `analyses[]` item for the metric/model/QC judgement behind the chart.
- When a chart should display a derived metric (relative_expression,
  fold_change, ΔΔCt, Tm, Ct/Cp, mP, fitted value, predicted MW, Km, Vmax, etc.),
  `chart_spec.y` MUST name that derived metric or analysis output, not the raw
  ingredient column used to compute it. For example, qPCR relative-expression
  bars must plot relative_expression/fold_change from the ΔΔCt analysis output;
  raw Ct/Cq is not an acceptable bar height.
- For qPCR relative-expression bars, the derived analysis output must include
  the plotted target identity in a `target_gene` or `gene` column. Reference
  genes are normalization inputs and must not be used as plotted target labels.
  Do not depend on a generic or inferred target label.
- If the user explicitly requested a chart (chart_preferences, or analysis_goals
  mentioning "plot / curve / 绘制"), include a matching chart.
- If the user asks to plot, draw, visualize, or create a named chart/curve,
  `charts` MUST contain at least one item. Do not return analyses-only plans
  for chart requests, but also do not return chart-only plans when analyzable
  data are present.
- Do not use generic chart keys when a predefined chart skill matches. Use the
  exact predefined `chart_key`.
- Every column reference MUST be an exact name from dataset_bundle.
- Every `source_table_id` MUST be one of the table_ids in dataset_bundle.
- Multi-table support: when dataset_bundle has MORE THAN ONE table, inspect
  every table (not just t1) before planning. Set `inputs.table_id` on each
  analysis to the table that step should read from. If a step naturally
  combines two tables (e.g. joining a long-form signal table with a peak
  annotation table on "well"), describe the join explicitly in `method_hint`
  — the code-gen layer has access to every table via an `available_tables`
  map keyed by table_id, so the generated code can `pd.read_csv` any of them.
  Do NOT silently discard a table the user provided — if it looks relevant
  to the requested outputs, include at least one step that reads it.

==== Output JSON schema ====
{
  "experiment_kind": "string",
  "analyses": [
    {
      "analysis_id": "a1",
      "goal": "one-sentence description of what to compute",
      "method_hint": "concrete algorithmic recipe in 1-3 sentences",
      "inputs": {
        "table_id": "t1",
        "group_by": ["col"],
        "x": "col_name",
        "y": "col_name",
        "from_analysis": "a1"
      },
      "outputs": ["named_artifact"]
    }
  ],
  "charts": [
    {
      "chart_id": "chart_01",
      "chart_key": "predefined_chart_key_or_generic_kind",
      "title": "string",
      "source_table_id": "t1",
      "chart_spec": {
        "kind": "line|scatter|bar|histogram|box|heatmap",
        "x": "col_name",
        "y": "col_name",
        "series_by": "col_name",
        "error": "col_name",
        "significance": "col_name",
        "annotate": {"vlines_from_analysis": "a1"}
      },
      "rationale": "short justification grounded in the user's requirement"
    }
  ],
  "notes": "string",
  "issues": [
    {"code": "string", "severity": "low|medium|high", "message": "string"}
  ]
}
""".replace("{chart_skills}", _CHART_SKILLS_PROMPT).strip()


def _make_llm() -> Any:
    return make_json_llm(
        model_env_name="PLANNING_LLM_MODEL",
        api_env_name="BAIDU_API_KEY",
        base_env_name="BAIDU_BASE_URL",
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _fix_to_valid_json(llm: Any, raw_text: str) -> Optional[Dict[str, Any]]:
    messages = [
        SystemMessage(
            content=(
                "You are a JSON repair tool. Convert the input into STRICT valid JSON only. "
                "Do not add markdown or explanations."
            )
        ),
        HumanMessage(content=raw_text or ""),
    ]
    try:
        resp = llm.invoke(messages)
        return _extract_json(getattr(resp, "content", "") or "")
    except Exception:
        return None


def _slim_bundle_for_llm(dataset_bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Compact the bundle to the fields a planner actually needs."""
    tables_slim: List[Dict[str, Any]] = []
    for t in dataset_bundle.get("tables") or []:
        cols_slim = []
        for c in t.get("columns") or []:
            cols_slim.append(
                {
                    "name": c.get("name"),
                    "dtype_hint": c.get("dtype_hint"),
                    "n_unique": c.get("n_unique"),
                    "numeric_ratio": c.get("numeric_ratio"),
                    "min_numeric": c.get("min_numeric"),
                    "max_numeric": c.get("max_numeric"),
                    "sample_values": (c.get("sample_values") or [])[:4],
                    "axis_role_hints": c.get("axis_role_hints") or [],
                }
            )
        tables_slim.append(
            {
                "table_id": t.get("table_id"),
                "source_ref": t.get("source_ref") or {},
                "layout_type": t.get("layout_type"),
                "shape": t.get("shape") or {},
                "columns": cols_slim,
            }
        )

    return {
        "bundle_version": dataset_bundle.get("bundle_version"),
        "instrument_family_hint": dataset_bundle.get("instrument_family_hint"),
        "tables": tables_slim,
        "relations": dataset_bundle.get("relations") or [],
        "global_entities": dataset_bundle.get("global_entities") or {},
        "bundle_summary": dataset_bundle.get("bundle_summary") or "",
    }


@tool
def plan_analysis_tool(
    dataset_bundle: Dict[str, Any],
    requirement: Dict[str, Any],
    experiment_background: Optional[Dict[str, Any]] = None,
    feedback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    LLM-driven open-vocabulary analysis planner.

    Produces {experiment_kind, analyses[], charts[], notes, issues}.
    Does NOT execute any analysis or plotting. Downstream run_analysis /
    run_plotting consume this spec.
    """
    experiment_background = experiment_background or {}
    llm = _make_llm()

    user_payload = {
        "dataset_bundle": _slim_bundle_for_llm(dataset_bundle or {}),
        "stable_requirement": requirement or {},
        "experiment_background": experiment_background,
        "instrument_family_hint": (dataset_bundle or {}).get("instrument_family_hint")
        or "unknown",
    }

    if feedback:
        user_payload["correction"] = feedback

    messages = [
        SystemMessage(content=PLAN_ANALYSIS_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(user_payload, ensure_ascii=False)),
    ]

    try:
        resp = llm.invoke(messages)
    except Exception as e:
        return {
            "experiment_kind": "unknown_or_generic",
            "analyses": [],
            "charts": [],
            "notes": "",
            "issues": [
                {
                    "code": "PLAN_ANALYSIS_LLM_EXCEPTION",
                    "severity": "high",
                    "message": repr(e),
                }
            ],
        }

    content = getattr(resp, "content", "") if resp is not None else ""
    data = _extract_json(content)
    if data is None:
        data = _fix_to_valid_json(llm, content) or {}

    if not isinstance(data, dict):
        return {
            "experiment_kind": "unknown_or_generic",
            "analyses": [],
            "charts": [],
            "notes": "",
            "issues": [
                {
                    "code": "PLAN_ANALYSIS_LLM_UNPARSEABLE",
                    "severity": "high",
                    "message": "LLM response could not be parsed into JSON.",
                }
            ],
        }

    return {
        "experiment_kind": str(data.get("experiment_kind") or "unknown_or_generic"),
        "analyses": list(data.get("analyses") or []),
        "charts": list(data.get("charts") or []),
        "notes": str(data.get("notes") or ""),
        "issues": list(data.get("issues") or []),
    }


__all__ = ["plan_analysis_tool"]
