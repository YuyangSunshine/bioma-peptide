"""Lossless measurement ingestion; no score-to-affinity conversion."""
from .contracts import digest

MISSING = {"missing", "not_detected", "below_detection_limit", "no_enrichment", "valid_negative", "unspecified"}
KINDS = {"experimental_kd", "functional_measurement", "screen_enrichment", "structure_score", "docking_score", "md_metric"}


def measurement(row):
    required = {"candidate_id", "target_id", "chemical_state", "source_id", "source_version", "measurement_kind"}
    if required - row.keys() or row["measurement_kind"] not in KINDS:
        raise ValueError("measurement_identity_or_kind_missing")
    result = dict(row)
    raw = row.get("value")
    if raw in (None, "", "/", "-"):
        result["value"] = None
        result["missingness"] = row.get("missingness", "unspecified")
    elif row.get("missingness"):
        raise ValueError("numeric_value_with_missingness_requires_explicit_censoring_field")
    if result.get("missingness") and result["missingness"] not in MISSING:
        raise ValueError("unknown_missingness")
    result["raw_value"] = raw
    for field in ("unit", "method", "conditions", "replicate_id", "construct", "target_domain", "family_id"):
        result.setdefault(field, None)
    result["record_id"] = digest(row)
    return result


def check_split(rows):
    """Union family, sequence aliases and replicate groups before checking splits."""
    groups = {}
    for row in rows:
        for key in ("family_id", "sequence_identity", "candidate_id", "replicate_group"):
            if row.get(key):
                groups.setdefault((key, row[key]), set()).add(row["split"])
    if any(len(splits) > 1 for splits in groups.values()):
        raise ValueError("candidate_family_alias_or_replicate_leakage")
