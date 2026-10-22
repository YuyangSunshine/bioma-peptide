from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bioma.backends.analysis.artifacts import task_artifact_dir


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def resolve_report_dir(task_id: str, artifact_root: str = "artifacts") -> str:
    return str(task_artifact_dir(artifact_root, task_id, "reporting"))


def normalize_visualization_paths_for_report(
    visualizations: List[Dict[str, Any]],
    report_dir: str,
) -> List[Dict[str, Any]]:
    """
    Convert visualization image paths into Markdown paths that resolve from the
    report file's directory. Artifact records may store paths relative to the
    project root; Markdown readers usually resolve image links relative to the
    .md file itself.
    """
    report_base = Path(report_dir).resolve()
    project_root = Path.cwd().resolve()
    out: List[Dict[str, Any]] = []
    for viz in visualizations or []:
        if not isinstance(viz, dict):
            continue
        item = dict(viz)
        normalized_paths: List[str] = []
        for raw_path in item.get("image_paths") or []:
            raw = str(raw_path).strip()
            if not raw:
                continue
            path_obj = Path(raw)
            abs_path = (
                path_obj
                if path_obj.is_absolute()
                else project_root / raw.replace("\\", "/")
            )
            try:
                rel_path = os.path.relpath(abs_path.resolve(), report_base)
                normalized_paths.append(rel_path.replace("\\", "/"))
            except Exception:
                normalized_paths.append(raw.replace("\\", "/"))
        item["image_paths"] = normalized_paths
        out.append(item)
    return out


def build_report_outline(
    requirement: Dict[str, Any],
    analysis_results: Dict[str, Any],
    visualizations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    report_pref = (requirement or {}).get("report_preferences") or {}
    include_sections = _as_list_str(report_pref.get("include_sections"))

    default_sections = [
        "Analysis Objective",
        "Data Overview",
        "Key Findings",
        "Figure-by-Figure Interpretation",
        "Limitations / Notes",
        "Conclusion",
    ]
    if include_sections:
        sections = include_sections
    else:
        sections = default_sections

    return {
        "sections": sections,
        "has_visualizations": bool(visualizations),
        "n_findings": len((analysis_results or {}).get("findings") or []),
    }


def build_report_front_matter(
    requirement: Dict[str, Any],
    experiment_background: Dict[str, Any],
) -> str:
    primary_intent = str((requirement or {}).get("primary_intent") or "").strip()
    requested_outputs = _as_list_str((requirement or {}).get("requested_outputs"))
    notes = _as_list_str((requirement or {}).get("notes"))

    lines = [
        "# Data Analysis Report",
        "",
    ]

    if primary_intent:
        lines.append(f"- **Primary intent:** {primary_intent}")
    if requested_outputs:
        lines.append(f"- **Requested outputs:** {', '.join(requested_outputs)}")

    exp_name = str((experiment_background or {}).get("experiment_name") or "").strip()
    if exp_name:
        lines.append(f"- **Experiment:** {exp_name}")

    if notes:
        lines.append(f"- **Notes:** {'; '.join(notes[:5])}")

    lines.append("")
    return "\n".join(lines)


def _collect_unembedded_figure_items(
    visualizations: List[Dict[str, Any]],
    body_markdown: str,
) -> List[Dict[str, Any]]:
    body = str(body_markdown or "")
    missing: List[Dict[str, Any]] = []

    def _norm(s: str) -> str:
        return s.replace("\\", "/").lower()

    body_norm = _norm(body)
    for viz in visualizations or []:
        if not isinstance(viz, dict):
            continue
        if str(viz.get("status") or "").lower() not in {"success", "ok"}:
            continue
        chart_id = str(viz.get("chart_id") or "").strip()
        image_paths = [str(p) for p in (viz.get("image_paths") or []) if str(p).strip()]
        if not image_paths:
            continue
        if any(_norm(p) in body_norm for p in image_paths):
            continue
        missing.append(
            {
                "chart_id": chart_id,
                "chart_key": str(viz.get("chart_key") or "").strip(),
                "title": str(viz.get("title") or "").strip(),
                "image_paths": image_paths,
            }
        )
    return missing


def _render_figure_item(item: Dict[str, Any]) -> List[str]:
    label = str(
        item.get("chart_id") or item.get("title") or item.get("chart_key") or "figure"
    )
    caption_bits = [
        str(b)
        for b in [item.get("chart_id"), item.get("chart_key"), item.get("title")]
        if b
    ]
    caption = " - ".join(dict.fromkeys(caption_bits)) or label
    lines = [f"### {caption}"]
    for p in item.get("image_paths") or []:
        lines.append(f"![{label}]({p})")
    lines.append("")
    return lines


def _insert_figure_items_into_interpretation_section(
    body_markdown: str,
    figure_items: List[Dict[str, Any]],
) -> str:
    if not figure_items:
        return str(body_markdown or "").strip()

    body = str(body_markdown or "").strip()
    if not body:
        lines = ["## 4. Figure-by-Figure Interpretation"]
        for item in figure_items:
            lines.extend(_render_figure_item(item))
        return "\n".join(lines).strip()

    lines = body.splitlines()
    heading_re = re.compile(r"^(#{1,6})\s*(?:\d+[\.\)]\s*)?(.*)$")
    headings: List[Tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        match = heading_re.match(line.strip())
        if not match:
            continue
        headings.append((idx, len(match.group(1)), match.group(2).strip()))

    figure_heading: Tuple[int, int, str] | None = None
    for item in headings:
        title = item[2].lower()
        compact = _compact_heading_text(title)
        if (
            "figurebyfigure" in compact
            or ("figure" in title and "interpret" in title)
            or (
                "图" in title
                and ("解读" in title or "解释" in title or "分析" in title)
            )
        ):
            figure_heading = item
            break

    if figure_heading is None:
        block_lines = ["## 4. Figure-by-Figure Interpretation"]
        for item in figure_items:
            block_lines.extend(_render_figure_item(item))
        next_section_index = _find_first_late_report_section(lines, heading_re)
        if next_section_index is None:
            return "\n".join(lines + [""] + block_lines).strip()
        return "\n".join(
            lines[:next_section_index] + block_lines + [""] + lines[next_section_index:]
        ).strip()

    section_start, section_level, _ = figure_heading
    section_end = len(lines)
    for idx, level, _title in headings:
        if idx > section_start and level <= section_level:
            section_end = idx
            break

    subsection_headings = [
        (idx, title)
        for idx, level, title in headings
        if section_start < idx < section_end and level > section_level
    ]
    insert_map: Dict[int, List[Dict[str, Any]]] = {}
    unmatched: List[Dict[str, Any]] = []
    used_heading_indices: set[int] = set()

    for item in figure_items:
        match_idx = _best_matching_figure_heading(
            item, subsection_headings, used_heading_indices
        )
        if match_idx is None:
            unmatched.append(item)
            continue
        used_heading_indices.add(match_idx)
        insert_map.setdefault(match_idx, []).append(item)

    out: List[str] = []
    for idx, line in enumerate(lines):
        out.append(line)
        if idx == section_start and unmatched:
            for item in unmatched:
                out.extend(_render_figure_item(item))
        for item in insert_map.get(idx, []):
            out.extend(_render_figure_image_only(item))
    return "\n".join(out).strip()


def _render_figure_image_only(item: Dict[str, Any]) -> List[str]:
    label = str(
        item.get("chart_id") or item.get("title") or item.get("chart_key") or "figure"
    )
    lines: List[str] = [""]
    for p in item.get("image_paths") or []:
        lines.append(f"![{label}]({p})")
    lines.append("")
    return lines


def _best_matching_figure_heading(
    item: Dict[str, Any],
    headings: List[Tuple[int, str]],
    used_heading_indices: set[int],
) -> Optional[int]:
    tokens = [
        str(item.get("chart_id") or ""),
        str(item.get("chart_key") or ""),
        str(item.get("title") or ""),
    ]
    token_norms = [_compact_heading_text(token) for token in tokens if token]
    token_norms.extend(
        _compact_heading_text(token.replace("_", " ")) for token in tokens if token
    )
    token_norms = [token for token in dict.fromkeys(token_norms) if len(token) >= 6]

    best_idx: Optional[int] = None
    best_score = 0
    for idx, title in headings:
        if idx in used_heading_indices:
            continue
        heading_norm = _compact_heading_text(title)
        score = 0
        for token in token_norms:
            if token and (token in heading_norm or heading_norm in token):
                score = max(score, len(token))
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx


def _compact_heading_text(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(text).lower())


def _find_first_late_report_section(
    lines: List[str], heading_re: re.Pattern[str]
) -> Optional[int]:
    for idx, line in enumerate(lines):
        match = heading_re.match(line.strip())
        if not match:
            continue
        title = match.group(2).strip().lower()
        if (
            "limitation" in title
            or "notes" in title
            or "conclusion" in title
            or "局限" in title
            or "结论" in title
        ):
            return idx
    return None


def build_report_markdown(
    requirement: Dict[str, Any],
    experiment_background: Dict[str, Any],
    body_markdown: str,
    visualizations: Optional[List[Dict[str, Any]]] = None,
) -> str:
    front = build_report_front_matter(
        requirement=requirement,
        experiment_background=experiment_background,
    )
    body_markdown = str(body_markdown or "").strip()
    figure_items = _collect_unembedded_figure_items(visualizations or [], body_markdown)
    body_with_figures = _insert_figure_items_into_interpretation_section(
        body_markdown, figure_items
    )
    return front + "\n" + body_with_figures + "\n"


def _has_unsupported_p_value_claim(report_text: str) -> bool:
    negation_markers = (
        "未执行",
        "未进行",
        "未报告",
        "未提供",
        "不报告",
        "没有",
        "无",
        "not performed",
        "not report",
        "not reported",
        "without",
        "no p",
    )
    claim_patterns = (
        r"\bp\s*[- ]?value\s*[<=>]",
        r"\bp\s*[<=>]",
        r"p\s*值\s*[<=>]",
        r"p值\s*[<=>]",
        r"welch",
        r"\bt[- ]?test\b",
        r"t\s*检验",
        r"t检验",
        r"anova",
    )
    mention_patterns = (
        r"\bp\s*[- ]?value\b",
        r"p\s*值",
        r"p值",
        r"welch",
        r"\bt[- ]?test\b",
        r"t\s*检验",
        r"t检验",
        r"anova",
    )
    for line in str(report_text or "").splitlines():
        low = line.lower()
        if not any(
            re.search(pattern, low, flags=re.IGNORECASE) for pattern in mention_patterns
        ):
            continue
        has_direct_claim = any(
            re.search(pattern, low, flags=re.IGNORECASE) for pattern in claim_patterns
        )
        is_negated_disclaimer = any(
            marker in line or marker in low for marker in negation_markers
        )
        if has_direct_claim and not is_negated_disclaimer:
            return True
        if not is_negated_disclaimer and (
            "p-value" in low or "p 值" in line or "p值" in line
        ):
            return True
    return False


def validate_report(
    report_text: str,
    analysis_results: Dict[str, Any],
    visualizations: List[Dict[str, Any]],
    report_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Conservative validation:
    - figure references
    - p-value claims without stat results
    - empty report
    """
    issues: List[Dict[str, Any]] = []
    report_text = str(report_text or "")

    if not report_text.strip():
        issues.append(
            {
                "code": "EMPTY_REPORT",
                "severity": "high",
                "message": "Report text is empty.",
            }
        )

    report_base = Path(report_dir or ".").resolve()
    for image_ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report_text):
        image_ref = str(image_ref).strip()
        if not image_ref or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", image_ref):
            continue
        image_path = Path(image_ref)
        candidate = image_path if image_path.is_absolute() else report_base / image_ref
        if not candidate.exists():
            issues.append(
                {
                    "code": "BROKEN_FIGURE_PATH",
                    "severity": "medium",
                    "message": f"Figure image path does not exist from report directory: {image_ref}",
                }
            )

    for viz in visualizations or []:
        if not isinstance(viz, dict):
            continue
        chart_id = str(viz.get("chart_id") or "")
        chart_key = str(viz.get("chart_key") or "")
        title = str(viz.get("title") or "")
        image_paths = [str(p) for p in (viz.get("image_paths") or []) if str(p).strip()]

        ref_tokens = [x for x in [chart_id, chart_key, title] if x]
        has_ref = any(token in report_text for token in ref_tokens) or any(
            img in report_text for img in image_paths
        )
        if ref_tokens and not has_ref:
            issues.append(
                {
                    "code": "UNREFERENCED_FIGURE",
                    "severity": "medium",
                    "message": f"Figure/chart not clearly referenced in report: {chart_id or chart_key or title}",
                }
            )

    stat_tests = (analysis_results or {}).get("stat_tests") or {}
    if _has_unsupported_p_value_claim(report_text) and not stat_tests:
        issues.append(
            {
                "code": "UNSUPPORTED_P_VALUE_CLAIM",
                "severity": "medium",
                "message": "Report mentions p-values but analysis_results.stat_tests is empty.",
            }
        )

    return {
        "passed": len(issues) == 0,
        "issues": issues,
    }


def write_report_file(report_dir: str, report_text: str) -> Tuple[str, str]:
    report_id = f"report_{_utc_now_tag()}"
    report_path = os.path.join(report_dir, f"{report_id}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    return report_id, report_path


def build_report_version_entry(
    report_id: str,
    report_path: str,
    note: str,
    analysis_results: Dict[str, Any],
    visualizations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "report_id": report_id,
        "created_at": _utc_now_iso(),
        "report_path": report_path,
        "note": note,
        "based_on_analysis_version": str(
            (analysis_results or {}).get("analysis_version") or "2.0"
        ),
        "based_on_plot_version": "available" if visualizations else "none",
    }
