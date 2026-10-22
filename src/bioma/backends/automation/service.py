from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


from bioma.core.protocols import VerificationReport, ExecutionOutcome
from bioma.core.handoffs import ReviewedProtocol
from .adapters import AutomationCompiler
from .simulation import ProtocolSimulator


class ExecutionPhase(str, Enum):
    INIT = "INIT"
    ALIGN_AUTOMATION = "ALIGN_AUTOMATION"
    DESIGN_CODE = "DESIGN_CODE"
    VERIFY_CODE = "VERIFY_CODE"
    RECTIFY_CODE = "RECTIFY_CODE"
    SIMULATE = "SIMULATE"
    SUCCESS = "SUCCESS"


@dataclass
class ExecutionSession:
    task_id: str
    objective: str
    protocol_id: str
    protocol_text: str
    phase: ExecutionPhase = ExecutionPhase.INIT
    aligned_protocol: str | None = None
    machine_code: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)


class AutomationService:
    """Compile a reviewed handoff; simulation requires explicit opt-in."""

    def __init__(
        self,
        automator: AutomationCompiler,
        *,
        max_rectifications: int = 3,
        simulator: ProtocolSimulator | None = None,
        allow_simulation: bool = False,
    ) -> None:
        self.automator = automator
        self.max_rectifications = max_rectifications
        self.simulator = simulator or ProtocolSimulator()
        self.allow_simulation = allow_simulation
        self.sessions: dict[str, ExecutionSession] = {}

    def execute(
        self,
        task_id: str,
        simulation_inputs: dict[str, Any],
        *,
        protocol: ReviewedProtocol,
        compile_only: bool = False,
        execution_id: str | None = None,
    ) -> ExecutionOutcome:
        from bioma.runtime.admission import check_execution
        check_execution()
        protocol.validate(task_id)
        session = ExecutionSession(
            task_id=task_id,
            objective=protocol.objective,
            protocol_id=protocol.protocol_id,
            protocol_text=protocol.text,
        )
        self.sessions[task_id] = session
        session.phase = ExecutionPhase.ALIGN_AUTOMATION
        aligned_protocol = self.automator.align(
            session.protocol_text, session.objective
        )
        session.aligned_protocol = aligned_protocol
        session.history.append(
            {"phase": session.phase, "aligned_protocol_chars": len(aligned_protocol)}
        )
        session.phase = ExecutionPhase.DESIGN_CODE
        code = self.automator.generate(
            aligned_protocol,
            session.objective,
            session.protocol_id,
            task_id,
        )
        session.history.append(
            {"phase": session.phase, "node_count": len(code.get("nodes") or [])}
        )
        report = VerificationReport(False, ("not yet verified",))
        for attempt in range(self.max_rectifications + 1):
            session.phase = ExecutionPhase.VERIFY_CODE
            report = self.automator.validate(code)
            session.history.append(
                {
                    "phase": session.phase,
                    "passed": report.passed,
                    "issues": report.issues,
                }
            )
            if report.passed and not report.issues:
                break
            if attempt == self.max_rectifications:
                break
            session.phase = ExecutionPhase.RECTIFY_CODE
            code = self.automator.rectify(code, report.issues, task_id)
            code["protocolId"] = session.protocol_id
            session.history.append({"phase": session.phase, "issues": report.issues})
        if not report.passed or report.issues:
            raise RuntimeError(f"K_p failed after rectification: {report.issues}")
        if compile_only:
            session.machine_code = code
            return ExecutionOutcome(
                aligned_protocol,
                code,
                report,
                {"mode": "compilation", "executed": False},
                tuple(session.history),
            )
        session.phase = ExecutionPhase.SIMULATE
        if not self.allow_simulation:
            raise ValueError(
                "Real experimental execution requires an instrument observation adapter; simulation is disabled"
            )
        inputs = {**simulation_inputs, '_simulation_context': {
            'task_id': task_id, 'action_id': execution_id or task_id + ':' + protocol.protocol_id,
            'protocol_sha256': protocol.sha256}}
        try:
            check_execution()
            observation = self.simulator.execute(code, inputs)
        except TimeoutError:
            observation = self.simulator.reconcile(code, inputs)
            if observation is None:
                raise
            session.history.append({'phase':'RECONCILED', 'action_id':inputs['_simulation_context']['action_id']})
        session.phase = ExecutionPhase.SUCCESS
        session.machine_code = code
        session.history.append({"phase": session.phase, "observation": observation})
        return ExecutionOutcome(
            aligned_protocol,
            code,
            report,
            observation,
            tuple(session.history),
        )
