from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from bioma.backends.analysis.chart_skills import chart_skill_keys, get_chart_skill
from bioma.backends.analysis.services.llm_json_service import (
    HumanMessage,
    SystemMessage,
    make_json_llm,
    make_text_llm,
    tool,
)


_LEGACY_CHART_KEYS = {
    "qpcr_amplification_curve",
    "qpcr_melt_curve_neg_derivative",
    "plate_standard_curve",
    "plate_sample_concentration_bar",
    "plate_michaelis_menten_curve",
    "plate_fret_time_curve",
    "ce_dna_electropherogram",
    "ce_rna_electropherogram",
    "ce_virtual_gel",
}

CHART_KEYS = chart_skill_keys() | _LEGACY_CHART_KEYS
DEFAULT_VISION_MODEL = "qwen3-vl-235b-a22b-instruct"


def _replace_python_placeholder(code: str, placeholder: str, literal: str) -> str:
    code = code.replace(f'"{placeholder}"', literal)
    code = code.replace(f"'{placeholder}'", literal)
    code = code.replace(placeholder, literal)
    return code


def _make_llm() -> Any:
    return make_json_llm(
        model_env_name="PLOTTING_LLM_MODEL",
        api_env_name="BAIDU_API_KEY",
        base_env_name="BAIDU_BASE_URL",
    )


def _make_vision_llm() -> Any:
    return make_text_llm(
        model_env_name="VISION_LLM_MODEL",
        api_env_name="BAIDU_API_KEY",
        base_env_name="BAIDU_BASE_URL",
    )


def _image_path_to_data_url(path: str) -> str:
    image_path = Path(path)
    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime:
        mime = "image/png"
    raw = image_path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


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


def get_chart_hint(chart_key: str) -> str:
    key = str(chart_key or "").strip()
    skill = get_chart_skill(key)
    if skill is not None and skill.plot_hint:
        return skill.plot_hint

    if key == "qpcr_relative_expression_bar":
        return (
            "- Make a grouped bar chart for qPCR expression-style values.\n"
            "- Prefer already computed summary/value columns when available.\n"
            "- X-axis: sample or target; color/hue: group when available.\n"
            "- Validate required columns conservatively."
        )
    if key == "qpcr_amplification_curve":
        return (
            "- Make a multi-line qPCR amplification curve.\n"
            "- X-axis: cycle; Y-axis: fluorescence or intensity.\n"
            "- Sort by cycle before plotting.\n"
            "- Use legend only if series count is small."
        )
    if key == "qpcr_melt_curve_neg_derivative":
        return (
            "- Make a multi-line melt curve plot using -dF/dT vs temperature.\n"
            "- If negative derivative is absent, compute it from fluorescence vs temperature.\n"
            "- Sort by temperature before differentiation."
        )
    if key == "plate_standard_curve":
        return (
            "- Make a standard-curve scatter/line plot.\n"
            "- X-axis: concentration; Y-axis: endpoint signal.\n"
            "- If replicates exist, summarize or overlay conservatively.\n"
            "- Add fitted line only if enough points exist."
        )
    if key == "plate_sample_concentration_bar":
        return (
            "- Make a grouped bar plot for sample concentrations or endpoint values.\n"
            "- X-axis: sample/group; Y-axis: value or concentration.\n"
            "- Use error bars only if std/cv columns exist."
        )
    if key == "plate_michaelis_menten_curve":
        return (
            "- Make a time-series or rate-vs-concentration style plot conservatively.\n"
            "- Prefer time on x-axis when available; otherwise use concentration on x-axis."
        )
    if key == "plate_fret_time_curve":
        return (
            "- Make a FRET time-series line plot.\n"
            "- Prefer ratio_value if available; otherwise compute from p_value / s_value if safe."
        )
    if key in {"ce_dna_electropherogram", "ce_rna_electropherogram"}:
        return (
            "- Make an electropherogram line plot.\n"
            "- X-axis: size_bp or migration time; Y-axis: intensity.\n"
            "- Sort x-axis before plotting."
        )
    if key == "ce_virtual_gel":
        return (
            "- Make a simplified virtual-gel style visualization conservatively.\n"
            "- If exact gel rendering is unsupported, fall back to a size_bp vs intensity/peak representation."
        )
    return (
        "- Make a conservative chart using only unambiguous fields.\n"
        "- If required fields are missing, raise ValueError listing required and candidate columns."
    )


def get_chart_analysis_hint(chart_key: str) -> str:
    key = str(chart_key or "").strip()
    skill = get_chart_skill(key)
    if skill is not None and skill.analysis_hint:
        return skill.analysis_hint

    if key == "qpcr_relative_expression_bar":
        return (
            "- Summarize group/sample-level expression differences conservatively.\n"
            "- Report simple descriptive comparisons, not unsupported causal claims."
        )
    if key == "qpcr_amplification_curve":
        return (
            "- Evaluate amplification-curve shape and basic QC flags.\n"
            "- Flag flat/no amplification, delayed amplification, or abnormal oscillation."
        )
    if key == "qpcr_melt_curve_neg_derivative":
        return (
            "- Estimate melt-curve peak candidates and Tm-like dominant peak.\n"
            "- Flag multiple peaks or broad peaks conservatively."
        )
    if key == "plate_standard_curve":
        return (
            "- Evaluate linearity/range candidate of the standard curve.\n"
            "- Report simple fit diagnostics if computable.\n"
            "- Coerce numeric columns with pd.to_numeric(errors='coerce') before any fit."
        )
    if key == "plate_sample_concentration_bar":
        return (
            "- Compare sample/group endpoint or concentration values descriptively.\n"
            "- Flag unusually high variance if visible from data."
        )
    if key == "plate_michaelis_menten_curve":
        return (
            "- Assess trend over time or concentration conservatively.\n"
            "- Estimate simple slope/rate candidates if possible."
        )
    if key == "plate_fret_time_curve":
        return (
            "- Analyze FRET ratio or donor/acceptor time trends conservatively.\n"
            "- Report trend direction and obvious anomalies."
        )
    if key in {"ce_dna_electropherogram", "ce_rna_electropherogram", "ce_virtual_gel"}:
        return (
            "- Summarize rough peak structure, dominant peak region, and signal range.\n"
            "- Avoid overclaiming molecular identity."
        )
    return "- Perform conservative chart-level analysis and output structured JSON."


VIZ_CODE_SYSTEM_PROMPT = """
You are a scientific plotting code generator.

You will receive:
- parsed_data_path: path to a CSV file containing a task-specific plotting view
- output_image_dir: directory where images should be saved
- data_profile_json
- chart_id
- chart_name
- chart_note
- chart_hint

Write Python code that:
1) reads the CSV from parsed_data_path,
2) validates required columns,
3) creates the requested plot conservatively using matplotlib,
4) saves one or more PNG files under output_image_dir,
5) prints a STRICT JSON object to stdout only if needed,
6) does not require seaborn.

Requirements:
- Use matplotlib only.
- No network access.
- Be conservative and explicit with column checks.
- If required columns are missing, raise ValueError with a helpful message.
- Use English chart titles in matplotlib figures. Do not put Chinese text in
  `plt.title`, `ax.set_title`, figure suptitles, or saved chart titles. Prefer
  concise English titles derived from the chart_key, such as "qPCR Melt Curve",
  "Relative Expression", "Amplification Curve", "Standard Curve", "FRET Curve",
  or "Michaelis-Menten Curve".
- Avoid dense text annotations on curves. For multi-trace plots, especially
  qPCR melt curves with many wells, do not draw Tm labels directly on the plot
  unless the user explicitly requests on-curve labels. Put Tm/peak values in
  tables, legends, or the report instead.
- For qPCR melt curves, plot all well traces in one overview PNG by default,
  matching common instrument-software displays. Use low alpha, thin lines, and
  avoid per-trace text labels so the all-well figure remains readable. Do not
  split qPCR melt curves into paged images unless the user explicitly asks for
  pagination.
- For other dense time/curve charts, if the input CSV contains a `plot_page`
  column, generate one PNG per `plot_page` value. Filter each page's data before
  plotting and save files with page suffixes such as `chart_01_page_01.png`,
  `chart_01_page_02.png`.
- Placeholder usage is MANDATORY. The actual paths contain Windows backslashes;
  hardcoding them as Python string literals causes invalid-escape warnings and
  wrong characters. Always route through the placeholder, which the post-
  processor replaces with a properly-escaped literal.
- STRICT RULE: a placeholder may appear ONLY as the right-hand side of a
  top-level variable assignment. Never embed a placeholder inside an f-string,
  concatenation, or function call — after substitution the surrounding quotes
  will collide and cause SyntaxError. Use the variable instead.
- Use "{{PARSED}}" as placeholder for parsed_data_path, like:
      parsed_data_path = "{{PARSED}}"
- Use "{{OUTPUT_DIR}}" as placeholder for output_image_dir, like:
      output_image_dir = "{{OUTPUT_DIR}}"
- IMPORTANT: The generated script must be SELF-CONTAINED. Every module used
  ANYWHERE in the code — including in debug prints or error paths — MUST be
  imported at the top of the file. If you use `sys.stderr`, you MUST
  `import sys`. Missing imports cause `NameError` and the step FAILS.
  Always begin with:
      import sys, os, json
      import pandas as pd
      import numpy as np
      import matplotlib
      matplotlib.use("Agg")
      import matplotlib.pyplot as plt
      from pathlib import Path
  (omit only what you truly don't use.)

Return STRICT JSON ONLY:

{
  "charts": [
    {
      "chart_id": "string",
      "python_code": "string",
      "output_files": ["chart_01.png"],
      "notes": "string"
    }
  ]
}
""".strip()


VIZ_ANALYSIS_SYSTEM_PROMPT = """
You are a scientific chart-level analysis code generator.

You will receive:
- parsed_data_path: path to CSV with chart-ready data
- data_profile_json
- chart_id
- chart_name
- chart_note
- chart_hint
- image_paths_json

Write Python code that contributes the DATA-EVIDENCE step of chart analysis:
1) reads the CSV,
2) optionally records image path metadata if useful, but does not claim direct visual interpretation,
3) performs conservative chart-level data analysis,
4) prints STRICT JSON to stdout.

Requirements:
- Do not make unsupported causal claims.
- Do not decide whether the result meets the user's goal; that is handled after
  visual_observation and data_evidence are combined.
- If image paths are provided, do not pretend to inspect image pixels unless your
  generated code actually reads them. Prefer CSV-derived evidence.
- Output JSON only to stdout.
- For numeric analysis, use pd.to_numeric(..., errors="coerce") and drop invalid rows before fitting or summarizing.
- Do NOT call .astype(float) directly on raw CSV columns.
- If numeric columns are unusable after coercion, return a structured warning/error JSON object instead of crashing.
- Placeholder usage is MANDATORY. The actual path contains Windows backslashes;
  do NOT hardcode it as a Python string literal. Always go through the
  placeholder, which is replaced with a properly-escaped literal.
- STRICT RULE: a placeholder may appear ONLY as the right-hand side of a
  top-level variable assignment. Never embed a placeholder inside an f-string,
  concatenation, or function call — after substitution the surrounding quotes
  will collide and cause SyntaxError. Use the variable instead.
- Use "{{PARSED}}" as placeholder for parsed_data_path, like:
      parsed_data_path = "{{PARSED}}"
- Use "{{IMAGES_JSON}}" as placeholder for image_paths_json (a list literal,
  NOT a string). Use it directly as a list — do not call json.loads on it:
      image_paths = {{IMAGES_JSON}}
- JSON output MUST use `default=str` or explicit casts — numpy int64/float64
  are not JSON-native and crash json.dumps.
- IMPORTANT: The generated script must be SELF-CONTAINED. Every module used
  ANYWHERE in the code — including in debug prints or error paths — MUST be
  imported at the top of the file. If you use `sys.stderr`, you MUST
  `import sys`. Missing imports cause `NameError` and the step FAILS.
  Always begin with:
      import sys, os, json
      import pandas as pd
      import numpy as np
      from pathlib import Path

Return STRICT JSON ONLY:

{
  "results": [
    {
      "result_id": "string",
      "analysis_purpose": "string",
      "python_code": "string",
      "notes": "string"
    }
  ]
}
""".strip()


@tool
def generate_viz_python_code_tool(
    chart_id: str,
    chart_key: str,
    requirement: str,
    data_profile: Dict[str, Any],
    parsed_data_path: str,
    output_image_dir: str,
) -> Dict[str, Any]:
    """
    Generate plotting python code for one chart spec.
    """
    llm = _make_llm()
    chart_hint = get_chart_hint(chart_key)

    payload = {
        "parsed_data_path": parsed_data_path,
        "output_image_dir": output_image_dir,
        "data_profile_json": data_profile or {},
        "chart_id": chart_id,
        "chart_name": chart_key,
        "chart_note": requirement or "",
        "chart_hint": chart_hint,
    }

    messages = [
        SystemMessage(content=VIZ_CODE_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]

    resp = llm.invoke(messages)
    content = getattr(resp, "content", "") if resp is not None else ""
    data = _extract_json(content)
    if data is None:
        data = _fix_to_valid_json(llm, content) or {}

    charts = (data.get("charts") or []) if isinstance(data, dict) else []
    cleaned: List[Dict[str, Any]] = []

    for c in charts:
        if not isinstance(c, dict):
            continue
        code = str(c.get("python_code") or "")
        code = _replace_python_placeholder(
            code, "{{PARSED}}", json.dumps(parsed_data_path, ensure_ascii=False)
        )
        code = _replace_python_placeholder(
            code, "{{OUTPUT_DIR}}", json.dumps(output_image_dir, ensure_ascii=False)
        )

        output_files = c.get("output_files") or []
        if isinstance(output_files, str):
            output_files = [output_files]
        output_files = [str(x) for x in output_files if str(x).strip()]
        if not output_files:
            output_files = [f"{chart_id}.png"]

        cleaned.append(
            {
                "chart_id": str(c.get("chart_id") or chart_id),
                "python_code": code,
                "output_files": output_files,
                "notes": str(c.get("notes") or ""),
            }
        )

    return {"charts": cleaned}


@tool
def generate_viz_analysis_python_code_tool(
    chart_id: str,
    chart_key: str,
    requirement: str,
    data_profile: Dict[str, Any],
    parsed_data_path: str,
    image_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Generate chart-level analysis python code for one visualization.
    """
    llm = _make_llm()
    chart_hint = get_chart_analysis_hint(chart_key)
    image_paths = image_paths or []
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    image_paths = [str(p) for p in image_paths if isinstance(p, str)]

    payload = {
        "parsed_data_path": parsed_data_path,
        "data_profile_json": data_profile or {},
        "chart_id": chart_id,
        "chart_name": chart_key,
        "chart_note": requirement or "",
        "chart_hint": chart_hint,
        "image_paths_json": image_paths,
    }

    messages = [
        SystemMessage(content=VIZ_ANALYSIS_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]

    resp = llm.invoke(messages)
    content = getattr(resp, "content", "") if resp is not None else ""
    data = _extract_json(content)
    if data is None:
        data = _fix_to_valid_json(llm, content) or {}

    results = []
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        results = data.get("results") or []
    elif isinstance(data, dict) and isinstance(data.get("python_code"), str):
        results = [data]

    cleaned: List[Dict[str, Any]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        code = str(r.get("python_code") or "")
        code = _replace_python_placeholder(
            code, "{{PARSED}}", json.dumps(parsed_data_path, ensure_ascii=False)
        )
        code = _replace_python_placeholder(
            code, "{{IMAGES_JSON}}", json.dumps(image_paths, ensure_ascii=False)
        )

        cleaned.append(
            {
                "result_id": str(r.get("result_id") or f"{chart_id}__analysis_01"),
                "analysis_purpose": str(
                    r.get("analysis_purpose") or requirement or f"Analyze {chart_key}"
                ),
                "python_code": code,
                "notes": str(r.get("notes") or ""),
            }
        )

    return {"results": cleaned}


VISUAL_OBSERVATION_SYSTEM_PROMPT = """
You are a scientific chart visual-inspection assistant.

Task:
- Inspect rendered chart image(s) directly.
- Describe only visible chart evidence.
- Do not use raw data values unless they are printed in the image.
- Do not infer causality or statistical significance.
- Return STRICT JSON only.

Return this JSON shape:
{
  "status": "success",
  "chart_type_matches_expected": "yes|no|unknown",
  "axes_visible": "yes|no|partial|unknown",
  "legend_visible": "yes|no|partial|unknown",
  "visual_trends": ["short visible trend statements"],
  "quality_issues": ["label overlap, missing legend, dense curves, etc."],
  "summary": "one concise paragraph"
}
""".strip()


@tool
def generate_visual_observation_tool(
    chart_id: str,
    chart_key: str,
    requirement: str,
    image_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run a vision-capable model over rendered chart image(s) and return visual observations.
    """
    image_paths = image_paths or []
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    image_paths = [str(p) for p in image_paths if str(p).strip()]

    if not image_paths:
        return {
            "status": "missing_image",
            "reason": "No rendered image path was provided.",
            "chart_id": chart_id,
            "chart_key": chart_key,
            "image_paths": [],
        }

    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"chart_id: {chart_id}\n"
                f"chart_key: {chart_key}\n"
                f"requirement: {requirement or ''}\n"
                "Inspect the chart image. Focus on chart type, axis/legend visibility, "
                "visible trends, and visual quality issues. Return strict JSON only."
            ),
        }
    ]

    usable_paths: List[str] = []
    try:
        for path in image_paths[:3]:
            p = Path(path)
            if not p.exists():
                continue
            if p.stat().st_size > 10 * 1024 * 1024:
                continue
            usable_paths.append(str(p))
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _image_path_to_data_url(str(p)),
                    },
                }
            )

        if not usable_paths:
            return {
                "status": "missing_image",
                "reason": "No readable image file was available.",
                "chart_id": chart_id,
                "chart_key": chart_key,
                "image_paths": image_paths,
            }

        llm = _make_vision_llm()
        resp = llm.invoke(
            [
                SystemMessage(content=VISUAL_OBSERVATION_SYSTEM_PROMPT),
                HumanMessage(content=content),
            ]
        )
        data = _extract_json(getattr(resp, "content", "") or "")
        if data is None:
            return {
                "status": "failed",
                "reason": "Vision model did not return valid JSON.",
                "chart_id": chart_id,
                "chart_key": chart_key,
                "image_paths": usable_paths,
            }

        data.setdefault("status", "success")
        data["chart_id"] = chart_id
        data["chart_key"] = chart_key
        data["image_paths"] = usable_paths
        data["vision_model"] = os.getenv("VISION_LLM_MODEL", DEFAULT_VISION_MODEL)
        return data
    except Exception as exc:
        return {
            "status": "vision_unavailable",
            "reason": repr(exc),
            "chart_id": chart_id,
            "chart_key": chart_key,
            "image_paths": usable_paths or image_paths,
            "vision_model": os.getenv("VISION_LLM_MODEL", DEFAULT_VISION_MODEL),
        }
