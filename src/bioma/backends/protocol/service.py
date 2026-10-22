from __future__ import annotations

import json
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


from bioma.core.protocols import VerificationReport, ProtocolOutcome
from .designers import ProtocolDesigner


class ProtocolPhase(str, Enum):
    INIT = "INIT"
    DESIGN_DRAFT = "DESIGN_DRAFT"
    VERIFY_DRAFT = "VERIFY_DRAFT"
    RECTIFY_DRAFT = "RECTIFY_DRAFT"


@dataclass
class ProtocolSession:
    task_id: str
    phase: ProtocolPhase = ProtocolPhase.INIT
    objective: str = ""
    protocol_id: str | None = None
    protocol_text: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


class ProtocolDesignService:
    """Draft, review and repair study protocols; owns no automation state."""

    def __init__(
        self,
        designer: ProtocolDesigner,
        *,
        max_rectifications: int = 3,
        checkpoint_dir: Path | None = None,
    ) -> None:
        self.designer = designer
        self.max_rectifications = max_rectifications
        self.checkpoint_dir = Path(checkpoint_dir).resolve() if checkpoint_dir else None
        self.sessions: dict[str, ProtocolSession] = {}

    def design_protocol(
        self,
        task_id: str,
        objective: str,
        prediction: dict[str, Any],
        *,
        previous_protocol: str | None = None,
        revision_issues: tuple[str, ...] = (),
        work_id: str | None = None,
    ) -> ProtocolOutcome:
        session = ProtocolSession(task_id=task_id, objective=objective)
        self.sessions[task_id] = session
        identity = json.dumps(
            {
                "version": 1,
                "task_id": task_id,
                "work_id": work_id,
                "objective": objective,
                "inputs": prediction,
                "previous_protocol": previous_protocol,
                "issues": revision_issues,
                "reviewer": getattr(
                    getattr(self.designer, "provider", None), "config", None
                ).model_dump(mode="json")
                if hasattr(
                    getattr(getattr(self.designer, "provider", None), "config", None),
                    "model_dump",
                )
                else type(self.designer).__name__,
            },
            sort_keys=True,
            default=str,
        )
        checkpoint = (
            (
                self.checkpoint_dir
                / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
            )
            if self.checkpoint_dir
            else None
        )
        saved = (
            json.loads(checkpoint.read_text(encoding="utf-8"))
            if checkpoint and checkpoint.exists()
            else None
        )

        def save(stage, attempt, text, report=None):
            if checkpoint is None:
                return
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "protocol_id": protocol_id,
                "text": text,
                "stage": stage,
                "attempt": attempt,
                "history": session.history,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "report": {"passed": report.passed, "issues": report.issues}
                if report
                else None,
            }
            temporary = checkpoint.with_name(uuid4().hex + ".tmp")
            try:
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                temporary.replace(checkpoint)
            finally:
                temporary.unlink(missing_ok=True)

        if saved:
            if hashlib.sha256(saved["text"].encode()).hexdigest() != saved["sha256"]:
                raise ValueError("Protocol checkpoint content changed.")
            protocol_id, protocol_text = saved["protocol_id"], saved["text"]
            session.history = saved["history"]
            if saved["stage"] == "verified":
                session.protocol_id, session.protocol_text = protocol_id, protocol_text
                report = VerificationReport(
                    saved["report"]["passed"], tuple(saved["report"]["issues"])
                )
                if not report.passed or report.issues:
                    raise ValueError("Invalid verified protocol checkpoint.")
                return ProtocolOutcome(
                    protocol_id, protocol_text, report, tuple(session.history)
                )
        session.phase = ProtocolPhase.DESIGN_DRAFT
        if not saved:
            protocol_id = f"protocol-{uuid4()}"
            protocol_text = (
                self.designer.revise(objective, previous_protocol, revision_issues)
                if previous_protocol is not None
                else self.designer.generate(objective, prediction)
            )
            session.history.append({"phase": session.phase, "protocol_id": protocol_id})
            save("review", 0, protocol_text)
        start_attempt = saved["attempt"] if saved else 0
        if saved and saved["stage"] == "revise":
            if start_attempt >= self.max_rectifications:
                raise RuntimeError(
                    f"K_s failed after rectification: {saved['report']['issues']}"
                )
            protocol_text = self.designer.revise(
                objective, protocol_text, tuple(saved["report"]["issues"])
            )
            start_attempt += 1
            save("review", start_attempt, protocol_text)
        report = VerificationReport(False, ("not yet verified",))
        for attempt in range(start_attempt, self.max_rectifications + 1):
            session.phase = ProtocolPhase.VERIFY_DRAFT
            report = self.designer.review(objective, protocol_text)
            session.history.append(
                {
                    "phase": session.phase,
                    "passed": report.passed,
                    "issues": report.issues,
                }
            )
            if report.passed and not report.issues:
                session.protocol_id = protocol_id
                session.protocol_text = protocol_text
                save("verified", attempt, protocol_text, report)
                return ProtocolOutcome(
                    protocol_id,
                    protocol_text,
                    report,
                    tuple(session.history),
                )
            save("revise", attempt, protocol_text, report)
            if attempt == self.max_rectifications:
                break
            session.phase = ProtocolPhase.RECTIFY_DRAFT
            protocol_text = self.designer.revise(
                objective, protocol_text, report.issues
            )
            session.history.append({"phase": session.phase, "issues": report.issues})
            save("review", attempt + 1, protocol_text)
        raise RuntimeError(f"K_s failed after rectification: {report.issues}")
