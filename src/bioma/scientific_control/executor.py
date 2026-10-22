"""Governed lifecycle for an already selected, exactly grounded option.

This module neither proposes nor compares options.  Domain agents remain the
method executors; the production runner below enters through the existing
router, WP1 admission, verification, ledger, and scientific commit path.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from bioma.core.methods import ResultContract
from bioma.core.models import ScientificState, ScientificTask
from .grounding import GroundedOption, GroundedStep, grounding_is_current
from .options import ScientificOption
from .plan import ResearchPlan
from .relations import FrozenModel, Reference


class OptionLifecycle(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUSPENDED = "SUSPENDED"


class StepExecutionOutcome(FrozenModel):
    """References returned by a governed method attempt, never evidence itself."""
    status: Literal["ACCEPTED", "BLOCKED", "FAILED", "EFFECT_UNKNOWN"]
    attempt_refs: tuple[Reference, ...] = ()
    permit_refs: tuple[Reference, ...] = ()
    mandate_refs: tuple[Reference, ...] = ()
    receipt_refs: tuple[Reference, ...] = ()
    result_refs: tuple[Reference, ...] = ()
    evidence_refs: tuple[Reference, ...] = ()
    artifact_refs: tuple[Reference, ...] = ()
    accepted_result_classes: tuple[Reference, ...] = ()
    unresolved_effect_refs: tuple[Reference, ...] = ()
    reason: str = ""


class OptionExecutionRecord(FrozenModel):
    schema_version: Literal["option-execution-v1"] = "option-execution-v1"
    execution_instance_id: Reference
    option_identity: Reference
    grounding_identity: Reference
    contract_ref: Reference
    contract_version: int = Field(ge=1)
    plan_ref: Reference
    plan_version: int = Field(ge=1)
    plan_identity_hash: Reference
    parent_execution_ref: Reference | None = None
    child_execution_refs: tuple[Reference, ...] = ()
    current_step_id: Reference | None = None
    lifecycle: OptionLifecycle = OptionLifecycle.PENDING
    completed_step_ids: tuple[Reference, ...] = ()
    attempt_refs: tuple[Reference, ...] = ()
    permit_refs: tuple[Reference, ...] = ()
    mandate_refs: tuple[Reference, ...] = ()
    receipt_refs: tuple[Reference, ...] = ()
    checkpoint_refs: tuple[Reference, ...] = ()
    result_refs: tuple[Reference, ...] = ()
    evidence_refs: tuple[Reference, ...] = ()
    artifact_refs: tuple[Reference, ...] = ()
    accepted_result_classes: tuple[Reference, ...] = ()
    unresolved_effect_refs: tuple[Reference, ...] = ()
    status_reason: str = ""
    created_at: datetime
    updated_at: datetime
    provenance_refs: tuple[Reference, ...] = ()

    @model_validator(mode="after")
    def canonical_refs(self):
        for field in ("child_execution_refs", "completed_step_ids", "attempt_refs",
                      "permit_refs", "mandate_refs", "receipt_refs", "checkpoint_refs",
                      "result_refs", "evidence_refs", "artifact_refs",
                      "accepted_result_classes", "unresolved_effect_refs", "provenance_refs"):
            object.__setattr__(self, field, tuple(sorted(set(getattr(self, field)))))
        return self


def new_execution_record(option: ScientificOption, grounded: GroundedOption, *,
                         execution_instance_id: str, parent_execution_ref: str | None = None,
                         now: datetime | None = None) -> OptionExecutionRecord:
    if grounded.option_identity != option.identity:
        raise ValueError("grounding does not bind the supplied option")
    timestamp = now or datetime.now(timezone.utc)
    return OptionExecutionRecord(execution_instance_id=execution_instance_id,
        option_identity=option.identity, grounding_identity=grounded.identity,
        contract_ref=grounded.contract_ref, contract_version=grounded.contract_version,
        plan_ref=grounded.plan_ref, plan_version=grounded.plan_version,
        plan_identity_hash=grounded.plan_identity_hash,
        parent_execution_ref=parent_execution_ref, created_at=timestamp,
        updated_at=timestamp, provenance_refs=(option.identity, grounded.identity))


StepRunner = Callable[[GroundedStep, ScientificOption, OptionExecutionRecord], StepExecutionOutcome]


class OptionExecutor:
    """Faithfully execute one supplied grounding; no planning or ranking."""

    def __init__(self, runtime=None, *, step_runner: StepRunner | None = None,
                 clock: Callable[[], datetime] | None = None):
        if (runtime is None) == (step_runner is None):
            raise ValueError("supply exactly one production runtime or governed step runner")
        self.runtime = runtime
        self.step_runner = step_runner or self._run_production_step
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(self, option: ScientificOption, grounded: GroundedOption, *,
                contract, plan: ResearchPlan, record: OptionExecutionRecord) -> OptionExecutionRecord:
        if record.lifecycle == OptionLifecycle.COMPLETED:
            return record
        if record.lifecycle == OptionLifecycle.SUSPENDED and record.unresolved_effect_refs:
            return record  # Reconciliation is mandatory; never blind-retry.
        if (record.option_identity != option.identity or
                record.grounding_identity != grounded.identity):
            return self._finish(record, OptionLifecycle.BLOCKED, "GROUNDING_STALE: record identity mismatch")
        snapshot_current = ((grounded.contract_ref, grounded.contract_version) ==
            (contract.identity, contract.version) and
            (grounded.plan_ref, grounded.plan_version, grounded.plan_identity_hash) ==
            plan.version_identity)
        runtime_current = (self.runtime is None or grounding_is_current(grounded,
            contract=contract, plan=plan, registry=self.runtime.capability_registry,
            active_agents=set(self.runtime.agents),
            available_resources=self.runtime.available_resources))
        if not snapshot_current or not runtime_current:
            return self._finish(record, OptionLifecycle.BLOCKED, "GROUNDING_STALE: live runtime changed")
        current = record.model_copy(update={"lifecycle": OptionLifecycle.ACTIVE,
                                            "updated_at": self.clock()})
        by_id = {step.step_id: step for step in grounded.steps}
        for step in grounded.steps:
            if step.step_id in current.completed_step_ids:
                continue
            if not set(step.depends_on_step_ids) <= set(current.completed_step_ids):
                return self._finish(current, OptionLifecycle.BLOCKED,
                                    "dependent step lacks an accepted checkpoint")
            current = current.model_copy(update={"current_step_id": step.step_id,
                                                  "updated_at": self.clock()})
            outcome = self.step_runner(step, option, current)
            current = self._merge(current, outcome)
            if outcome.status == "EFFECT_UNKNOWN":
                return self._finish(current, OptionLifecycle.SUSPENDED,
                                    outcome.reason or "EFFECT_UNKNOWN: reconciliation required")
            if outcome.status == "FAILED":
                return self._finish(current, OptionLifecycle.FAILED,
                                    outcome.reason or "grounded step failed")
            if outcome.status != "ACCEPTED":
                return self._finish(current, OptionLifecycle.BLOCKED,
                                    outcome.reason or "step result was not accepted")
            checkpoint = f"checkpoint:{record.execution_instance_id}:{step.step_id}"
            current = current.model_copy(update={
                "completed_step_ids": (*current.completed_step_ids, step.step_id),
                "checkpoint_refs": (*current.checkpoint_refs, checkpoint),
                "current_step_id": None, "updated_at": self.clock()})
        if option.termination_condition == "one declared WorkResult exists":
            # A WorkResult has one discriminated kind. A Method's advertised
            # kinds are alternatives, not a requirement to emit every union arm.
            contracts = [ResultContract.model_validate_json(s.result_contract_json)
                         for s in grounded.steps]
            allowed = set(option.expected_result_classes)
            accepted = set(current.accepted_result_classes)
            if (len(contracts) != 1 or contracts[0].result_channel != 'work_result'
                    or not allowed <= set(contracts[0].evidence_kinds)
                    or not current.result_refs or not accepted.intersection(allowed)
                    or not accepted <= set(contracts[0].evidence_kinds)):
                return self._finish(current, OptionLifecycle.BLOCKED,
                    "termination unsatisfied: no committed declared WorkResult")
        elif option.termination_condition == "accepted result exists":
            if not set(option.expected_result_classes) <= set(current.accepted_result_classes):
                return self._finish(current, OptionLifecycle.BLOCKED,
                                    "termination unsatisfied: expected accepted result classes are absent")
        else:
            return self._finish(current, OptionLifecycle.BLOCKED,
                                "termination condition has no deterministic evaluator")
        return self._finish(current, OptionLifecycle.COMPLETED, "termination condition satisfied")

    def reconcile_unknown(self, record: OptionExecutionRecord,
                          resolution: StepExecutionOutcome) -> OptionExecutionRecord:
        """Apply an authoritative reconciliation outcome; this does not create evidence."""
        if record.lifecycle != OptionLifecycle.SUSPENDED or not record.unresolved_effect_refs:
            raise ValueError("execution has no unresolved effect")
        if resolution.status == "EFFECT_UNKNOWN" or not resolution.receipt_refs:
            raise ValueError("reconciliation needs a conclusive authoritative receipt")
        current = self._merge(record, resolution).model_copy(update={
            "unresolved_effect_refs": (), "updated_at": self.clock()})
        if resolution.status == "ACCEPTED":
            if current.current_step_id is None:
                raise ValueError("suspended execution has no current step")
            checkpoint = f"checkpoint:{record.execution_instance_id}:{current.current_step_id}"
            return current.model_copy(update={
                "lifecycle": OptionLifecycle.ACTIVE,
                "completed_step_ids": (*current.completed_step_ids, current.current_step_id),
                "checkpoint_refs": (*current.checkpoint_refs, checkpoint),
                "current_step_id": None, "status_reason": "effect reconciled",
                "updated_at": self.clock()})
        lifecycle = OptionLifecycle.FAILED if resolution.status == "FAILED" else OptionLifecycle.BLOCKED
        return self._finish(current, lifecycle, resolution.reason or "effect reconciliation did not accept output")

    def _run_production_step(self, step, option, record):
        return run_grounded_step(self.runtime, step, option, record)

    def _merge(self, record, outcome):
        additions = {
            "attempt_refs": outcome.attempt_refs, "permit_refs": outcome.permit_refs,
            "mandate_refs": outcome.mandate_refs, "receipt_refs": outcome.receipt_refs,
            "result_refs": outcome.result_refs, "evidence_refs": outcome.evidence_refs,
            "artifact_refs": outcome.artifact_refs,
            "accepted_result_classes": outcome.accepted_result_classes,
            "unresolved_effect_refs": outcome.unresolved_effect_refs,
        }
        return record.model_copy(update={field: (*getattr(record, field), *values)
            for field, values in additions.items()} | {"updated_at": self.clock()})

    def _finish(self, record, lifecycle, reason):
        return record.model_copy(update={"lifecycle": lifecycle, "status_reason": reason,
                                         "updated_at": self.clock()})


def _accepted_result(state: ScientificState, contract: ResultContract):
    evidence = [item for item in state.evidence if item.kind in contract.evidence_kinds]
    evidence_ids = {item.id for item in evidence}
    artifact_kinds = {item.kind for item in state.artifacts
                      if evidence_ids.intersection(item.evidence_ids)}
    accepted = bool(evidence) and set(contract.artifact_kinds) <= artifact_kinds
    return accepted, evidence, [item for item in state.artifacts if item.kind in artifact_kinds]


def run_grounded_step(runtime, step: GroundedStep, option: ScientificOption,
                      record: OptionExecutionRecord) -> StepExecutionOutcome:
    """Run one exact binding through production routing and WP1."""
    binding = getattr(step, 'capability_binding', None)
    if binding is not None and (
            binding.content_reference() != binding.qualification_ref
            or (binding.capability_family, binding.provider_id, binding.method_id,
                binding.implementation_identity) != (step.capability_id, step.provider_id,
                    step.method_id, step.implementation_identity)
            or binding.result_contract.model_dump(mode='json') != json.loads(step.result_contract_json)
            or binding.request.payload.get('inputs') != json.loads(step.input_bindings_json)):
        return StepExecutionOutcome(status='BLOCKED', reason='GROUNDING_STALE: neutral binding changed')
    task_id = "option." + hashlib.sha256(
        f"{record.execution_instance_id}:{step.step_id}".encode()).hexdigest()
    inputs = json.loads(step.input_bindings_json)
    source_inputs = list(inputs.get("source_inputs", []))
    admitted_refs = set(getattr(record, "result_refs", ()))
    for dependency_id in getattr(step, "depends_on_step_ids", ()):
        dependency_task_id = "option." + hashlib.sha256(
            f"{record.execution_instance_id}:{dependency_id}".encode()).hexdigest()
        dependency_state = runtime.ledger.replay_state(dependency_task_id) if runtime.ledger else None
        if dependency_state is None:
            return StepExecutionOutcome(status="BLOCKED",
                reason="dependent step has no replayable accepted state: " + dependency_id)
        for observation in dependency_state.observations:
            if observation.kind == "work_result" and observation.id in admitted_refs:
                source_inputs.append(observation.model_dump(mode="json"))
    if getattr(step, "depends_on_step_ids", ()) and admitted_refs and not source_inputs:
        return StepExecutionOutcome(status="BLOCKED",
            reason="dependent step has no admitted WorkResult input")
    if source_inputs:
        unique = {item["id"]: item for item in source_inputs
                  if isinstance(item, dict) and item.get("id")}
        inputs["source_inputs"] = [unique[key] for key in sorted(unique)]
        inputs["input_refs"] = sorted(set(inputs.get("input_refs", ())) | set(unique))
    scientific_objective = (binding.request.objective if binding is not None
                            else getattr(option, "scientific_intent", None)
                            or getattr(step, "description", None)
                            or "Execute the grounded scientific method.")
    task = ScientificTask(id=task_id,
        objective=scientific_objective,
        initial_capability=step.capability_id, initial_method_id=step.method_id,
        initial_message_type=step.message_type, active_agents=[step.provider_id],
        inputs=inputs, success_criteria={
            "result_contract": json.loads(step.result_contract_json)}, max_cycles=8,
        gateway={"exact_grounded_binding": {
            "provider_id": step.provider_id, "capability_id": step.capability_id,
            "method_id": step.method_id,
            "implementation_identity": step.implementation_identity},
            "scoped_capability_context": {
                "scientific_action_ref": "scientific-action:" + hashlib.sha256(
                    f"{record.execution_instance_id}:{getattr(option, 'identity', 'direct')}:{step.step_id}".encode()).hexdigest(),
                "option_ref": getattr(option, 'identity', None),
                "contract_ref": getattr(record, 'contract_ref', 'direct-contract:' + task_id),
                "contract_version": getattr(record, 'contract_version', 1),
                "plan_ref": getattr(record, 'plan_ref', 'direct-plan:' + task_id),
                "plan_version": getattr(record, 'plan_version', 1),
                "plan_identity_hash": getattr(record, 'plan_identity_hash', 'direct'),
                "grounding_ref": getattr(record, 'grounding_identity', None),
                "execution_instance_ref": record.execution_instance_id,
                "parent_task_ref": (getattr(record, "plan_ref", "").removeprefix("plan:")
                                    if getattr(record, "plan_ref", "").startswith("plan:") else None),
                "step_ref": step.step_id}})
    error = None
    try:
        state = runtime.run(task)
    except Exception as exc:  # Runtime journal remains authoritative after failure.
        from bioma.llm import ModelBudgetUnavailable
        if isinstance(exc, ModelBudgetUnavailable):
            raise  # No false FAILED scientific outcome and no failed-binding pruning.
        error = exc
        state = runtime.ledger.replay_state(task_id) if runtime.ledger else None
    events = runtime.ledger.events(task_id) if runtime.ledger else ()
    permits = [event.payload for event in events if event.kind == "ExecutionPermitted"]
    receipts = [event.payload for event in events if event.kind == "ExecutionResultRecorded"]
    unknown = [event for event in events if event.kind == "reconciliation.required"]
    common = dict(
        attempt_refs=tuple("attempt:" + p["proposal_id"] + ":" + str(p["attempt"]) for p in permits),
        permit_refs=tuple("permit:" + p["proposal_id"] + ":" + str(p["attempt"]) for p in permits),
        mandate_refs=tuple("mandate:" + p["proposal_id"] for p in permits),
        receipt_refs=tuple("result:" + p["proposal_id"] + ":" + str(p["attempt"]) for p in receipts),
    )
    if unknown:
        return StepExecutionOutcome(status="EFFECT_UNKNOWN", **common,
            unresolved_effect_refs=tuple(event.event_id for event in unknown),
            reason="EFFECT_UNKNOWN: runtime reconciliation is required")
    if state is None:
        return StepExecutionOutcome(status="FAILED", **common,
                                    reason=str(error or "runtime produced no committed state"))
    result_contract = ResultContract.model_validate_json(step.result_contract_json)
    if result_contract.result_channel == 'work_result':
        from bioma.core.scientific_capabilities import WorkResult
        initialized = next((event for event in events if event.kind == "RunInitialized"), None)
        inherited_ids = ({row["id"] for row in initialized.payload.get("after", {}).get("observations", [])}
                         if initialized else set())
        results = [(item, WorkResult.model_validate(item.value)) for item in state.observations
                   if item.kind == 'work_result' and item.id not in inherited_ids]
        results = [(item, work) for item, work in results
                   if work.family == result_contract.responsibility and work.kind in result_contract.evidence_kinds]
        if state.status == 'completed' and results:
            return StepExecutionOutcome(status='ACCEPTED', **common,
                result_refs=tuple(item.id for item, _ in results),
                artifact_refs=tuple(item.id for item in state.artifacts),
                accepted_result_classes=tuple(sorted({work.kind for _, work in results})),
                reason='Operational result accepted; scientific qualification remains a separate runtime decision')
        return StepExecutionOutcome(status='FAILED' if error else 'BLOCKED', **common,
            reason=str(error or state.status_reason or 'No verified provider result'))
    accepted, evidence, artifacts = _accepted_result(state, result_contract)
    if accepted:
        return StepExecutionOutcome(status="ACCEPTED", **common,
            evidence_refs=tuple(item.id for item in evidence),
            artifact_refs=tuple(item.id for item in artifacts),
            accepted_result_classes=tuple(sorted({item.kind for item in evidence})))
    status = "FAILED" if state.status == "failed" or error else "BLOCKED"
    return StepExecutionOutcome(status=status, **common,
        evidence_refs=tuple(item.id for item in state.evidence),
        artifact_refs=tuple(item.id for item in state.artifacts),
        reason=str(error or state.status_reason or "process returned without an accepted result"))
