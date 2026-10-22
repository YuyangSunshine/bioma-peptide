from __future__ import annotations

import ast
import hashlib
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from bioma.core.models import StrictModel, utc_now


class DeliveryStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    VALIDATED = "validated"
    FAILED = "failed"


class CoverageSufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    REPAIRABLE = "repairable"
    BLOCKING = "blocking"


class ArtifactObligation(StrictModel):
    """A deliverable requirement; it is deliberately not scientific evidence."""

    name: str = Field(min_length=1)
    kind: Literal["python", "text", "json", "csv", "tsv", "markdown", "image", "binary"]
    path: str = Field(min_length=1)
    producer: str = Field(default="artifact_producer", min_length=1)
    validation_mode: Literal["exists", "python_parse", "nonempty"] = "exists"
    required_for_terminal: bool = True

    @model_validator(mode="after")
    def safe_relative_path(self) -> "ArtifactObligation":
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or path.name in {"", "."}:
            raise ValueError("artifact path must be a safe relative path")
        if self.kind == "python" and path.suffix != ".py":
            raise ValueError("python artifacts must use a .py path")
        return self


class ExternalTaskContract(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    objective: str = Field(min_length=1)
    public_inputs: list[str] = Field(default_factory=list)
    execution_context: dict[str, Any] = Field(default_factory=dict)
    artifact_obligations: list[ArtifactObligation] = Field(min_length=1)
    terminal_delivery_criterion: str = "all_required_artifacts_validated"

    @model_validator(mode="after")
    def unique_obligations(self) -> "ExternalTaskContract":
        names = [item.name for item in self.artifact_obligations]
        paths = [item.path for item in self.artifact_obligations]
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            raise ValueError("artifact obligation names and paths must be unique")
        return self


class ArtifactDelivery(StrictModel):
    obligation_name: str
    path: str
    status: DeliveryStatus = DeliveryStatus.NOT_STARTED
    validation_error: str | None = None
    sha256: str | None = None


class DeliveryState(StrictModel):
    contract_id: str
    status: DeliveryStatus = DeliveryStatus.NOT_STARTED
    artifacts: list[ArtifactDelivery] = Field(default_factory=list)
    trajectory: list[DeliveryStatus] = Field(default_factory=lambda: [DeliveryStatus.NOT_STARTED])
    failure_reason: str | None = None
    started_at: Any | None = None
    finished_at: Any | None = None

    @classmethod
    def from_contract(cls, contract: ExternalTaskContract) -> "DeliveryState":
        return cls(contract_id=contract.id, artifacts=[
            ArtifactDelivery(obligation_name=item.name, path=item.path)
            for item in contract.artifact_obligations
        ])

    def transition(self, status: DeliveryStatus, *, reason: str | None = None) -> None:
        allowed = {
            DeliveryStatus.NOT_STARTED: {DeliveryStatus.IN_PROGRESS, DeliveryStatus.FAILED},
            DeliveryStatus.IN_PROGRESS: {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED},
            DeliveryStatus.DELIVERED: {DeliveryStatus.VALIDATED, DeliveryStatus.FAILED},
            DeliveryStatus.VALIDATED: set(),
            DeliveryStatus.FAILED: set(),
        }
        if status not in allowed[self.status]:
            raise ValueError(f"invalid delivery transition: {self.status} -> {status}")
        self.status = status
        self.trajectory.append(status)
        if status == DeliveryStatus.IN_PROGRESS:
            self.started_at = utc_now()
        if status in {DeliveryStatus.VALIDATED, DeliveryStatus.FAILED}:
            self.finished_at = utc_now()
            self.failure_reason = reason


class CoverageDecision(StrictModel):
    outcome: CoverageSufficiency
    reasons: list[str] = Field(default_factory=list)
    repair_count: int = Field(default=0, ge=0)
    max_repairs: int = Field(default=1, ge=0)
    unique_source_fragments: int = Field(default=0, ge=0)


def assess_external_coverage(
    contract: ExternalTaskContract, *, source_fragments: list[str] | None = None,
    repair_count: int = 0, max_repairs: int = 1,
) -> CoverageDecision:
    """Decide whether an executable delivery contract has enough coverage to run."""
    fragments = {" ".join(item.split()).casefold() for item in source_fragments or [] if item.strip()}
    reasons: list[str] = []
    if not contract.objective.strip():
        reasons.append("objective is missing")
    if not contract.artifact_obligations:
        reasons.append("artifact obligations are missing")
    if any(not item.producer for item in contract.artifact_obligations):
        reasons.append("artifact producer is missing")
    missing_inputs = [path for path in contract.public_inputs if not Path(path).is_file()]
    if missing_inputs:
        reasons.append("public inputs are unavailable: " + ", ".join(missing_inputs))
    if not reasons:
        outcome = CoverageSufficiency.SUFFICIENT
    elif repair_count < max_repairs and not missing_inputs:
        outcome = CoverageSufficiency.REPAIRABLE
    else:
        outcome = CoverageSufficiency.BLOCKING
    return CoverageDecision(outcome=outcome, reasons=reasons, repair_count=repair_count,
                            max_repairs=max_repairs, unique_source_fragments=len(fragments))


def validate_delivery(contract: ExternalTaskContract, workspace: Path,
                      state: DeliveryState, *, artifact_root: Path | None = None) -> DeliveryState:
    """Validate configured artifact obligations without creating scientific evidence."""
    required_valid = True
    delivered_any = False
    by_name = {item.obligation_name: item for item in state.artifacts}
    for obligation in contract.artifact_obligations:
        delivery = by_name[obligation.name]
        target = (artifact_root or workspace) / obligation.path
        if not target.is_file():
            delivery.status = DeliveryStatus.NOT_STARTED
            delivery.validation_error = "required artifact does not exist"
            required_valid &= not obligation.required_for_terminal
            continue
        delivered_any = True
        delivery.status = DeliveryStatus.DELIVERED
        delivery.sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        try:
            data = target.read_bytes()
            if obligation.validation_mode == "nonempty" and not data:
                raise ValueError("artifact is empty")
            if obligation.validation_mode == "python_parse":
                ast.parse(data.decode("utf-8"), filename=obligation.path)
            delivery.status = DeliveryStatus.VALIDATED
            delivery.validation_error = None
        except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
            delivery.validation_error = str(exc)
            required_valid &= not obligation.required_for_terminal
    if delivered_any and state.status == DeliveryStatus.IN_PROGRESS:
        state.transition(DeliveryStatus.DELIVERED)
    if required_valid and all(
        by_name[item.name].status == DeliveryStatus.VALIDATED
        for item in contract.artifact_obligations if item.required_for_terminal
    ):
        if state.status == DeliveryStatus.DELIVERED:
            state.transition(DeliveryStatus.VALIDATED)
    elif state.status in {DeliveryStatus.IN_PROGRESS, DeliveryStatus.DELIVERED}:
        state.transition(DeliveryStatus.FAILED, reason="required artifact validation failed")
    return state
