from __future__ import annotations

import csv
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from bioma.backends.analysis.services.lightcycler_parser import parse_lightcycler_txt


SUPPORTED_EXTS = {
    ".json",
    ".csv",
    ".tsv",
    ".txt",
    ".xlsx",
    ".xls",
}


@dataclass
class PrepResult:
    transformed_paths: List[str]
    artifact_tables: List[Dict[str, Any]]
    prep_meta: Dict[str, Any]
    table_count: int
    issues: List[Dict[str, Any]]


def validate_input_sources(
    raw_data_sources: List[str],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    valid: List[str] = []
    issues: List[Dict[str, Any]] = []

    for src in raw_data_sources or []:
        p = Path(str(src))
        if not p.exists():
            issues.append(
                {
                    "code": "INPUT_NOT_FOUND",
                    "severity": "high",
                    "message": f"Input file does not exist: {src}",
                }
            )
            continue

        if p.suffix.lower() not in SUPPORTED_EXTS:
            issues.append(
                {
                    "code": "UNSUPPORTED_INPUT_TYPE",
                    "severity": "high",
                    "message": f"Unsupported input type: {src}",
                }
            )
            continue

        valid.append(str(p))

    return valid, issues


def _dedupe_names(names: Iterable[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for raw in names:
        base = str(raw or "").strip() or "column"
        idx = seen.get(base, 0)
        seen[base] = idx + 1
        out.append(base if idx == 0 else f"{base}_{idx + 1}")
    return out


def _normalize_table(df: pd.DataFrame) -> pd.DataFrame:
    clean = pd.DataFrame(df).copy()
    clean = clean.dropna(axis=0, how="all").dropna(axis=1, how="all")
    clean = clean.fillna("")
    if isinstance(clean.columns, pd.MultiIndex):
        clean.columns = [
            "__".join(str(part).strip() for part in item if str(part).strip())
            for item in clean.columns.to_flat_index()
        ]
    clean.columns = _dedupe_names([str(col).strip() for col in clean.columns.tolist()])
    return clean.reset_index(drop=True)


def _looks_like_real_table(df_table: pd.DataFrame) -> bool:
    if df_table is None or df_table.empty:
        return False

    rows, cols = df_table.shape
    if rows < 2 or cols < 2:
        return False

    cols_list = [str(c) for c in df_table.columns.tolist()]
    unnamed_ratio = sum(
        (c.startswith("Unnamed") or c.strip() == "") for c in cols_list
    ) / max(1, len(cols_list))
    if unnamed_ratio > 0.85:
        return False

    head = df_table.head(min(20, len(df_table)))
    try:
        if head.replace("", pd.NA).isna().mean().mean() > 0.97:
            return False
    except Exception:
        pass

    try:
        row_non_blank = head.astype(str).apply(
            lambda row: sum(1 for x in row if str(x).strip()), axis=1
        )
        if len(row_non_blank) > 0 and row_non_blank.mean() < max(2, cols * 0.3):
            return False
    except Exception:
        pass

    return True


def _table_signature(df: pd.DataFrame, max_rows: int = 5) -> str:
    try:
        head = df.head(max_rows).astype(str)
        cols = list(head.columns)
        rows = [tuple(r) for r in head.values.tolist()]
        return repr((tuple(cols), tuple(rows)))
    except Exception:
        return repr(df.shape)


def _dedup_tables(tables: List[pd.DataFrame]) -> List[pd.DataFrame]:
    out: List[pd.DataFrame] = []
    seen = set()
    for df in tables:
        if df is None or df.empty:
            continue
        normalized = _normalize_table(df)
        if not _looks_like_real_table(normalized):
            continue
        sig = _table_signature(normalized)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(normalized)
    return out


def _read_text_preview(path: str, max_lines: int = 20, max_chars: int = 4000) -> str:
    lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if i >= max_lines:
                    break
                lines.append(", ".join([str(x) for x in row]))
    except Exception:
        return ""
    return "\n".join(lines)[:max_chars]


def _preview_from_df(
    df: pd.DataFrame, max_rows: int = 20, max_cols: int = 12, max_chars: int = 4000
) -> str:
    head = df.head(max_rows).copy()
    if head.shape[1] > max_cols:
        head = head.iloc[:, :max_cols]

    lines = [", ".join([str(col) for col in head.columns.tolist()])]
    for _, row in head.iterrows():
        lines.append(", ".join([str(x) for x in row.tolist()]))
    return "\n".join(lines)[:max_chars]


def _infer_basic_shape_from_csv(path: str) -> Dict[str, int]:
    rows = 0
    cols = 0
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                rows += 1
                cols = max(cols, len(row))
    except Exception:
        pass
    return {"rows": rows, "cols": cols}


def _sniff_file_kind(path: Path) -> str:
    try:
        head = path.read_bytes()[:1024]
    except Exception:
        return "unknown"

    if zipfile.is_zipfile(path):
        return "xlsx_zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls_ole"
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return "text_export"

    sample = head.decode("latin-1", errors="ignore")
    if ("\t" in sample and "\x00" in sample) or sample.startswith("##BLOCKS"):
        return "text_export"

    return "unknown"


def _read_text_lines_best_effort(path: Path) -> Tuple[pd.DataFrame, str]:
    last_err: Optional[Exception] = None
    for enc in ["utf-8-sig", "utf-8", "utf-16", "latin-1", "iso-8859-1"]:
        try:
            with open(path, "r", encoding=enc) as f:
                lines = [ln.rstrip("\n\r") for ln in f]
            while lines and lines[-1].strip() == "":
                lines.pop()
            return pd.DataFrame({"raw_line": lines}), enc
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"TEXT_READ_FAILED: {path}: {repr(last_err)}")


def _parse_delimited_line(line: str, delimiter: str) -> List[str]:
    if delimiter == "\t":
        return [x.strip() for x in line.split("\t")]
    return [str(x).strip() for x in next(csv.reader([line], delimiter=delimiter))]


def _dedup_column_names(cols: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for col in cols:
        name = str(col).strip() or "col"
        if name not in seen:
            seen[name] = 0
            out.append(name)
        else:
            seen[name] += 1
            out.append(f"{name}_{seen[name]}")
    return out


def _rows_to_df_with_header(rows: List[List[str]]) -> Optional[pd.DataFrame]:
    if not rows or len(rows) < 2:
        return None

    max_len = max(len(r) for r in rows)
    if max_len < 2:
        return None

    norm = [r + [""] * (max_len - len(r)) for r in rows]
    header = [str(x).strip() for x in norm[0]]
    body = norm[1:]

    nonempty_header = sum(1 for x in header if x != "")
    if nonempty_header < max(1, max_len // 3):
        header = [f"col_{i + 1}" for i in range(max_len)]

    header = _dedup_column_names(header)
    df = pd.DataFrame(body, columns=header)
    df = df.replace("", pd.NA).dropna(axis=1, how="all").fillna("")

    if df.empty or len(df.columns) < 2:
        return None
    return df


def _extract_all_table_blocks_from_raw_lines(
    df_raw_lines: pd.DataFrame,
) -> List[pd.DataFrame]:
    if (
        df_raw_lines is None
        or df_raw_lines.empty
        or "raw_line" not in df_raw_lines.columns
    ):
        return []

    raw_lines = [str(x) for x in df_raw_lines["raw_line"].tolist()]
    delimiter_candidates = ["\t", ",", ";"]
    found_tables: List[pd.DataFrame] = []

    for delim in delimiter_candidates:
        parsed: List[Tuple[int, Optional[List[str]]]] = []
        for idx, line in enumerate(raw_lines):
            stripped = line.strip()
            if not stripped:
                parsed.append((idx, None))
                continue
            if delim not in line:
                parsed.append((idx, None))
                continue

            cells = _parse_delimited_line(line, delim)
            nonempty = sum(1 for c in cells if str(c).strip() != "")
            if nonempty < 2:
                parsed.append((idx, None))
                continue

            parsed.append((idx, cells))

        i = 0
        n = len(parsed)
        while i < n:
            if parsed[i][1] is None:
                i += 1
                continue

            block: List[List[str]] = []
            gap = 0
            j = i
            while j < n:
                row = parsed[j][1]
                if row is None:
                    gap += 1
                    if gap > 1:
                        break
                else:
                    gap = 0
                    block.append(row)
                j += 1

            i = j

            if len(block) < 3:
                continue

            lengths = [len(r) for r in block]
            max_len = max(lengths)
            min_len = min(lengths)
            if max_len < 2:
                continue
            if min_len < max(2, int(max_len * 0.5)):
                continue

            df = _rows_to_df_with_header(block)
            if df is None:
                continue
            found_tables.append(df)

    return _dedup_tables(found_tables)


def _sheet_df_to_tables(df_sheet: pd.DataFrame) -> List[pd.DataFrame]:
    if df_sheet is None or df_sheet.empty:
        return []

    work = df_sheet.copy().fillna("")
    nonblank_row_mask = work.astype(str).apply(
        lambda row: any(str(x).strip() != "" for x in row), axis=1
    )
    if nonblank_row_mask.sum() == 0:
        return []

    tables: List[pd.DataFrame] = []
    i = 0
    n = len(work)

    while i < n:
        row = work.iloc[i]
        if not any(str(x).strip() != "" for x in row.tolist()):
            i += 1
            continue

        block_rows: List[List[str]] = []
        gap = 0
        j = i
        while j < n:
            rowj = work.iloc[j]
            is_blank = not any(str(x).strip() != "" for x in rowj.tolist())
            if is_blank:
                gap += 1
                if gap > 1:
                    break
            else:
                gap = 0
                block_rows.append([str(x).strip() for x in rowj.tolist()])
            j += 1

        i = j

        if len(block_rows) < 2:
            continue

        df = _rows_to_df_with_header(block_rows)
        if df is None:
            continue
        tables.append(df)

    return _dedup_tables(tables)


def _read_excel_tables(path: Path) -> Tuple[List[Tuple[str, pd.DataFrame]], List[str]]:
    sniff_kind = _sniff_file_kind(path)
    if sniff_kind == "xlsx_zip" or path.suffix.lower() == ".xlsx":
        engines = ["openpyxl", None]
    else:
        engines = ["xlrd", None]

    last_error: Optional[Exception] = None
    workbook = None
    engine_used = None

    for engine in engines:
        try:
            workbook = (
                pd.ExcelFile(path, engine=engine) if engine else pd.ExcelFile(path)
            )
            engine_used = engine or "auto"
            break
        except Exception as exc:
            last_error = exc

    if workbook is None:
        raise last_error or RuntimeError(f"Failed to open Excel workbook: {path}")

    extracted: List[Tuple[str, pd.DataFrame]] = []
    for sheet_name in workbook.sheet_names:
        df_sheet_noheader = pd.read_excel(
            path,
            sheet_name=sheet_name,
            header=None,
            dtype=str,
            engine=None if engine_used == "auto" else engine_used,
        )
        tables = _sheet_df_to_tables(df_sheet_noheader)
        if not tables:
            df_sheet = pd.read_excel(
                path,
                sheet_name=sheet_name,
                dtype=str,
                engine=None if engine_used == "auto" else engine_used,
            )
            normalized = _normalize_table(df_sheet)
            if _looks_like_real_table(normalized):
                tables = [normalized]

        for idx, table in enumerate(tables, start=1):
            label = (
                str(sheet_name)
                if len(tables) == 1
                else f"{sheet_name}__block_{idx:02d}"
            )
            extracted.append((label, table))

    return extracted, [f"excel_multi_sheet({engine_used})"]


def _copy_delimited_source(source_path: str, out_path: Path, suffix: str) -> None:
    if suffix == ".csv":
        with (
            open(source_path, "r", encoding="utf-8", errors="ignore") as fr,
            open(out_path, "w", encoding="utf-8", newline="") as fw,
        ):
            fw.write(fr.read())
        return

    if suffix == ".tsv":
        with (
            open(source_path, "r", encoding="utf-8", errors="ignore", newline="") as fr,
            open(out_path, "w", encoding="utf-8", newline="") as fw,
        ):
            reader = csv.reader(fr, delimiter="\t")
            writer = csv.writer(fw)
            for row in reader:
                writer.writerow(row)
        return

    with (
        open(source_path, "r", encoding="utf-8", errors="ignore") as fr,
        open(out_path, "w", encoding="utf-8", newline="") as fw,
    ):
        for line in fr:
            fw.write(line)


def _artifact_entry(
    *,
    table_id: str,
    path: Path,
    source_file: str,
    source_sheet: Optional[str],
    source_block_id: Optional[str],
    extraction_method: str,
    preview_text: str,
) -> Dict[str, Any]:
    return {
        "table_id": table_id,
        "path": str(path),
        "source_file": source_file,
        "source_sheet": source_sheet,
        "source_block_id": source_block_id,
        "extraction_method": extraction_method,
        "shape": _infer_basic_shape_from_csv(str(path)),
        "preview": {
            "type": "table",
            "text": preview_text,
        },
    }


def prepare_single_source(
    source_path: str,
    output_dir: str,
) -> PrepResult:
    p = Path(source_path)
    suffix = p.suffix.lower()

    transformed_paths: List[str] = []
    artifact_tables: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []

    if suffix == ".json":
        from bioma.backends.analysis.services.json_data import prepare_json

        try:
            tables, metadata = prepare_json(p, Path(output_dir))
            return PrepResult([t["path"] for t in tables], tables, metadata, len(tables), [])
        except (ValueError, OSError) as exc:
            issue = {"code": "JSON_READ_FAILED", "severity": "high",
                     "message": f"Cannot read JSON input {source_path}: {exc}"}
            return PrepResult([], [], {"source_file": source_path}, 0, [issue])

    source_stem = p.stem
    source_out_dir = Path(output_dir) / source_stem
    source_out_dir.mkdir(parents=True, exist_ok=True)

    prep_meta: Dict[str, Any] = {
        "source_file": source_path,
        "output_dir": str(source_out_dir),
        "source_suffix": suffix,
        "sniff_kind": _sniff_file_kind(p),
        "read_strategies": [],
        "encoding": None,
        "table_count": 0,
    }

    if suffix == ".txt":
        lightcycler_result = parse_lightcycler_txt(p)
        if lightcycler_result is not None:
            out_path = source_out_dir / f"{source_stem}__lightcycler_long.csv"
            lightcycler_result.dataframe.to_csv(
                out_path, index=False, header=True, encoding="utf-8-sig"
            )
            prep_meta["read_strategies"].append("lightcycler_txt_long")
            prep_meta["encoding"] = lightcycler_result.encoding
            prep_meta["lightcycler"] = {
                "value_column": lightcycler_result.value_column,
                "well_count": lightcycler_result.well_count,
                "row_count": lightcycler_result.row_count,
            }

            transformed_paths.append(str(out_path))
            artifact_tables.append(
                _artifact_entry(
                    table_id=f"{source_stem}__lightcycler_long",
                    path=out_path,
                    source_file=source_path,
                    source_sheet=None,
                    source_block_id=None,
                    extraction_method="lightcycler_txt_long",
                    preview_text=_preview_from_df(lightcycler_result.dataframe),
                )
            )
        else:
            out_path = source_out_dir / f"{source_stem}__table_01.csv"
            _copy_delimited_source(source_path, out_path, suffix)
            prep_meta["read_strategies"].append(f"delimited_copy({suffix})")

            transformed_paths.append(str(out_path))
            artifact_tables.append(
                _artifact_entry(
                    table_id=f"{source_stem}__table_01",
                    path=out_path,
                    source_file=source_path,
                    source_sheet=None,
                    source_block_id=None,
                    extraction_method="delimited_copy",
                    preview_text=_read_text_preview(str(out_path)),
                )
            )

    elif suffix in {".csv", ".tsv"}:
        out_path = source_out_dir / f"{source_stem}__table_01.csv"
        _copy_delimited_source(source_path, out_path, suffix)
        prep_meta["read_strategies"].append(f"delimited_copy({suffix})")

        transformed_paths.append(str(out_path))
        artifact_tables.append(
            _artifact_entry(
                table_id=f"{source_stem}__table_01",
                path=out_path,
                source_file=source_path,
                source_sheet=None,
                source_block_id=None,
                extraction_method="delimited_copy",
                preview_text=_read_text_preview(str(out_path)),
            )
        )

    elif suffix in {".xlsx", ".xls"}:
        extracted_tables: List[Tuple[str, pd.DataFrame]] = []

        if prep_meta["sniff_kind"] == "text_export":
            df_raw_lines, encoding = _read_text_lines_best_effort(p)
            prep_meta["encoding"] = encoding
            prep_meta["read_strategies"].append(f"text_export_lines({encoding})")
            extracted_tables = [
                (f"block_{idx:02d}", table)
                for idx, table in enumerate(
                    _extract_all_table_blocks_from_raw_lines(df_raw_lines), start=1
                )
            ]
            extraction_method = "text_export"
        else:
            try:
                extracted_tables, strategies = _read_excel_tables(p)
                prep_meta["read_strategies"].extend(strategies)
                extraction_method = "excel_sheet"
            except Exception as exc:
                try:
                    df_raw_lines, encoding = _read_text_lines_best_effort(p)
                    prep_meta["encoding"] = encoding
                    prep_meta["read_strategies"].append(
                        f"text_export_fallback({encoding})"
                    )
                    extracted_tables = [
                        (f"block_{idx:02d}", table)
                        for idx, table in enumerate(
                            _extract_all_table_blocks_from_raw_lines(df_raw_lines),
                            start=1,
                        )
                    ]
                    extraction_method = "text_export"
                except Exception:
                    issues.append(
                        {
                            "code": "SPREADSHEET_READ_FAILED",
                            "severity": "high",
                            "message": f"Failed to read spreadsheet-like file: {source_path}",
                            "detail": repr(exc),
                        }
                    )
                    extracted_tables = []
                    extraction_method = "spreadsheet"

        for idx, (label, df_table) in enumerate(extracted_tables, start=1):
            out_path = source_out_dir / f"{source_stem}__table_{idx:02d}.csv"
            normalized = _normalize_table(df_table)
            normalized.to_csv(out_path, index=False, header=True, encoding="utf-8-sig")

            transformed_paths.append(str(out_path))
            artifact_tables.append(
                _artifact_entry(
                    table_id=f"{source_stem}__table_{idx:02d}",
                    path=out_path,
                    source_file=source_path,
                    source_sheet=label if extraction_method == "excel_sheet" else None,
                    source_block_id=label
                    if extraction_method == "text_export"
                    else None,
                    extraction_method=extraction_method,
                    preview_text=_preview_from_df(normalized),
                )
            )

        if not transformed_paths and not issues:
            issues.append(
                {
                    "code": "NO_USABLE_SPREADSHEET_TABLES",
                    "severity": "high",
                    "message": f"No usable subtables were extracted from: {source_path}",
                }
            )

    else:
        issues.append(
            {
                "code": "UNSUPPORTED_INPUT_TYPE",
                "severity": "high",
                "message": f"Unsupported input type: {source_path}",
            }
        )

    prep_meta["table_count"] = len(transformed_paths)

    return PrepResult(
        transformed_paths=transformed_paths,
        artifact_tables=artifact_tables,
        prep_meta=prep_meta,
        table_count=len(transformed_paths),
        issues=issues,
    )


def merge_prep_results(results: List[PrepResult]) -> Dict[str, Any]:
    transformed_paths: List[str] = []
    artifact_tables: List[Dict[str, Any]] = []
    prep_meta_items: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    table_count = 0

    for result in results:
        transformed_paths.extend(result.transformed_paths)
        artifact_tables.extend(result.artifact_tables)
        prep_meta_items.append(result.prep_meta)
        issues.extend(result.issues)
        table_count += result.table_count

    preview_texts: List[str] = []
    for item in artifact_tables[:6]:
        preview = ((item or {}).get("preview") or {}).get("text") or ""
        if preview:
            preview_texts.append(f"[{item.get('table_id')}]")
            preview_texts.append(preview)

    data_artifact = {
        "artifact_version": "1.0",
        "tables": artifact_tables,
        "preview": {
            "type": "multi_table",
            "text": "\n\n".join(preview_texts)[:12000],
        },
    }

    data_prep_meta = {
        "table_count": table_count,
        "sources": prep_meta_items,
        "issues": issues,
    }

    return {
        "transformed_data_sources": transformed_paths,
        "data_artifact": data_artifact,
        "data_prep_meta": data_prep_meta,
        "table_count": table_count,
        "prep_issues": issues,
    }


def build_downstream_reset_patch() -> Dict[str, Any]:
    return {
        "dataset_bundle": {},
        "bundle_issues": [],
        "data_profile": {},
        "chart_plan": [],
        "analysis_plan": {},
        "planning_issues": [],
        "analysis_results": {},
        "chart_data_ready_results": {},
        "visualizations": [],
        "visualizations_code": [],
        "visualization_log": [],
        "report_outline": {},
        "final_report": "",
        "report_versions": [],
        "active_report_id": None,
        "report_validation": {},
        "need_clarification": False,
        "pending_question": None,
        "error_code": None,
        "error_detail": None,
    }


def run_data_prep(
    raw_data_sources: List[str],
    output_dir: str,
) -> Dict[str, Any]:
    valid_sources, issues = validate_input_sources(raw_data_sources)

    if not valid_sources:
        return {
            "transformed_data_sources": [],
            "data_artifact": {},
            "data_prep_meta": {
                "table_count": 0,
                "sources": [],
                "issues": issues,
            },
            "table_count": 0,
            "prep_issues": issues,
            "error_code": "NO_VALID_INPUT_SOURCES",
            "error_detail": "; ".join([x.get("message", "") for x in issues])
            or "No valid input sources.",
        }

    results: List[PrepResult] = []
    all_issues = list(issues)

    for src in valid_sources:
        result = prepare_single_source(src, output_dir)
        results.append(result)
        all_issues.extend(result.issues)

    merged = merge_prep_results(results)
    merged["prep_issues"] = all_issues
    merged["data_prep_meta"]["issues"] = all_issues

    if merged["table_count"] <= 0:
        return {
            **merged,
            "error_code": "NO_USABLE_SUBTABLES",
            "error_detail": "; ".join(issue["message"] for issue in all_issues)
            or "No usable subtables were extracted from the input.",
        }

    return {
        **merged,
        "error_code": None,
        "error_detail": None,
    }
