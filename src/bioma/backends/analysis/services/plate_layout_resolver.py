from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd


_ROWS_96 = tuple("ABCDEFGH")
_COLS_96 = tuple(range(1, 13))
_NUM_1_TO_12 = r"(?:1[0-2]|[1-9])"
_WELL_RE = re.compile(rf"^([A-H])({_NUM_1_TO_12})$", re.IGNORECASE)
_A1_RANGE_RE = re.compile(
    rf"\b([A-H])\s*({_NUM_1_TO_12})\s*[-~–—至到]\s*([A-H])\s*({_NUM_1_TO_12})\b",
    re.IGNORECASE,
)
_12A_RANGE_RE = re.compile(
    rf"\b({_NUM_1_TO_12})\s*([A-H])\s*[-~–—至到]\s*({_NUM_1_TO_12})\s*([A-H])\b",
    re.IGNORECASE,
)
_A1_SINGLE_AFTER_LABEL_RE = re.compile(
    rf"(?:孔|well)\s*([A-H])\s*({_NUM_1_TO_12})\b", re.IGNORECASE
)
_12A_SINGLE_AFTER_LABEL_RE = re.compile(
    rf"(?:孔|well)\s*({_NUM_1_TO_12})\s*([A-H])\b", re.IGNORECASE
)
_COLUMN_RANGE_RE = re.compile(
    rf"(?:列|columns?|cols?)\s*({_NUM_1_TO_12})(?:\s*[-~–—至到]\s*({_NUM_1_TO_12}))?",
    re.IGNORECASE,
)


@dataclass
class LayoutResolveResult:
    dataframe: pd.DataFrame
    issues: list[dict[str, Any]]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if isinstance(item, (str, int, float)):
                parts.append(f"{key}: {item}")
            elif isinstance(item, list):
                parts.append(f"{key}: " + "\n".join(str(x) for x in item))
            elif isinstance(item, dict):
                parts.append(f"{key}: {_as_text(item)}")
        return "\n".join(parts)
    if isinstance(value, list):
        return "\n".join(_as_text(x) for x in value)
    return str(value)


def requirement_text_from_state(state: dict[str, Any]) -> str:
    parts = [
        state.get("current_user_request"),
        state.get("stable_requirement"),
        state.get("experiment_config"),
        state.get("experiment_background"),
        state.get("context_summary"),
    ]
    return "\n".join(_as_text(x) for x in parts if _as_text(x).strip())


def _well(row: str, column: int) -> str:
    return f"{row.upper()}{int(column)}"


def _normalize_well(raw: str) -> Optional[str]:
    match = _WELL_RE.match(str(raw or "").strip())
    if not match:
        return None
    return _well(match.group(1), int(match.group(2)))


def _expand_column_range(start: int, end: int) -> list[str]:
    lo, hi = sorted((int(start), int(end)))
    wells: list[str] = []
    for row in _ROWS_96:
        for col in range(lo, hi + 1):
            if col in _COLS_96:
                wells.append(_well(row, col))
    return wells


def _expand_well_range(row1: str, col1: int, row2: str, col2: int) -> list[str]:
    row1 = row1.upper()
    row2 = row2.upper()
    col1 = int(col1)
    col2 = int(col2)
    if col1 == col2:
        rows = _ROWS_96[_ROWS_96.index(row1) : _ROWS_96.index(row2) + 1]
        if _ROWS_96.index(row1) > _ROWS_96.index(row2):
            rows = _ROWS_96[_ROWS_96.index(row2) : _ROWS_96.index(row1) + 1]
        return [_well(row, col1) for row in rows]
    if row1 == row2:
        lo, hi = sorted((col1, col2))
        return [_well(row1, col) for col in range(lo, hi + 1)]
    return []


def _extract_wells_from_rule(rule: str) -> list[str]:
    wells: list[str] = []

    for match in _COLUMN_RANGE_RE.finditer(rule):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        wells.extend(_expand_column_range(start, end))

    for match in _A1_RANGE_RE.finditer(rule):
        wells.extend(
            _expand_well_range(
                match.group(1), int(match.group(2)), match.group(3), int(match.group(4))
            )
        )

    for match in _12A_RANGE_RE.finditer(rule):
        wells.extend(
            _expand_well_range(
                match.group(2), int(match.group(1)), match.group(4), int(match.group(3))
            )
        )

    # Single-well parsing is intentionally conservative: only use it when the
    # rule did not already contain ranges, so range endpoints are not duplicated.
    if not wells:
        for match in _A1_SINGLE_AFTER_LABEL_RE.finditer(rule):
            wells.append(_well(match.group(1), int(match.group(2))))
        for match in _12A_SINGLE_AFTER_LABEL_RE.finditer(rule):
            wells.append(_well(match.group(2), int(match.group(1))))

    out: list[str] = []
    seen = set()
    for well in wells:
        normalized = _normalize_well(well)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _normalize_group(rule: str) -> tuple[str, str]:
    text = str(rule or "")
    low = text.lower()

    # Lines such as "Top1-5: columns 1-10" are summaries, not a single group
    # assignment. Treating them as Top1 would overwrite the precise per-group
    # rules that usually appear elsewhere in the prompt.
    if re.search(r"\btop\s*[1-9]\d*\s*[-~–—至到]\s*[1-9]\d*\b", low):
        return "Unknown", "unknown"

    top_match = re.search(r"\btop\s*([1-9]\d*)\b", low)
    if top_match:
        return f"Top{int(top_match.group(1))}", "variant"

    seq_match = re.search(r"\br7\s*seq\s*([1-9]\d*)\b", low)
    if seq_match:
        return f"Top{int(seq_match.group(1))}", "variant"

    if (
        any(
            token in low
            for token in (
                "positive control",
                "positive_control",
                "positivecontrol",
                "r6",
                "baseline",
            )
        )
        or "阳性对照" in text
    ):
        return "Positive_Control", "positive_control"

    if "blank" in low or "空白" in text:
        return "Blank", "blank"

    return "Unknown", "unknown"


def _candidate_rule_lines(text: str) -> Iterable[str]:
    for raw in str(text or "").splitlines():
        line = raw.strip().strip("-*•> ")
        if not line:
            continue
        chunks = [part.strip() for part in re.split(r"[,;；，]", line) if part.strip()]
        for chunk in chunks or [line]:
            low = chunk.lower()
            has_label = any(
                token in low for token in ("列", "column", "col", "孔", "well")
            )
            has_well_range = bool(
                _A1_RANGE_RE.search(chunk) or _12A_RANGE_RE.search(chunk)
            )
            if has_label or has_well_range:
                yield chunk


def resolve_plate_layout_from_text(text: str) -> LayoutResolveResult:
    assignments: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []

    for line in _candidate_rule_lines(text):
        wells = _extract_wells_from_rule(line)
        if not wells:
            continue
        group, role = _normalize_group(line)
        if group == "Unknown":
            continue
        for well in wells:
            if well in assignments and _rule_specificity(line) < _rule_specificity(
                assignments[well].get("source_rule", "")
            ):
                continue
            assignments[well] = {
                "well": well,
                "row": re.match(r"([A-H])", well).group(1),
                "column": int(re.search(r"(1[0-2]|[1-9])$", well).group(1)),
                "group": group,
                "role": role,
                "source_rule": line,
            }

    if not assignments:
        return LayoutResolveResult(
            pd.DataFrame(),
            issues
            + [
                {
                    "code": "LAYOUT_RULE_NOT_FOUND",
                    "severity": "low",
                    "message": "No parseable 96-well layout rules were found in requirement text.",
                }
            ],
        )

    rows: list[dict[str, Any]] = []
    for row in _ROWS_96:
        for column in _COLS_96:
            well = _well(row, column)
            item = assignments.get(well) or {
                "well": well,
                "row": row,
                "column": column,
                "group": "Unknown",
                "role": "unknown",
                "source_rule": "",
            }
            rows.append(item)

    df = pd.DataFrame(
        rows, columns=["well", "row", "column", "group", "role", "source_rule"]
    )
    df["replicate_id"] = df.groupby("group").cumcount() + 1
    df = df[["well", "row", "column", "group", "role", "replicate_id", "source_rule"]]
    unresolved = int((df["role"] == "unknown").sum())
    if unresolved:
        issues.append(
            {
                "code": "LAYOUT_PARTIAL_ASSIGNMENT",
                "severity": "medium",
                "message": f"{unresolved} well(s) were not assigned by parsed layout rules.",
            }
        )
    return LayoutResolveResult(df, issues)


def _rule_specificity(rule: str) -> int:
    text = str(rule or "")
    score = 0
    if re.search(r"\btop\s*[1-9]\d*\b", text, re.IGNORECASE):
        score += 2
    if re.search(r"\br7\s*seq\s*[1-9]\d*\b", text, re.IGNORECASE):
        score += 2
    if _A1_RANGE_RE.search(text) or _12A_RANGE_RE.search(text):
        score += 2
    if _COLUMN_RANGE_RE.search(text):
        score += 1
    if re.search(r"\btop\s*[1-9]\d*\s*[-~–—至到]\s*[1-9]\d*\b", text, re.IGNORECASE):
        score -= 4
    return score


def find_existing_layout_csv(paths: list[str]) -> Optional[str]:
    for path in paths or []:
        try:
            df = pd.read_csv(path, nrows=5)
        except Exception:
            continue
        cols = {str(c).strip().lower() for c in df.columns}
        if "well" in cols and ({"group", "role"} & cols):
            return path
    return None


def load_existing_layout_csv(path: str) -> LayoutResolveResult:
    try:
        df = pd.read_csv(path, dtype=object)
    except Exception as exc:
        return LayoutResolveResult(
            pd.DataFrame(),
            [
                {
                    "code": "LAYOUT_CSV_READ_FAILED",
                    "severity": "medium",
                    "message": f"Could not read existing layout CSV: {path}: {exc!r}",
                }
            ],
        )

    columns = {str(c).strip().lower(): c for c in df.columns}
    if "well" not in columns:
        return LayoutResolveResult(
            pd.DataFrame(),
            [
                {
                    "code": "LAYOUT_CSV_MISSING_WELL",
                    "severity": "medium",
                    "message": f"Existing layout CSV lacks a well column: {path}",
                }
            ],
        )

    out = pd.DataFrame()
    out["well"] = df[columns["well"]].astype(str).str.strip().str.upper()
    out["row"] = out["well"].str.extract(r"^([A-H])", expand=False).fillna("")
    out["column"] = pd.to_numeric(
        out["well"].str.extract(r"(1[0-2]|[1-9])$", expand=False), errors="coerce"
    )

    group_col = columns.get("group")
    role_col = columns.get("role")
    if group_col:
        out["group"] = df[group_col].astype(str).str.strip()
    else:
        out["group"] = "Unknown"
    if role_col:
        out["role"] = df[role_col].astype(str).str.strip()
    else:
        out["role"] = out["group"].map(lambda x: _normalize_group(str(x))[1])

    out["group"] = out["group"].map(
        lambda x: _normalize_group(str(x))[0] if str(x).strip() else "Unknown"
    )
    out["role"] = out["role"].replace({"": "unknown"}).fillna("unknown")
    out["replicate_id"] = out.groupby("group").cumcount() + 1
    out["source_rule"] = f"existing_layout_csv:{path}"
    out = out[["well", "row", "column", "group", "role", "replicate_id", "source_rule"]]
    out = out[out["well"].map(lambda x: _normalize_well(str(x)) is not None)].copy()
    out["well"] = out["well"].map(lambda x: _normalize_well(str(x)))
    return LayoutResolveResult(out.reset_index(drop=True), [])


def merge_layout_into_tables(
    *,
    transformed_data_sources: list[str],
    layout_df: pd.DataFrame,
    output_dir: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    if layout_df is None or layout_df.empty:
        return transformed_data_sources, []

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    layout_path = out_dir / "plate_layout__table_01.csv"
    layout_df.to_csv(layout_path, index=False, encoding="utf-8-sig")

    new_paths: list[str] = []
    issues: list[dict[str, Any]] = []
    layout_cols = ["well", "group", "role", "replicate_id"]
    merge_df = layout_df[layout_cols].copy()

    for src in transformed_data_sources or []:
        try:
            df = pd.read_csv(src, dtype=object)
        except Exception as exc:
            issues.append(
                {
                    "code": "LAYOUT_ENRICH_READ_FAILED",
                    "severity": "medium",
                    "message": f"Could not read table for layout enrichment: {src}: {exc!r}",
                }
            )
            new_paths.append(src)
            continue

        lower_cols = {str(c).strip().lower() for c in df.columns}
        if "well" not in lower_cols:
            new_paths.append(src)
            continue
        if (
            "group" in lower_cols
            and "role" in lower_cols
            and "temperature" not in lower_cols
        ):
            new_paths.append(src)
            continue

        work = df.copy()
        work["well"] = work["well"].astype(str).str.strip().str.upper()
        drop_cols = [c for c in layout_cols if c != "well" and c in work.columns]
        if drop_cols:
            work = work.drop(columns=drop_cols)
        enriched = work.merge(merge_df, on="well", how="left")
        output_path = out_dir / f"{Path(src).stem}__with_layout.csv"
        enriched.to_csv(output_path, index=False, encoding="utf-8-sig")
        new_paths.append(str(output_path))

    if str(layout_path) not in new_paths:
        new_paths.append(str(layout_path))
    return new_paths, issues


__all__ = [
    "LayoutResolveResult",
    "find_existing_layout_csv",
    "load_existing_layout_csv",
    "merge_layout_into_tables",
    "requirement_text_from_state",
    "resolve_plate_layout_from_text",
]
