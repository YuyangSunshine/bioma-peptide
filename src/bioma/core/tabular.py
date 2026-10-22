"""Deterministic tabular summaries shared by scientific producers and validators."""
from __future__ import annotations

import csv
import hashlib
import math
import statistics
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_csv(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = list(reader.fieldnames or [])
    missing = {name: sum(row.get(name, "").strip() == "" for row in rows) for name in columns}
    duplicate_rows = len(rows) - len({tuple(row.get(name, "") for name in columns) for row in rows})
    numeric = {}
    categorical = {}
    for name in columns:
        values = [row.get(name, "").strip() for row in rows if row.get(name, "").strip() != ""]
        parsed = []
        for value in values:
            try:
                number = float(value)
                if math.isfinite(number):
                    parsed.append(number)
            except ValueError:
                pass
        if values and len(parsed) == len(values):
            ordered = sorted(parsed)
            q1, median, q3 = (
                statistics.quantiles(ordered, n=4, method="inclusive")
                if len(ordered) > 1 else (ordered[0],) * 3
            )
            iqr = q3 - q1
            numeric[name] = {
                "count": len(ordered), "min": min(ordered), "max": max(ordered),
                "mean": statistics.fmean(ordered),
                "standard_deviation": statistics.pstdev(ordered),
                "median": median, "q1": q1, "q3": q3,
                "iqr_outlier_count": sum(
                    x < q1 - 1.5 * iqr or x > q3 + 1.5 * iqr for x in ordered),
            }
        else:
            categorical[name] = {"count": len(values), "unique": len(set(values))}
    position = next(
        (name for name in columns
         if name.casefold() in {"position", "residue_position", "site"}),
        None,
    )
    coverage = None
    if position:
        values = sorted({row[position] for row in rows if row.get(position, "").strip()})
        counts = {value: sum(row.get(position) == value for row in rows) for value in values}
        coverage = {
            "column": position, "unique_positions": len(values),
            "minimum_rows_per_position": min(counts.values()) if counts else 0,
            "maximum_rows_per_position": max(counts.values()) if counts else 0,
        }
    return {
        "schema_version": "generic-tabular-analysis-v1",
        "row_count": len(rows), "column_count": len(columns), "columns": columns,
        "missing_values": missing, "duplicate_rows": duplicate_rows,
        "numeric_columns": numeric, "categorical_columns": categorical,
        "position_coverage": coverage,
        "interpretation_limits": [
            "Numeric summaries do not establish a model score direction.",
            "Model predictions are not experimental observations.",
        ],
    }


def check_binary_event_matrix(path: Path, expected_sha256: str, *, id_column: str,
                              metadata_columns: tuple[str, ...] = (),
                              required_id_prefix: str = 'TCGA-') -> dict:
    """Bounded input-contract check, not a biological interpretation or classifier.

    Checks every feature cell. Never thresholds continuous values into events or
    substitutes cell-line identifiers for patient identifiers. Caller fixes the
    source hash and schema; unknown/missing cells do not count as absence.
    """
    if sha256_file(path) != expected_sha256:
        raise ValueError('Frozen event matrix changed')
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        if len(set(columns)) != len(columns) or id_column not in columns:
            raise ValueError('Event matrix requires unique columns and declared identifier')
        if set(metadata_columns) - set(columns):
            raise ValueError('Declared metadata columns are absent')
        features = [c for c in columns if c not in {id_column, *metadata_columns}]
        counts = {'binary_cells': 0, 'nonbinary_numeric_cells': 0,
                  'missing_cells': 0, 'nonnumeric_cells': 0}
        identifiers = []
        for row in reader:
            if None in row or any(row[c] is None for c in columns):
                raise ValueError('Ragged event matrix')
            identifiers.append(row[id_column])
            for col in features:
                text = row[col].strip()
                if not text or text.lower() in {'na', 'nan', 'null'}:
                    counts['missing_cells'] += 1
                    continue
                try:
                    value = float(text)
                except ValueError:
                    counts['nonnumeric_cells'] += 1
                    continue
                if not math.isfinite(value):
                    counts['missing_cells'] += 1
                elif value in (0, 1):
                    counts['binary_cells'] += 1
                else:
                    counts['nonbinary_numeric_cells'] += 1
    if sha256_file(path) != expected_sha256:
        raise ValueError('Event matrix changed during verification')
    matching = sum(i.startswith(required_id_prefix) for i in identifiers)
    return {'source_sha256': expected_sha256, 'rows': len(identifiers),
            'feature_columns': len(features), 'id_column': id_column,
            'metadata_columns': list(metadata_columns),
            'required_id_prefix': required_id_prefix, 'matching_identifiers': matching,
            'unique_identifiers': len(set(identifiers)), **counts,
            'binary_event_matrix_verified': bool(identifiers and features)
                and matching == len(identifiers) == len(set(identifiers))
                and not any(counts[k] for k in counts if k != 'binary_cells'),
            'limits': ['Schema/value-domain verification only; mutation annotation and biological meaning are not certified.',
                       'No thresholding, imputation, gene mapping or scientific truth labels were introduced.']}
