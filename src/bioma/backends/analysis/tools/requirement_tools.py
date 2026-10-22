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


ALLOWED_INSTRUMENT_FAMILY = {
    "qpcr",
    "plate_reader",
    "capillary_electrophoresis",
    "unknown",
}

ALLOWED_PRIMARY_INTENT = {
    "analyze_data",
    "generate_report",
    "revise_report",
    "explain_result",
    "unknown",
}

DEFAULT_REQUIREMENT = {
    "primary_intent": "analyze_data",
    "requested_outputs": [],
    "analysis_goals": [],
    "chart_preferences": [],
    "report_preferences": {
        "tone": "",
        "length": "",
        "language": "",
        "include_sections": [],
    },
    "must_have_dimensions": [],
    "user_constraints": [],
    "notes": [],
}


REQUIREMENT_OUTPUT_SCHEMA = """
Chart requirements must be explicit: put every requested visualization in
chart_preferences, including its purpose when no chart type was specified.
Leave chart_preferences empty when no visualization was requested. Mentions of
figures in consistency rules, evidence references, or exclusions do not request
new charts. Do not invent a chart requirement from field names or file formats.

Return STRICT JSON only with this structure:

{
  "resolved_requirement": {
    "primary_intent": "analyze_data|generate_report|revise_report|explain_result|unknown",
    "requested_outputs": ["..."],
    "analysis_goals": ["..."],
    "chart_preferences": ["..."],
    "report_preferences": {
      "tone": "...",
      "length": "...",
      "language": "...",
      "include_sections": ["..."]
    },
    "must_have_dimensions": ["..."],
    "user_constraints": ["..."],
    "notes": ["..."]
  },
  "requirement_patch": {
    "primary_intent": "",
    "requested_outputs": ["..."],
    "analysis_goals": ["..."],
    "chart_preferences": ["..."],
    "report_preferences": {},
    "must_have_dimensions": ["..."],
    "user_constraints": ["..."],
    "notes": ["..."]
  },
  "instrument_family": "qpcr|plate_reader|capillary_electrophoresis|unknown",
  "experiment_kind_hint": "string",
  "context_summary": "string",
  "experiment_background": {},
  "issues": [
    {"code": "string", "severity": "low|medium|high", "message": "string"}
  ],
  "need_clarification": true,
  "question_to_user": "string",
  "fatal": false
}
""".strip()


INITIAL_REQUIREMENT_SYSTEM_PROMPT = f"""
You are the initial requirement-extraction module for a scientific data-analysis agent.

This is the FIRST requirement pass for a task.

Your job in THIS STEP is ONLY to:
1) extract and normalize the user's initial task requirement,
2) infer instrument_family conservatively but decisively from user request + dialog history + metadata + preview hints,
3) summarize experiment/context background only when supported by the user request, dialog, or metadata,
4) perform input QC: list issues and decide whether clarification is truly blocking,
5) produce the initial best-available requirement object for downstream stages.

You MUST NOT:
- design chart_plan,
- design analysis_plan,
- parse tables,
- propose transformations,
- choose specific chart types or plotting templates,
- invent data columns that are not evidenced,
- assume a fixed schema across all subtables,
- hallucinate experimental facts from weak hints.

You will receive:
- current_user_request
- dialog_history
- data_artifact_json
- preview_text
- data_prep_meta_json

{REQUIREMENT_OUTPUT_SCHEMA}

General principles:
- Be conservative, but do not overuse "unknown".
- The preview text is truncated and may be incomplete. Use it only as supporting hints, not as the sole source of truth.
- Do NOT hallucinate column names, sample identities, experimental groups, control conditions, or instrument-specific facts.
- If evidence is partial, prefer a high-level but usable conclusion over an overly specific invented one.
- Choose the best-supported interpretation from the available evidence.
- Use "unknown" only when no instrument family is sufficiently supported.

Instrument family classification:
- You MUST choose exactly one allowed value:
  - "qpcr"
  - "plate_reader"
  - "capillary_electrophoresis"
  - "unknown"

Evidence priority for instrument_family:
1. explicit wording in current_user_request
2. confirmed information from dialog_history
3. metadata / artifact metadata / file-level context
4. preview_text hints

Important:
- Do NOT let weak or ambiguous preview hints override explicit user wording.
- Missing details do NOT automatically imply "unknown".
- Prefer the best-supported family rather than defaulting to "unknown" too easily.

Instrument family decision guide:
- Choose "qpcr" when the evidence clearly suggests qPCR-style workflows, such as:
  - qPCR, PCR cycle, Ct, Cq, amplification curve, melt peak, melt curve in PCR context, ΔCt, ΔΔCt, relative expression, primer, PCR replicate context.
- Choose "plate_reader" when the evidence clearly suggests plate-based optical measurements, such as:
  - plate reader, 96-well plate, 384-well plate, fluorescence, absorbance, OD, luminescence, kinetic plate read, endpoint plate assay, well positions like A1/B2, plate-based screening.
- Choose "capillary_electrophoresis" when the evidence clearly suggests fragment/separation workflows, such as:
  - electropherogram, fragment analysis, fragment size, bp, ladder, capillary electrophoresis, Bioanalyzer, TapeStation, peak sizing.
- Choose "unknown" only if none of the above families is sufficiently supported by the request, history, metadata, or preview.

Strong preference rules:
- If the user explicitly mentions a plate-based fluorescence experiment on a 96-well or 384-well plate, prefer "plate_reader" unless there is stronger contradictory evidence.
- If the user explicitly mentions qPCR / Ct / Cq / cycle-like language, prefer "qpcr" unless there is stronger contradictory evidence.
- If the user explicitly mentions electropherogram / fragment-size / capillary-electrophoresis-like language, prefer "capillary_electrophoresis" unless there is stronger contradictory evidence.

Requirement normalization:
- resolved_requirement should be the initial best available requirement object for the task.
- requirement_patch should contain ONLY what was explicitly expressed in this first turn or clearly confirmed in the current user request.
- Normalize user intent, requested outputs, report preferences, constraints, and must-have dimensions ONLY into the schema fields that actually exist.
- Do not add concrete fields or dimensions unless they are supported by the request, dialog, metadata, or strong preview evidence.
- If the user asks for summary / EDA / reporting rather than a specialized quantitative workflow, prefer a general requirement rather than over-specializing.
- If the user explicitly asks for a specific analysis target (for example Tm, melt curve, report, concentration, QC), preserve that request clearly in resolved_requirement.

Context summarization:
- You may summarize experiment context from dialog history, user request, and metadata.
- Keep experiment_background and context_summary concise, evidence-based, and useful for downstream nodes.
- Do not fabricate hypothesis, groups, control names, mutation labels, or sample mappings if they are not given.

QC and clarification rules:
- Your job is to identify whether there are truly blocking clarification needs.
- Ask a clarification question ONLY if it is genuinely blocking the next stage.
- A clarification is blocking only when downstream execution would be impossible, fundamentally ambiguous, or likely invalid without it.
- Missing labels, missing sample-name mappings, missing richer business context, or missing nicer report annotations are often NON-blocking if best-effort continuation is still possible.
- If the user already requests best-effort continuation, says no more information is available, or explicitly allows assumptions/default handling, prefer proceeding with assumptions/default identifiers and record them in notes or user_constraints instead of asking again.
- If a non-blocking issue affects interpretation, labeling, confidence, or report richness, record it as an issue, but do not force clarification.
- fatal=true only when the request is impossible, unsupported in principle, or fundamentally contradictory.
- If instrument_family is unknown and that uncertainty truly prevents safe next-stage execution, clarification may be needed.
- If there is any reasonable best-effort path, prefer non-fatal and proceed.

Issue reporting:
- Each issue should be concrete, grounded, and action-relevant.
- Prefer describing uncertainty explicitly, for example:
  - "preview is truncated"
  - "sample mapping not provided"
  - "instrument family weakly evidenced"
- Do not turn every missing detail into a blocking issue.
- Use issues to record uncertainty, not to avoid making a reasonable best-supported decision.

Output rules:
- Output JSON only.
- Follow the provided schema strictly.
- Use only supported enum values and field names defined by the schema.
- Do not output markdown.
- Do not output explanations outside the JSON.
""".strip()


UPDATE_REQUIREMENT_SYSTEM_PROMPT = f"""
You are the requirement-update module for a scientific data-analysis agent.

This is a FOLLOW-UP requirement update, not the first extraction pass.

You will receive:
- current_user_request
- dialog_history
- pending_question
- previous_requirement
- previous_resolved_parser_target
- previous_experiment_background
- data_artifact_json
- preview_text
- data_prep_meta_json

Your job in THIS STEP is ONLY to:
1) interpret the user's reply or requirement modification,
2) produce an incremental requirement_patch,
3) preserve previously confirmed requirement content unless the user explicitly changes it,
4) update parser-target hints conservatively,
5) decide whether more clarification is still truly blocking.

You MUST NOT:
- drop prior confirmed fields just because they were not repeated,
- design chart_plan,
- design analysis_plan.

{REQUIREMENT_OUTPUT_SCHEMA}

Rules:
- Treat previous_requirement as the current source of truth.
- requirement_patch should contain only changes, additions, or corrections for this turn.
- resolved_requirement may be included as a convenience, but it must remain compatible with previous_requirement plus the new reply.
- If the user says there is no more information, asks you to continue, or allows reasonable assumptions / best effort, then set need_clarification=false unless the task is truly blocked.
- In that case, record the assumption or constraint in notes/user_constraints instead of asking the same question again.
- Only ask follow-up questions when the missing information blocks parse/planning/execution in a concrete way.
- fatal=true only when the request is impossible or fundamentally contradictory.
- Output JSON only.
""".strip()


def _make_llm() -> Any:
    return make_json_llm(
        model_env_name="REQUIREMENT_LLM_MODEL",
        api_env_name="BAIDU_API_KEY",
        base_env_name="BAIDU_BASE_URL",
    )


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _as_list_str(x: Any) -> List[str]:
    out: List[str] = []
    for item in _as_list(x):
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _uniq_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


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
    repair_messages = [
        SystemMessage(
            content=(
                "You are a JSON repair tool. "
                "Convert the input into STRICT valid JSON only. "
                "Do not add markdown or explanations."
            )
        ),
        HumanMessage(content=raw_text or ""),
    ]
    try:
        resp = llm.invoke(repair_messages)
        return _extract_json(getattr(resp, "content", "") or "")
    except Exception:
        return None


def _coerce_issue_list(raw_issues: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in _as_list(raw_issues):
        if isinstance(it, dict):
            out.append(
                {
                    "code": str(it.get("code") or "GENERIC"),
                    "severity": str(it.get("severity") or "medium"),
                    "message": str(
                        it.get("message") or it.get("detail") or "Unspecified issue"
                    ),
                }
            )
        else:
            s = str(it).strip()
            if s:
                out.append(
                    {
                        "code": "GENERIC",
                        "severity": "medium",
                        "message": s,
                    }
                )
    return out


def _normalize_report_preferences(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}
    return {
        "tone": str(obj.get("tone") or "").strip(),
        "length": str(obj.get("length") or "").strip(),
        "language": str(obj.get("language") or "").strip(),
        "include_sections": _uniq_keep_order(_as_list_str(obj.get("include_sections"))),
    }


def _normalize_requirement_obj(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}

    primary_intent = str(obj.get("primary_intent") or "unknown").strip()
    if primary_intent not in ALLOWED_PRIMARY_INTENT:
        primary_intent = "unknown"

    return {
        "primary_intent": primary_intent,
        "requested_outputs": _uniq_keep_order(
            _as_list_str(obj.get("requested_outputs"))
        ),
        "analysis_goals": _uniq_keep_order(_as_list_str(obj.get("analysis_goals"))),
        "chart_preferences": _uniq_keep_order(
            _as_list_str(obj.get("chart_preferences"))
        ),
        "report_preferences": _normalize_report_preferences(
            obj.get("report_preferences")
        ),
        "must_have_dimensions": _uniq_keep_order(
            _as_list_str(obj.get("must_have_dimensions"))
        ),
        "user_constraints": _uniq_keep_order(_as_list_str(obj.get("user_constraints"))),
        "notes": _uniq_keep_order(_as_list_str(obj.get("notes"))),
    }


def _normalize_partial_requirement_obj(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}

    primary_intent = str(obj.get("primary_intent") or "").strip()
    if primary_intent and primary_intent not in ALLOWED_PRIMARY_INTENT:
        primary_intent = ""

    return {
        "primary_intent": primary_intent,
        "requested_outputs": _uniq_keep_order(
            _as_list_str(obj.get("requested_outputs"))
        ),
        "analysis_goals": _uniq_keep_order(_as_list_str(obj.get("analysis_goals"))),
        "chart_preferences": _uniq_keep_order(
            _as_list_str(obj.get("chart_preferences"))
        ),
        "report_preferences": _normalize_report_preferences(
            obj.get("report_preferences")
        ),
        "must_have_dimensions": _uniq_keep_order(
            _as_list_str(obj.get("must_have_dimensions"))
        ),
        "user_constraints": _uniq_keep_order(_as_list_str(obj.get("user_constraints"))),
        "notes": _uniq_keep_order(_as_list_str(obj.get("notes"))),
    }


def _coerce_result(raw: Dict[str, Any]) -> Dict[str, Any]:
    resolved_requirement = _normalize_requirement_obj(raw.get("resolved_requirement"))
    requirement_patch = _normalize_partial_requirement_obj(raw.get("requirement_patch"))

    instrument_family = str(raw.get("instrument_family") or "unknown").strip()
    issues = _coerce_issue_list(raw.get("issues"))

    if instrument_family not in ALLOWED_INSTRUMENT_FAMILY:
        issues.append(
            {
                "code": "INVALID_INSTRUMENT_FAMILY",
                "severity": "medium",
                "message": f"Invalid instrument_family: {instrument_family}",
            }
        )
        instrument_family = "unknown"

    experiment_background = raw.get("experiment_background")
    if not isinstance(experiment_background, dict):
        experiment_background = {}
        issues.append(
            {
                "code": "INVALID_EXPERIMENT_BACKGROUND",
                "severity": "low",
                "message": "experiment_background must be an object; replaced with {}.",
            }
        )

    return {
        "resolved_requirement": resolved_requirement,
        "requirement_patch": requirement_patch,
        "instrument_family": instrument_family,
        "experiment_kind_hint": str(
            raw.get("experiment_kind_hint") or "unknown"
        ).strip(),
        "context_summary": str(raw.get("context_summary") or "").strip(),
        "experiment_background": experiment_background,
        "issues": issues,
        "need_clarification": bool(raw.get("need_clarification", False)),
        "question_to_user": str(raw.get("question_to_user") or "").strip(),
        "fatal": bool(raw.get("fatal", False)),
    }


def issues_to_text(issues: Any) -> str:
    parts: List[str] = []
    for it in _as_list(issues):
        if isinstance(it, dict):
            parts.append(
                str(it.get("message") or it.get("detail") or it.get("code") or "")
            )
        else:
            parts.append(str(it))
    parts = [p for p in parts if p]
    return "; ".join(parts)


def _invoke_requirement_llm(
    system_prompt: str, user_payload: Dict[str, Any]
) -> Dict[str, Any]:
    llm = _make_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=json.dumps(user_payload, ensure_ascii=False)),
    ]

    resp = llm.invoke(messages)
    content = getattr(resp, "content", "") if resp is not None else ""

    parsed = _extract_json(content)
    if parsed is None:
        parsed = _fix_to_valid_json(llm, content) or {}

    return _coerce_result(parsed)


def _prepare_artifact_payload(
    data_artifact: Dict[str, Any], data_prep_meta: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    artifact_payload = dict(data_artifact or {})
    if data_prep_meta:
        artifact_payload["data_prep_meta"] = data_prep_meta
    return artifact_payload


def _extract_preview_text(artifact_payload: Dict[str, Any]) -> str:
    try:
        preview_text = (artifact_payload.get("preview") or {}).get("text") or ""
    except Exception:
        preview_text = ""
    return preview_text or "<empty>"


@tool
def extract_initial_requirement_tool(
    current_user_request: str,
    dialog_history: str,
    data_artifact: Dict[str, Any],
    data_prep_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Extract the initial task requirement and parser target from the first user turn.
    """
    artifact_payload = _prepare_artifact_payload(data_artifact, data_prep_meta)
    preview_text = _extract_preview_text(artifact_payload)

    user_payload = {
        "current_user_request": current_user_request or "",
        "dialog_history": dialog_history or "<empty>",
        "data_artifact_json": artifact_payload,
        "preview_text": preview_text,
        "data_prep_meta_json": data_prep_meta or {},
    }

    return _invoke_requirement_llm(INITIAL_REQUIREMENT_SYSTEM_PROMPT, user_payload)


@tool
def update_requirement_tool(
    current_user_request: str,
    dialog_history: str,
    previous_requirement: Dict[str, Any],
    pending_question: str = "",
    previous_resolved_parser_target: Optional[Dict[str, Any]] = None,
    previous_experiment_background: Optional[Dict[str, Any]] = None,
    data_artifact: Optional[Dict[str, Any]] = None,
    data_prep_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Update an existing requirement state from a clarification reply or modification request.
    """
    artifact_payload = _prepare_artifact_payload(data_artifact or {}, data_prep_meta)
    preview_text = _extract_preview_text(artifact_payload)

    user_payload = {
        "current_user_request": current_user_request or "",
        "dialog_history": dialog_history or "<empty>",
        "pending_question": pending_question or "",
        "previous_requirement": previous_requirement or DEFAULT_REQUIREMENT,
        "previous_resolved_parser_target": previous_resolved_parser_target or {},
        "previous_experiment_background": previous_experiment_background or {},
        "data_artifact_json": artifact_payload,
        "preview_text": preview_text,
        "data_prep_meta_json": data_prep_meta or {},
    }

    return _invoke_requirement_llm(UPDATE_REQUIREMENT_SYSTEM_PROMPT, user_payload)


@tool
def understand_request_tool(
    current_user_request: str,
    dialog_history: str,
    previous_requirement: Dict[str, Any],
    data_artifact: Dict[str, Any],
    data_prep_meta: Optional[Dict[str, Any]] = None,
    mode: str = "auto",
    pending_question: str = "",
    previous_resolved_parser_target: Optional[Dict[str, Any]] = None,
    previous_experiment_background: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Dispatch initial requirement extraction or an incremental user clarification.
    """
    normalized_mode = str(mode or "auto").strip().lower()
    has_previous_requirement = bool(previous_requirement)
    use_update_path = normalized_mode == "update" or (
        normalized_mode == "auto"
        and (bool(pending_question) or has_previous_requirement)
    )

    if use_update_path:
        return update_requirement_tool.invoke(
            {
                "current_user_request": current_user_request,
                "dialog_history": dialog_history,
                "previous_requirement": previous_requirement,
                "pending_question": pending_question,
                "previous_resolved_parser_target": previous_resolved_parser_target
                or {},
                "previous_experiment_background": previous_experiment_background or {},
                "data_artifact": data_artifact,
                "data_prep_meta": data_prep_meta or {},
            }
        )

    return extract_initial_requirement_tool.invoke(
        {
            "current_user_request": current_user_request,
            "dialog_history": dialog_history,
            "data_artifact": data_artifact,
            "data_prep_meta": data_prep_meta or {},
        }
    )


# optional alias, if you still like the old name semantically
analyze_requirement_and_context_tool = understand_request_tool
