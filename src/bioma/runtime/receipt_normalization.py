"""Normalize Oper receipts for WP1 without changing admission semantics."""
from __future__ import annotations
from typing import Any, Literal
from pydantic import Field
from bioma.core.models import StrictModel

EffectStatus = Literal["NOT_EXECUTED", "EXECUTED", "EFFECT_UNKNOWN", "PARTIAL_EFFECT", "RECONCILED"]

class NormalizedExecutionReceipt(StrictModel):
    action_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    mandate_id: str = Field(min_length=1)
    execution_contract_id: str = Field(min_length=1)
    contract_hash: str = Field(min_length=1)
    effect_status: EffectStatus
    acknowledgement_status: Literal["ACKNOWLEDGED", "LOST", "NOT_APPLICABLE"]
    tool_identity: str | None = None
    device_identity: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    results: dict[str, Any] = Field(default_factory=dict)
    started_at: float | None = None
    ended_at: float | None = None
    ordering: list[Any] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1)
    provenance: dict[str, Any] = Field(default_factory=dict)

def normalize_oper_receipt(receipt: dict[str, Any], *, mandate_id: str,
                           execution_contract_id: str, attempt: int = 1,
                           acknowledged: bool = True, reconciled: bool = False) -> NormalizedExecutionReceipt:
    executed = receipt.get("status") == "completed" or receipt.get("executed") is True
    status: EffectStatus = ("RECONCILED" if reconciled else "EFFECT_UNKNOWN" if not acknowledged and executed
                            else "EXECUTED" if executed else "PARTIAL_EFFECT" if receipt.get("status") == "partial"
                            else "NOT_EXECUTED")
    action_id = str(receipt.get("action_id") or "")
    contract_hash = str(receipt.get("contract_sha256") or receipt.get("contract_hash") or "")
    if not action_id or not contract_hash:
        raise ValueError("Oper receipt must carry action_id and contract hash")
    return NormalizedExecutionReceipt(action_id=action_id, attempt=attempt, mandate_id=mandate_id,
        execution_contract_id=execution_contract_id, contract_hash=contract_hash, effect_status=status,
        acknowledgement_status="ACKNOWLEDGED" if acknowledged else "LOST",
        tool_identity=receipt.get("tool_id"), device_identity=receipt.get("device_id"),
        artifacts=list(receipt.get("artifact_ids") or []), results=dict(receipt.get("results") or {}),
        started_at=receipt.get("started_at"), ended_at=receipt.get("ended_at"),
        ordering=list(receipt.get("node_trace") or []), idempotency_key=action_id,
        provenance={"source": "operagent", "raw_receipt": receipt})

def reconcile_oper_receipt(unknown: NormalizedExecutionReceipt, recovered: dict[str, Any]) -> NormalizedExecutionReceipt:
    candidate = normalize_oper_receipt(recovered, mandate_id=unknown.mandate_id,
        execution_contract_id=unknown.execution_contract_id, attempt=unknown.attempt, reconciled=True)
    if (candidate.action_id, candidate.contract_hash) != (unknown.action_id, unknown.contract_hash):
        raise ValueError("Reconciliation receipt identity or contract mismatch")
    return candidate
