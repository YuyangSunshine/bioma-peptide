from __future__ import annotations

import json
from typing import Any, Dict, List

from bioma.backends.analysis.services.llm_json_service import (
    HumanMessage,
    SystemMessage,
    make_text_llm,
    tool,
)


REPORT_SYSTEM_PROMPT = """
You are the formal report-writing module for a scientific data-analysis agent.

Your job:
- Write a clear, professional markdown report body.
- Use ONLY the provided requirement / experiment background / data profile / analysis results / chart plan / visualizations.
- Be conservative about unsupported claims, but NEVER deny work that was actually done.
- If uncertainty exists, explicitly say so.
- The tone should follow report_preferences when available.

==== Ground truth rule — critical ====
`analysis_results.findings` is the authoritative record of what the analysis
layer actually computed. Each finding has an `analysis_id`, `goal`,
`method_hint`, `summary` (with real numbers) and `output_files`.

- `analysis_results.result_registry` may mark results with `trust_level`
  and `scope`. When one or more entries have `trust_level=="canonical"`,
  canonical entries are the trusted factual source for their scopes. Do not mix
  conflicting exploratory/supplemental findings into the main numbers for a
  canonical scope.
- Non-canonical / exploratory result ids are diagnostic trace data only. Do not
  mention their ids, counts, or summaries in the scientific report when
  canonical findings exist.
- If a finding's summary contains the numeric result the user asked about
  (e.g. Tm values, retention percentages, pass/fail counts), you MUST present
  those numbers in "Key Findings". Do NOT write phrases like
  "无法计算Tm值" / "cannot derive retention" / "data does not contain X"
  when the finding's summary clearly contains those computed values.
- Derived / secondary-calculation results count: Tm computed from negative
  derivative peaks, retention computed from F_end/F_start, classifications
  computed from thresholds — all are valid results if they appear in findings.
- If findings is truly empty, then and only then say the analysis could not
  be completed — and explain the gap from `qc_flags` / `issues`.
- If `analysis_results.stat_tests` is empty, do NOT mention p-values,
  Welch's t-test, ANOVA, significance stars, or statistical significance.
  You may describe numeric differences (mean, SD, delta) only.

==== Prediction-review rule ====
If `analysis_results.findings` contains a canonical finding with
`scope=="prediction_vs_observed"`:
- Add a concise prediction-vs-observed review section.
- Present the entities, predicted values, observed values, residuals, anomaly
  levels, and directions exactly as provided in the finding summary.
- Treat hypothesis seeds as possible explanations only. Use cautious wording
  such as "可能", "提示", "需要验证". Do NOT state hypotheses as confirmed causal
  mechanisms.
- Include next actions from the finding summary when present.
- Do not bring in historical comparisons unless they are explicitly present in
  provided analysis results.

==== Figure rule — critical ====
- `visualizations` is a list of rendered figures. Every item with
  status=="success" and image_count > 0 IS a real figure that has already been
  rendered to disk.
- Do NOT claim "visualizations 列表为空" / "no figures were generated" when
  the list has success items.
- Do NOT write Markdown image links yourself. The report assembly layer embeds
  every successful figure deterministically inside the Figure-by-Figure
  Interpretation section. In that section, refer to each figure by chart_id or
  title and write the interpretation text only.
- If a visualization has multiple page images split by `plot_page`, describe
  them as multiple figure pages with up to N traces/lines per page. Do NOT call
  them "subplots" unless the image itself visibly contains subplots.
- Elsewhere in the report you may also refer to a figure by its chart_id
  (e.g. "见 chart_01"). Do NOT invent chart ids or image paths.
- If canonical analysis findings exist, use them as the only source for numeric
  QC, Tm, group-statistics, and residual facts. Visualization metadata is for
  image embedding and visual orientation, not for replacing canonical numbers.
- Do NOT claim requested figures are missing if `visualizations` contains
  success items for those chart types.
- If a visualization's `plot_analysis` contains `visual_observation`, use it
  only to describe what the image appears to show. For each figure, write the
  interpretation in this order:
    1. Visual observation: what the rendered figure appears to show. If status is
       `vision_unavailable`, explicitly say direct visual interpretation was not
       available instead of pretending to inspect the image.
    2. Link the observation back to canonical analysis findings where relevant.
  Do not add a separate judgement subsection. Synthesize the final conclusion
  from the user requirement, analysis findings, visual observation, and data
  evidence in normal report sections.

Output requirements:
- Return markdown text only.
- Do NOT wrap in JSON.
- Do NOT include code fences unless necessary.
- Use headings and concise paragraphs.

Recommended structure:
1. Analysis Objective
2. Data Overview
3. Key Findings          <- must reflect findings[].summary numbers
4. Figure-by-Figure Interpretation   <- must embed every successful viz
5. Limitations / Notes   <- only real gaps (from qc_flags / missing findings)
6. Conclusion
""".strip()


def _make_llm() -> Any:
    return make_text_llm(
        model_env_name="REPORT_LLM_MODEL",
        api_env_name="BAIDU_API_KEY",
        base_env_name="BAIDU_BASE_URL",
    )


def _safe_json(obj: Any, max_chars: int = 60000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        s = str(obj)
    return s[:max_chars]


def _slim_data_profile(dp: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(dp, dict):
        return {}
    tables_slim: List[Dict[str, Any]] = []
    for t in dp.get("tables") or []:
        cols_slim = []
        for c in t.get("columns") or []:
            cols_slim.append(
                {
                    "name": c.get("name"),
                    "dtype_hint": c.get("dtype_hint"),
                    "n_unique": c.get("n_unique"),
                    "axis_role_hints": c.get("axis_role_hints") or [],
                }
            )
        tables_slim.append(
            {
                "table_id": t.get("table_id"),
                "layout_type": t.get("layout_type"),
                "shape": t.get("shape") or {},
                "columns": cols_slim,
            }
        )
    return {
        "bundle_version": dp.get("bundle_version"),
        "instrument_family_hint": dp.get("instrument_family_hint"),
        "tables": tables_slim,
        "bundle_summary": dp.get("bundle_summary") or "",
    }


def _slim_analysis_results(ar: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ar, dict):
        return {}
    # derived_views: drop preview_rows, keep the skeleton
    dv_in = ar.get("derived_views") or {}
    dv_out: Dict[str, Any] = {}
    if isinstance(dv_in, dict):
        for vid, v in dv_in.items():
            if not isinstance(v, dict):
                continue
            dv_out[vid] = {
                "view_id": v.get("view_id"),
                "table_id": v.get("table_id"),
                "analysis_id": v.get("analysis_id"),
                "csv_path": v.get("csv_path"),
                "shape": v.get("shape"),
                "columns": v.get("columns"),
            }
    # step_results: tiny status skeleton
    sr_in = ar.get("step_results") or []
    sr_out: List[Dict[str, Any]] = []
    for s in sr_in:
        if not isinstance(s, dict):
            continue
        sr_out.append(
            {
                "step": s.get("step") or s.get("analysis_id"),
                "status": s.get("status"),
                "view_id": s.get("view_id"),
                "csv_path": s.get("csv_path"),
            }
        )
    findings_in = list(ar.get("findings") or [])
    registry = list(ar.get("result_registry") or [])
    canonical_ids = {
        str(item.get("analysis_id") or "")
        for item in registry
        if isinstance(item, dict)
        and str(item.get("trust_level") or "").lower() == "canonical"
    }
    canonical_scopes = {
        str(item.get("scope") or "")
        for item in registry
        if isinstance(item, dict)
        and str(item.get("trust_level") or "").lower() == "canonical"
    }

    registry_for_report: List[Dict[str, Any]] = []
    has_canonical_registry = any(
        isinstance(item, dict)
        and str(item.get("trust_level") or "").lower() == "canonical"
        for item in registry
    )
    for item in registry:
        if not isinstance(item, dict):
            continue
        trust = str(item.get("trust_level") or "").lower()
        if trust == "canonical":
            registry_for_report.append(item)
        elif not has_canonical_registry:
            registry_for_report.append(
                {
                    "analysis_id": item.get("analysis_id"),
                    "scope": item.get("scope"),
                    "trust_level": item.get("trust_level"),
                    "notes": "Non-canonical result; summary/artifacts omitted from report payload.",
                }
            )

    has_canonical = bool(canonical_ids)
    if has_canonical:
        findings_main: List[Dict[str, Any]] = []
        supplemental_findings: List[Dict[str, Any]] = []
        for finding in findings_in:
            if not isinstance(finding, dict):
                continue
            analysis_id = str(finding.get("analysis_id") or "")
            scope = str(finding.get("scope") or "")
            trust = str(finding.get("trust_level") or "").lower()
            if analysis_id in canonical_ids or trust == "canonical":
                findings_main.append(finding)
            elif (
                scope and scope not in canonical_scopes and trust not in {"exploratory"}
            ):
                findings_main.append(finding)
            else:
                supplemental_findings.append(finding)
    else:
        findings_main = findings_in
        supplemental_findings = []
    findings_main = [finding for finding in findings_main if isinstance(finding, dict)]

    return {
        "analysis_version": ar.get("analysis_version"),
        "primary_view_id": ar.get("primary_view_id"),
        "derived_views": dv_out,
        "step_results": sr_out,
        # the fields report-writing actually needs — keep full fidelity
        "findings": findings_main,
        "supplemental_findings": [] if has_canonical else supplemental_findings[:8],
        "qc_flags": ar.get("qc_flags") or [],
        "metrics": ar.get("metrics") or {},
        "fits": ar.get("fits") or {},
        "stat_tests": ar.get("stat_tests") or {},
        "analysis_outputs": ar.get("analysis_outputs") or {},
        "deterministic_artifacts": ar.get("deterministic_artifacts") or {},
        "result_registry": registry_for_report,
    }


def _slim_visualizations(
    vizs: List[Dict[str, Any]],
    *,
    canonical_facts_available: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for v in vizs or []:
        if not isinstance(v, dict):
            continue
        plot_analysis = v.get("plot_analysis") or {}
        if canonical_facts_available and isinstance(plot_analysis, dict):
            plot_analysis = {
                "status": plot_analysis.get("status"),
                "results": [
                    {
                        "visual_observation": item.get("visual_observation"),
                    }
                    for item in (plot_analysis.get("results") or [])
                    if isinstance(item, dict) and item.get("visual_observation")
                ][:3],
            }
        out.append(
            {
                "chart_id": v.get("chart_id"),
                "chart_key": v.get("chart_key"),
                "title": v.get("title"),
                "status": v.get("status"),
                "image_count": len(v.get("image_paths") or []),
                "rationale": v.get("rationale"),
                "plot_analysis": plot_analysis,
            }
        )
    return out


@tool
def generate_report_body_tool(
    requirement: Dict[str, Any],
    experiment_background: Dict[str, Any],
    data_profile: Dict[str, Any],
    analysis_results: Dict[str, Any],
    chart_plan: List[Dict[str, Any]],
    visualizations: List[Dict[str, Any]],
) -> str:
    """
    Generate the formal markdown report body from already-produced artifacts.
    """
    llm = _make_llm()

    slim_analysis_results = _slim_analysis_results(analysis_results or {})
    canonical_facts_available = any(
        isinstance(item, dict)
        and str(item.get("trust_level") or "").lower() == "canonical"
        for item in (slim_analysis_results.get("result_registry") or [])
    )

    payload = {
        "requirement": requirement or {},
        "experiment_background": experiment_background or {},
        "data_profile": _slim_data_profile(data_profile or {}),
        "analysis_results": slim_analysis_results,
        "chart_plan": chart_plan or [],
        "visualizations": _slim_visualizations(
            visualizations or [],
            canonical_facts_available=canonical_facts_available,
        ),
    }

    messages = [
        SystemMessage(content=REPORT_SYSTEM_PROMPT),
        HumanMessage(content=_safe_json(payload)),
    ]

    resp = llm.invoke(messages)
    return str(getattr(resp, "content", "") or "").strip()


# optional alias to mirror the old naming style
generate_report_body_fixed_tool = generate_report_body_tool
