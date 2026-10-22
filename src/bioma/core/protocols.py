from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerificationReport:
    passed: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProtocolOutcome:
    protocol_id: str
    protocol_text: str
    verification: VerificationReport
    history: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ExecutionOutcome:
    aligned_protocol: str
    machine_code: dict[str, Any]
    verification: VerificationReport
    observation: dict[str, Any]
    history: tuple[dict[str, Any], ...]
