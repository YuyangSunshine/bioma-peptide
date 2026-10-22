"""Independent, bounded OLS checks; no execution or scientific commit authority."""
from __future__ import annotations

import csv
from decimal import Decimal, localcontext
import hashlib
import io
import json
import math
import statistics
from pathlib import Path

RTOL = 1e-8
ATOL = 1e-10


def _read(path, expected_hash):
    with Path(path).open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Numerical check input exceeds 16 MiB")
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("Numerical check file hash mismatch")
    return raw


def checked_linear_fit(source, source_sha256, x_column, y_column):
    """Unweighted OLS with intercept, complete finite rows; no causal inference."""
    reader = csv.DictReader(io.StringIO(_read(source, source_sha256).decode("utf-8-sig")))
    fields = reader.fieldnames or []
    if (len(fields) != len(set(fields)) or x_column == y_column
            or x_column not in fields or y_column not in fields):
        raise ValueError("Ambiguous or missing regression columns")
    rows = list(reader)
    if len(rows) < 3 or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError("At least three complete, well-formed rows required")
    try:
        xs = [Decimal(r[x_column]) for r in rows]
        ys = [Decimal(r[y_column]) for r in rows]
    except Exception as exc:
        raise ValueError("Regression requires numeric values") from exc
    if not all(v.is_finite() for v in xs + ys):
        raise ValueError("Regression requires finite values")
    with localcontext() as ctx:
        ctx.prec = 60
        n = Decimal(len(xs))
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            raise ValueError("Constant predictor cannot identify slope")
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        intercept = my - slope * mx
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        mse = sum(r * r for r in residuals) / n
    # A different implementation is a numerical cross-check, not another dataset.
    sx, sy = list(map(float, xs)), list(map(float, ys))
    if not all(math.isfinite(v) for v in sx + sy):
        raise ValueError("Values exceed supported floating-point range")
    try:
        second = statistics.linear_regression(sx, sy)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Independent floating-point fit is ill-conditioned") from exc
    for actual, expected in ((second.slope, slope), (second.intercept, intercept)):
        if not math.isclose(actual, float(expected), rel_tol=RTOL, abs_tol=ATOL):
            raise ValueError("Independent fitting implementations disagree")
    second_residuals = [y - (second.intercept + second.slope*x) for x, y in zip(sx, sy)]
    second_mse = statistics.fmean(r*r for r in second_residuals)
    if not math.isclose(second_mse, float(mse), rel_tol=RTOL, abs_tol=ATOL):
        raise ValueError("Independent residual calculations disagree")
    output = {"sample_count": len(xs), "slope": float(slope), "intercept": float(intercept),
              "mean_squared_residual": float(mse), "residuals": list(map(float, residuals))}
    if not all(math.isfinite(v) for v in [output["slope"], output["intercept"],
                                         output["mean_squared_residual"], *output["residuals"]]):
        raise ValueError("Fit exceeds supported numeric range")
    return output


def verify_linear_fit(source, source_sha256, result, result_sha256, x_column, y_column):
    """Check actual result bytes against frozen data, including optional residuals."""
    expected = checked_linear_fit(source, source_sha256, x_column, y_column)
    def unique(pairs):
        value = {}
        for k, v in pairs:
            if k in value:
                raise ValueError("Duplicate result key")
            value[k] = v
        return value
    saved = json.loads(_read(result, result_sha256), object_pairs_hook=unique)
    if not isinstance(saved, dict):
        raise ValueError("Regression result must be an object")
    if type(saved.get("sample_count")) is not int or saved["sample_count"] != expected["sample_count"]:
        raise ValueError("Incorrect sample count")
    def close(actual, target):
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, target, rel_tol=RTOL, abs_tol=ATOL))
    for key in ("slope", "intercept", "mean_squared_residual"):
        if not close(saved.get(key), expected[key]):
            raise ValueError("Incorrect regression " + key)
    if "residuals" in saved:
        residuals = saved["residuals"]
        if (not isinstance(residuals, list) or len(residuals) != expected["sample_count"]
                or not all(close(a, b) for a, b in zip(residuals, expected["residuals"]))):
            raise ValueError("Incorrect residuals")
    return expected
