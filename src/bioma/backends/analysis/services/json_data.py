"""Lossless JSON sources with explicit, non-joining tabular projections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import pandas as pd


def normalize_scientific_json(value, *, source_missing=False):
    """Lossless JSON-compatible scalars; missing sources are not computed numbers.

    NaN alone has no provenance. Only the explicit source_missing boundary may
    interpret it as an absent source cell. Infinity is never a missing cell.
    Undefined calculations must carry an explicit null and status/reason supplied
    by the calculation; serialization neither establishes truth nor egress rights.
    """
    import math
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if type(value).__module__.split('.')[0] == 'numpy':
        converted = value.tolist() if hasattr(value, 'tolist') else value.item()
        if type(converted) is type(value):
            raise TypeError('Numeric conversion would lose precision; provide an explicit representation')
        return normalize_scientific_json(converted, source_missing=source_missing)
    if isinstance(value, float):
        if math.isnan(value) and source_missing:
            return None
        if not math.isfinite(value):
            raise ValueError('Non-finite value is not JSON compliant. source_missing=True is only '
                'for missing source cells; undefined calculations require null with an explicit status/reason. '
                'Infinity is never converted to missing or zero.')
        return value
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (list, tuple)):
        return [normalize_scientific_json(v, source_missing=source_missing) for v in value]
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise TypeError('Scientific JSON object keys must be strings')
        return {k: normalize_scientific_json(v, source_missing=source_missing) for k,v in value.items()}
    raise TypeError(f'Artifact values must be JSON-compatible, got {type(value).__name__}')


def scientific_json_dumps(value, *, source_missing=False, **kwargs):
    return json.dumps(normalize_scientific_json(value, source_missing=source_missing),
                      ensure_ascii=False, allow_nan=False, **kwargs)


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def load_document(raw: bytes) -> Any:
    return normalize_scientific_json(json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant))


def _cell(value: Any) -> Any:
    # Nested values stay in their parent record, never explode into extra rows.
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def _frame(value: Any, projection: str) -> pd.DataFrame:
    if projection == "records":
        rows = [{key: _cell(item) for key, item in row.items()} for row in value]
    elif projection == "properties":
        rows = [{key: item for key, item in value.items() if not isinstance(item, (dict, list))}]
    else:
        rows = [{"value": _cell(item)} for item in value]
    return pd.DataFrame(rows, dtype=object)


def _views(value: Any, pointer: str = "") -> Iterator[tuple[str, str, pd.DataFrame]]:
    if isinstance(value, list):
        if value:
            projection = "records" if all(isinstance(row, dict) for row in value) else "values"
            frame = _frame(value, projection)
            if len(frame.columns):
                yield pointer, projection, frame
    elif isinstance(value, dict):
        frame = _frame(value, "properties")
        if len(frame.columns):
            yield pointer, "properties", frame
        for key, child in value.items():
            token = key.replace("~", "~0").replace("/", "~1")
            yield from _views(child, f"{pointer}/{token}")


def prepare_json(source: Path, output_dir: Path) -> tuple[list[dict], dict]:
    raw = source.read_bytes()
    document = load_document(raw)
    if not isinstance(document, (dict, list)):
        raise ValueError("JSON data must have an object or array at its root.")
    digest = hashlib.sha256(raw).hexdigest()
    source_id = hashlib.sha256(str(source.resolve()).encode("utf-8") + raw).hexdigest()[:16]
    folder = output_dir / f"{source.stem}__{source_id}"
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / "source.json"
    snapshot.write_bytes(raw)
    tables = []
    for index, (pointer, projection, frame) in enumerate(_views(document), 1):
        path = folder / f"table_{index:02d}.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        tables.append({
            "table_id": f"{source.stem}__{source_id}__{index}", "path": str(path),
            "source_file": str(source), "extraction_method": "json_projection",
            "json_source": {"path": str(snapshot), "sha256": digest,
                            "pointer": pointer, "projection": projection},
            "shape": {"rows": len(frame), "cols": len(frame.columns)},
            "preview": {"type": "table", "text": frame.head(20).to_csv(index=False)[:4000]},
        })
    return tables, {"source_file": str(source), "source_suffix": ".json",
                    "read_strategies": ["json_projection"], "table_count": len(tables),
                    "snapshot": str(snapshot), "sha256": digest}


def read_json_view(source: dict) -> pd.DataFrame:
    """Load the typed source, not the potentially lossy CSV preview."""
    raw = Path(source["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("JSON source snapshot hash mismatch.")
    value = load_document(raw)
    if source["pointer"]:
        for token in source["pointer"][1:].split("/"):
            key = token.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
    return _frame(value, source["projection"])
