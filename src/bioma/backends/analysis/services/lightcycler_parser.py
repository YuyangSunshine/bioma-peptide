from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd


_WELL_RE = re.compile(r"\b([A-H](?:[1-9]|1[0-2]))\b", re.IGNORECASE)
_SAMPLE_RE = re.compile(r"sample\s*([A-Za-z0-9_.-]+)", re.IGNORECASE)


@dataclass
class LightCyclerParseResult:
    dataframe: pd.DataFrame
    value_column: str
    encoding: str
    well_count: int
    row_count: int


def _read_tsv_rows(path: Path) -> tuple[list[list[str]], str]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1", "iso-8859-1"):
        try:
            with open(path, "r", encoding=encoding, errors="strict", newline="") as fh:
                rows = [
                    [str(cell).strip() for cell in row]
                    for row in csv.reader(fh, delimiter="\t")
                ]
            rows = [row for row in rows if any(str(cell).strip() for cell in row)]
            return rows, encoding
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"LIGHTCYCLER_TXT_READ_FAILED: {path}: {last_error!r}")


def _value_column_from_name(path: Path) -> Optional[str]:
    name = path.name.lower()
    if "melting" not in name:
        return None
    if "peak" in name:
        return "negative_derivative"
    if "curve" in name:
        return "fluorescence"
    return None


def _extract_well(header_cell: str) -> Optional[str]:
    match = _WELL_RE.search(str(header_cell or ""))
    if not match:
        return None
    return match.group(1).upper()


def _extract_sample(header_cell: str) -> str:
    text = str(header_cell or "").strip()
    match = _SAMPLE_RE.search(text)
    if match:
        return match.group(1)
    if ":" in text:
        return text.split(":", 1)[1].strip()
    return text


def parse_lightcycler_txt(path: str | Path) -> Optional[LightCyclerParseResult]:
    """
    Parse Roche/LightCycler-style 96-well melting exports.

    The parser intentionally returns None unless the file strongly matches the
    expected LightCycler tab export: alternating X/well header columns and a
    filename that identifies melting peaks or melting curves.
    """
    p = Path(path)
    if p.suffix.lower() != ".txt":
        return None

    value_column = _value_column_from_name(p)
    if not value_column:
        return None

    rows, encoding = _read_tsv_rows(p)
    if len(rows) < 2:
        return None

    header = rows[0]
    if len(header) < 24 or len(header) % 2 != 0:
        return None

    pairs: list[tuple[int, int, str, str]] = []
    for idx in range(0, len(header) - 1, 2):
        left = str(header[idx] or "").strip()
        right = str(header[idx + 1] or "").strip()
        well = _extract_well(right)
        if left.upper() != "X" or not well:
            continue
        pairs.append((idx, idx + 1, well, _extract_sample(right)))

    unique_wells = {well for _, _, well, _ in pairs}
    if len(pairs) < 12 or len(unique_wells) < 12:
        return None

    out_rows: list[dict[str, object]] = []
    for row in rows[1:]:
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        for temp_idx, value_idx, well, sample in pairs:
            temp = pd.to_numeric(row[temp_idx], errors="coerce")
            value = pd.to_numeric(row[value_idx], errors="coerce")
            if pd.isna(temp) or pd.isna(value):
                continue
            out_rows.append(
                {
                    "well": well,
                    "sample": sample,
                    "temperature": float(temp),
                    value_column: float(value),
                    "source_file": p.name,
                }
            )

    if not out_rows:
        return None

    df = pd.DataFrame(
        out_rows, columns=["well", "sample", "temperature", value_column, "source_file"]
    )
    return LightCyclerParseResult(
        dataframe=df,
        value_column=value_column,
        encoding=encoding,
        well_count=int(df["well"].nunique()),
        row_count=int(df.shape[0]),
    )


__all__ = ["LightCyclerParseResult", "parse_lightcycler_txt"]
