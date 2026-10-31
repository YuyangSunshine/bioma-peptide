"""Trusted per-run ensemble-size contract for crosslinked-peptide tools.

The contract path is supplied by the query runner through the process
environment, not by an LLM tool argument.  It therefore prevents an agent
from replacing a requested final ensemble size with an intermediate xTB pool.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bioma.tools.errors import ToolError


ENVIRONMENT_VARIABLE = "BIOMA_CROSSLINKED_ENSEMBLE_CONTRACT"


def load_contract() -> dict[str, Any] | None:
    raw_path = os.environ.get(ENVIRONMENT_VARIABLE)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            f"Cannot read {ENVIRONMENT_VARIABLE} at {path}: {exc}",
            error_type="invalid_input",
            retriable=False,
            backend="BioMA",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("final_ensemble_size"), int):
        raise ToolError(
            "The ensemble contract must contain integer final_ensemble_size.",
            error_type="invalid_input",
            retriable=False,
            backend="BioMA",
        )
    return payload


def contract_hash(contract: dict[str, Any] | None) -> str | None:
    if contract is None:
        return None
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def require_final_target(target_ensemble_size: int, *, stage: str) -> dict[str, Any] | None:
    contract = load_contract()
    if contract is not None and target_ensemble_size != contract["final_ensemble_size"]:
        raise ToolError(
            f"{stage} received target_ensemble_size={target_ensemble_size}, but the "
            f"trusted run contract requires final_ensemble_size="
            f"{contract['final_ensemble_size']}. Intermediate pool sizes are tool-internal.",
            error_type="invalid_input",
            retriable=False,
            backend="BioMA",
        )
    return contract


def internal_pool(contract: dict[str, Any] | None, *, key: str, fallback: int) -> int:
    if contract is None:
        return fallback
    value = contract.get(key, fallback)
    if not isinstance(value, int) or value < 1:
        raise ToolError(
            f"Ensemble contract field {key!r} must be a positive integer.",
            error_type="invalid_input",
            retriable=False,
            backend="BioMA",
        )
    return value
