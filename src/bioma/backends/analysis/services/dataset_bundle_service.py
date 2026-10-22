"""
dataset_bundle_service — structural facts extractor (v2.0).

This module builds a `dataset_bundle` that captures *structural facts* about
the input tables (shape, column profile, axis role hints, inter-table
relations) and nothing else.  It does NOT attempt to classify the experiment
type, choose analysis steps, or pick chart templates — those decisions belong
to the downstream LLM-driven `plan_analysis` stage.

Design choices:
  * No instrument-family routing.  The same pipeline handles qpcr,
    plate_reader, capillary_electrophoresis, and unknown.  Family is carried
    through as `instrument_family_hint` metadata only.
  * Axis role hints use word-boundary token matching against the normalised
    column name, not loose substring checks.  This removes the whole class of
    false positives that previously mis-bound e.g. ``sample_num`` to
    ``concentration`` via the "um" substring.
  * Hints are intentionally conservative and open-vocabulary — a column may
    have zero hints.  Downstream is expected to look at ``sample_values`` +
    the column name to disambiguate the rest.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from bioma.backends.analysis.services.json_data import read_json_view


# =========================================================================
# Name normalisation + tokenisation
# =========================================================================


def _norm(s: Any) -> str:
    """Lowercase, collapse non-alphanumerics to underscores, strip."""
    s = str(s or "").strip().lower()
    out: List[str] = []
    prev_us = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        else:
            if not prev_us:
                out.append("_")
                prev_us = True
    result = "".join(out).strip("_")
    while "__" in result:
        result = result.replace("__", "_")
    return result


def _tokens(name: str) -> set:
    return {t for t in _norm(name).split("_") if t}


# =========================================================================
# I/O
# =========================================================================


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_csv_any(path: str) -> pd.DataFrame:
    last_err: Optional[Exception] = None
    for enc in ["utf-8-sig", "utf-8", "utf-16", "latin-1", "iso-8859-1"]:
        try:
            return pd.read_csv(path, dtype=object, encoding=enc)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"READ_TABLE_FAILED: {path}: {repr(last_err)}")


# =========================================================================
# Column profile
# =========================================================================


def _to_numeric_ratio(series: pd.Series) -> float:
    try:
        s = series.astype(str).str.strip()
        s = s.replace({"": None, "nan": None, "None": None, "<NA>": None})
        converted = pd.to_numeric(s, errors="coerce")
        valid = (~converted.isna()).sum()
        total = max(1, len(series))
        return float(valid) / float(total)
    except Exception:
        return 0.0


def _numeric_range(series: pd.Series) -> Optional[Tuple[float, float]]:
    try:
        nums = pd.to_numeric(series, errors="coerce").dropna()
        if nums.empty:
            return None
        return float(nums.min()), float(nums.max())
    except Exception:
        return None


def _sample_values(series: pd.Series, k: int = 5) -> List[str]:
    out: List[str] = []
    try:
        for x in series.astype(str).fillna("").tolist():
            v = str(x).strip()
            if not v or v in out:
                continue
            out.append(v)
            if len(out) >= k:
                break
    except Exception:
        pass
    return out


def _column_profile(col_name: str, series: pd.Series) -> Dict[str, Any]:
    numeric_ratio = _to_numeric_ratio(series)
    is_numeric = numeric_ratio >= 0.8
    rng = _numeric_range(series) if is_numeric else None
    non_null_ratio = round(float((~series.isna()).sum()) / max(1, len(series)), 4)
    return {
        "name": str(col_name),
        "name_norm": _norm(col_name),
        "dtype_hint": "numeric" if is_numeric else "string",
        "numeric_ratio": round(numeric_ratio, 4),
        "non_null_ratio": non_null_ratio,
        "n_unique": int(series.astype(str).nunique(dropna=True)),
        "min_numeric": None if rng is None else rng[0],
        "max_numeric": None if rng is None else rng[1],
        "sample_values": _sample_values(series),
    }


# =========================================================================
# Axis role hints  (word-boundary, open-vocabulary, family-agnostic)
# =========================================================================

_WELL_ID_RE = re.compile(r"^[A-P]\d{1,2}$", re.IGNORECASE)


def _is_well_value_series(samples: List[str]) -> bool:
    if not samples:
        return False
    hits = sum(1 for s in samples if _WELL_ID_RE.match(str(s).strip()))
    return hits >= max(2, int(0.6 * len(samples)))


# (hint_name, matching_token_set, require_numeric)
_AXIS_HINT_RULES: List[Tuple[str, set, bool]] = [
    ("temperature_axis", {"temp", "temperature"}, True),
    ("cycle_axis", {"cycle", "cycles"}, True),
    (
        "time_axis",
        {"time", "sec", "seconds", "min", "minutes", "hour", "hours", "timepoint"},
        True,
    ),
    ("sample_id", {"sample"}, False),
    ("group_id", {"group", "condition", "treatment", "class"}, False),
    ("target_id", {"target", "gene", "marker", "assay"}, False),
    ("replicate_id", {"replicate", "rep", "repeat"}, False),
    ("concentration_axis", {"conc", "concentration", "dose", "standard"}, True),
    ("size_bp_axis", {"bp", "size", "fragment"}, True),
    ("fluorescence_signal", {"fluorescence", "fluor", "rfu"}, True),
    ("intensity_signal", {"intensity"}, True),
    ("ct_signal", {"ct", "cq", "cp"}, True),
    ("absorbance_signal", {"od", "abs", "absorbance"}, True),
    ("generic_signal", {"value", "readout", "signal"}, True),
    ("peak_area_signal", {"area"}, True),
    ("peak_height_signal", {"height"}, True),
    ("ratio_signal", {"ratio"}, True),
    ("migration_time_axis", {"migration"}, True),
]


def _axis_role_hints(
    col_name: str, series: pd.Series, profile: Dict[str, Any]
) -> List[str]:
    toks = _tokens(col_name)
    is_numeric = profile["dtype_hint"] == "numeric"
    samples = profile.get("sample_values") or []
    hints: List[str] = []

    for hint, required_tokens, require_numeric in _AXIS_HINT_RULES:
        if not (toks & required_tokens):
            continue
        if require_numeric and not is_numeric:
            continue
        hints.append(hint)

    well_by_name = bool(toks & {"well", "wellid", "position"})
    well_by_values = _is_well_value_series(samples)
    if well_by_values:
        hints.append("well_id")
    elif well_by_name:
        hints.append("well_id_candidate")

    # Single-letter "t" / "tm" column: numeric + physically plausible range.
    if (
        _norm(col_name) in {"t", "tm"}
        and is_numeric
        and "temperature_axis" not in hints
    ):
        lo, hi = profile.get("min_numeric"), profile.get("max_numeric")
        if lo is not None and hi is not None and 0.0 <= lo and hi <= 150.0:
            hints.append("temperature_axis")

    return hints


# =========================================================================
# Layout detection
# =========================================================================


def _looks_matrix(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False
    if df.shape[0] < 4 or df.shape[1] < 4:
        return False

    cols_norm = [_norm(c) for c in df.columns.tolist()]

    numeric_header_like = sum(1 for c in cols_norm[1:] if c.isdigit())
    numeric_header_ratio = numeric_header_like / max(1, len(cols_norm) - 1)

    first_col = df.iloc[:, 0].astype(str).str.strip().fillna("")
    row_label_sample = [x for x in first_col.head(16).tolist() if x]
    row_label_hits = sum(
        1
        for x in row_label_sample
        if len(x) <= 2 and x.upper()[:1] in "ABCDEFGHIJKLMNOP"
    )
    row_label_ratio = row_label_hits / max(1, len(row_label_sample))

    numeric_body_cols = sum(
        1 for c in df.columns.tolist()[1:] if _to_numeric_ratio(df[c]) >= 0.7
    )
    numeric_body_ratio = numeric_body_cols / max(1, len(df.columns) - 1)

    return (
        numeric_header_ratio >= 0.4
        and row_label_ratio >= 0.4
        and numeric_body_ratio >= 0.5
    )


def _infer_layout_type(df: pd.DataFrame, column_profiles: List[Dict[str, Any]]) -> str:
    if _looks_matrix(df):
        return "matrix"

    all_hints: set = set()
    for p in column_profiles:
        all_hints.update(p.get("axis_role_hints") or [])

    axis_like = {
        "temperature_axis",
        "cycle_axis",
        "time_axis",
        "concentration_axis",
        "size_bp_axis",
        "migration_time_axis",
    }
    if all_hints & axis_like:
        return "long"

    numeric_cols = sum(1 for p in column_profiles if p["dtype_hint"] == "numeric")
    if numeric_cols >= max(2, len(column_profiles) // 2):
        return "wide"
    return "unknown"


# =========================================================================
# Relation detection  (shared-column based, family-independent)
# =========================================================================


def _shared_columns(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, Any]]:
    a_cols = {c["name_norm"]: c for c in (a.get("columns") or [])}
    b_cols = {c["name_norm"]: c for c in (b.get("columns") or [])}
    shared: List[Dict[str, Any]] = []
    for n in sorted(set(a_cols) & set(b_cols)):
        ca, cb = a_cols[n], b_cols[n]
        shared.append(
            {
                "column_norm": n,
                "a_column": ca["name"],
                "b_column": cb["name"],
                "a_n_unique": ca["n_unique"],
                "b_n_unique": cb["n_unique"],
                "a_axis_role_hints": ca.get("axis_role_hints") or [],
                "b_axis_role_hints": cb.get("axis_role_hints") or [],
            }
        )
    return shared


def _infer_relations(tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rels: List[Dict[str, Any]] = []
    rel_id = 1
    for i in range(len(tables)):
        for j in range(i + 1, len(tables)):
            shared = _shared_columns(tables[i], tables[j])
            if not shared:
                continue
            conf = round(min(0.92, 0.45 + 0.08 * len(shared)), 4)
            rels.append(
                {
                    "relation_id": f"r{rel_id}",
                    "from_table_id": tables[i]["table_id"],
                    "to_table_id": tables[j]["table_id"],
                    "shared_columns": shared,
                    "confidence": conf,
                }
            )
            rel_id += 1
    return rels


# =========================================================================
# Global entities
# =========================================================================


def _global_entities(tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    entity_map = {
        "wells": {"well_id", "well_id_candidate"},
        "samples": {"sample_id"},
        "groups": {"group_id"},
        "targets": {"target_id"},
    }
    out: Dict[str, Any] = {k: {"n_tables": 0, "columns": []} for k in entity_map}

    for t in tables:
        for out_key, hint_set in entity_map.items():
            for c in t.get("columns") or []:
                if set(c.get("axis_role_hints") or []) & hint_set:
                    out[out_key]["n_tables"] += 1
                    if c["name"] not in out[out_key]["columns"]:
                        out[out_key]["columns"].append(c["name"])
                    break
    return out


# =========================================================================
# Preview
# =========================================================================


def _preview(
    df: pd.DataFrame, max_rows: int = 12, max_cols: int = 12, max_chars: int = 4000
) -> Dict[str, Any]:
    head = df.head(max_rows).copy()
    if head.shape[1] > max_cols:
        head = head.iloc[:, :max_cols]
    cols = [str(c) for c in head.columns.tolist()]
    lines = ["COLUMNS: " + ", ".join(cols)]
    for _, row in head.iterrows():
        lines.append("\t".join(str(v)[:80] for v in row.tolist()))
    text = "\n".join(lines)[:max_chars]
    return {"type": "table", "text": text}


# =========================================================================
# Table builder
# =========================================================================


def _artifact_table_map(data_artifact: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in (data_artifact or {}).get("tables", []) or []:
        p = str(t.get("path") or "")
        if p:
            out[p] = t
    return out


def _source_ref(path: str, artifact_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    a = artifact_map.get(path) or {}
    return {
        "source_file": a.get("source_file"),
        "source_sheet": a.get("source_sheet"),
        "source_block_id": a.get("source_block_id"),
        "extraction_method": a.get("extraction_method"),
        **({"json_source": a["json_source"]} if "json_source" in a else {}),
    }


def _build_table(
    path: str,
    idx: int,
    artifact_map: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    issues: List[Dict[str, Any]] = []

    source_ref = _source_ref(path, artifact_map)
    json_source = source_ref.get("json_source")
    try:
        df = read_json_view(json_source) if json_source else _read_csv_any(path)
    except Exception as e:
        return None, [
            {
                "code": "READ_TABLE_FAILED",
                "severity": "high",
                "message": f"{path}: {repr(e)}",
            }
        ]

    if not json_source:
        df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if df.empty:
        return None, [
            {
                "code": "EMPTY_SUBTABLE",
                "severity": "medium",
                "message": f"{path}: empty after dropna.",
            }
        ]

    column_profiles: List[Dict[str, Any]] = []
    for col in df.columns.tolist():
        prof = _column_profile(col, df[col])
        prof["axis_role_hints"] = _axis_role_hints(col, df[col], prof)
        column_profiles.append(prof)

    table = {
        "table_id": f"t{idx + 1}",
        "path": path,
        "source_ref": source_ref,
        "shape": {"rows": int(df.shape[0]), "cols": int(df.shape[1])},
        "layout_type": _infer_layout_type(df, column_profiles),
        "columns": column_profiles,
        "preview": _preview(df),
        "issues": [],
    }
    return table, issues


# =========================================================================
# Entry point
# =========================================================================


def build_dataset_bundle(
    instrument_family: str,
    transformed_data_sources: List[str],
    data_artifact: Optional[Dict[str, Any]] = None,
    data_prep_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a structural dataset_bundle from pre-processed sub-tables.

    Called by nodes.parsing.parse_node; emits the current v2.0 bundle schema.

    Returns
    -------
    dict with keys:
        dataset_bundle      (dict)     — v2.0 schema or {} on fatal failure
        bundle_issues       (list)     — all issues gathered during parse
        need_clarification  (bool)     — always False in v2 (LLM decides later)
        question_to_user    (str)      — always "" in v2
        fatal               (bool)     — True when no usable table was produced
    """
    issues: List[Dict[str, Any]] = []
    paths = [str(p) for p in (transformed_data_sources or []) if str(p).strip()]

    if not paths:
        return {
            "dataset_bundle": {},
            "bundle_issues": [
                {
                    "code": "NO_TRANSFORMED_DATA_SOURCES",
                    "severity": "high",
                    "message": "No transformed_data_sources were provided to parse node.",
                }
            ],
            "need_clarification": False,
            "question_to_user": "",
            "fatal": True,
        }

    artifact_map = _artifact_table_map(data_artifact or {})
    tables: List[Dict[str, Any]] = []

    for idx, p in enumerate(paths):
        t, table_issues = _build_table(p, idx, artifact_map)
        issues.extend(table_issues)
        if t:
            tables.append(t)

    if not tables:
        return {
            "dataset_bundle": {},
            "bundle_issues": issues
            + [
                {
                    "code": "NO_USABLE_TABLES_AFTER_PARSE",
                    "severity": "high",
                    "message": "No usable tables could be profiled from transformed_data_sources.",
                }
            ],
            "need_clarification": False,
            "question_to_user": "",
            "fatal": True,
        }

    relations = _infer_relations(tables)
    global_entities = _global_entities(tables)
    family_hint = str(instrument_family or "unknown").strip() or "unknown"

    source_files = sorted(
        {
            str((t.get("source_ref") or {}).get("source_file") or "")
            for t in tables
            if (t.get("source_ref") or {}).get("source_file")
        }
    )

    summary = (
        f"Dataset bundle: {len(tables)} table(s), "
        f"layout_types={sorted({t['layout_type'] for t in tables})}, "
        f"relations={len(relations)}, family_hint={family_hint}."
    )

    bundle = {
        "bundle_version": "2.0",
        "generated_at": _utc_now_iso(),
        "instrument_family_hint": family_hint,
        "source_files": source_files,
        "data_artifact_ref": {
            "artifact_version": (data_artifact or {}).get("artifact_version")
        },
        "data_prep_meta": data_prep_meta or {},
        "tables": tables,
        "relations": relations,
        "global_entities": global_entities,
        "bundle_summary": summary,
        "issues": issues,
    }

    return {
        "dataset_bundle": bundle,
        "bundle_issues": issues,
        "need_clarification": False,
        "question_to_user": "",
        "fatal": False,
    }


__all__ = ["build_dataset_bundle"]
