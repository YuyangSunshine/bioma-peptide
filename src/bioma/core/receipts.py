from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VerificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "verification-receipt-v1"
    run_id: str
    verifier_id: str
    input_hash: str
    policy_hash: str
    decision: str
    reasons: tuple[str, ...] = ()
    receipt_hash: str
    policy_context: dict[str, Any] | None = Field(default=None, exclude=True)

    @classmethod
    def calculate(cls, *, run_id: str, verifier_id: str, inputs: dict[str, Any], policy: dict[str, Any], decision: str, reasons: tuple[str, ...] = ()) -> "VerificationReceipt":
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        input_hash = hashlib.sha256(canonical(inputs)).hexdigest()
        policy_hash = hashlib.sha256(canonical(policy)).hexdigest()
        body = {"schema_version": "verification-receipt-v1", "run_id": run_id, "verifier_id": verifier_id, "input_hash": input_hash, "policy_hash": policy_hash, "decision": decision, "reasons": reasons}
        return cls(**body, receipt_hash=hashlib.sha256(canonical(body)).hexdigest())

    def recompute(self) -> str:
        body = self.model_dump(exclude={"receipt_hash"})
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
