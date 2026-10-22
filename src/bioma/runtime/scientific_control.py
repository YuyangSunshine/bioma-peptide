"""Persistent receding-horizon orchestration over the existing governed runtime."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Literal

from pydantic import Field

from bioma.core.models import ScientificState, ScientificTask
from bioma.core.research import contract_for, evidence_snapshot, prepare_contract
from bioma.core.research_planning import plan_for
from bioma.scientific_control.controller import FiniteHorizonController
from bioma.scientific_control.executor import (OptionExecutor, OptionLifecycle,
                                                new_execution_record)
from bioma.scientific_control.forecast import decision_snapshot
from bioma.scientific_control.forecasting import RuleOutcomeForecaster
from bioma.scientific_control.generation import (EvidenceDrivenOptionGenerator,
                                                 validate_generated_options, action_information_key, completed_action)
from bioma.scientific_control.grounding import ground_option
from bioma.scientific_control.progress import contract_progress
from bioma.scientific_control.relations import FrozenModel
from bioma.scientific_control.revision import RevisionTrigger
from bioma.scientific_control.revision_policy import (BoundedRevisionPolicy,
    RevisionTriggerEvent, apply_revision_proposal)


class ControlTerminal(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    NEEDS_INPUT = "NEEDS_INPUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FAILED = "FAILED"
    SUSPENDED = "SUSPENDED"


class ControlBudget(FrozenModel):
    max_control_cycles: int = Field(default=8, ge=1)
    max_option_proposals_per_cycle: int = Field(default=32, ge=1)
    max_grounding_candidates_per_cycle: int = Field(default=32, ge=1)
    max_forecast_calls: int = Field(default=128, ge=1)
    rollout_horizon: Literal[1, 2, 3] = 2
    max_revision_attempts: int = Field(default=8, ge=0)


class ConsumedControlBudget(FrozenModel):
    cycles: int = 0
    proposals: int = 0
    groundings: int = 0
    forecasts: int = 0
    revisions: int = 0


def _terminal_state(state, outcome: ControlTerminal, reason: str):
    status = ("completed" if outcome is ControlTerminal.COMPLETED else
              "failed" if outcome is ControlTerminal.FAILED else "blocked")
    return state.model_copy(update={"status": status, "status_reason": {
        **state.status_reason, "control_outcome": outcome.value, "message": reason}})


from bioma.runtime.projections import merge_committed_child as _merge_committed


class ScientificControlRuntime:
    """Generate→Ground→Forecast→Select→Execute→Commit→Revise, with no fallback."""

    def __init__(self, runtime, *, option_generator=None, outcome_forecaster=None,
                 controller=None, revision_policy=None, budget=None, result_qualifier=None, completion_policy=None,
                 clock: Callable[[], datetime] | None = None):
        if runtime.ledger is None:
            raise ValueError("adaptive_v3 requires a persistent ledger")
        self.runtime = runtime
        self.option_generator = option_generator or EvidenceDrivenOptionGenerator()
        self.outcome_forecaster = outcome_forecaster or RuleOutcomeForecaster()
        self.controller = controller or FiniteHorizonController()
        self.revision_policy = revision_policy or BoundedRevisionPolicy()
        self.budget = budget or ControlBudget()
        self.result_qualifier = result_qualifier
        self.completion_policy = completion_policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _append(self, run_id, kind, payload, key):
        return self.runtime.ledger.append(run_id, kind, "scientific-controller",
            payload, idempotency_key=key)

    def _checkpoint(self, task_id, state, consumed, cycle, terminal=None):
        self._append(task_id, "AdaptiveControlCheckpoint", {
            "state": state.model_dump(mode="json"),
            "consumed": consumed.model_dump(mode="json"), "next_cycle": cycle,
            "terminal": terminal.value if terminal else None,
        }, f"adaptive-checkpoint:{cycle}:{terminal.value if terminal else 'running'}")

    @staticmethod
    def _public_task(value):
        value = dict(value)
        gateway = dict(value.get("gateway", {}))
        for key in ("research_contract", "acceptance_protocol", "research_plan",
                    "research_plan_ref"):
            gateway.pop(key, None)
        value["gateway"] = gateway
        return value

    def _resume(self, task):
        events = self.runtime.ledger.events(task.id)
        bootstrap = next((e for e in events
                          if e.kind == "AdaptiveContractPlanBootstrapped"), None)
        if bootstrap and self._public_task(bootstrap.payload["task"]) != self._public_task(
                task.model_dump(mode="json")):
            raise ValueError("Task ID reused with different adaptive-control inputs")
        rows = [e for e in events
                if e.kind == "AdaptiveControlCheckpoint"]
        if not rows:
            return None
        payload = rows[-1].payload
        return (ScientificState.model_validate(payload["state"]),
                ConsumedControlBudget.model_validate(payload["consumed"]),
                payload["next_cycle"], payload.get("terminal"))

    def run(self, task: ScientificTask, *, event_sink=None) -> ScientificState:
        if task.external_work_results and task.gateway.get('scoped_capability_context'):
            raise ValueError('Scoped work must use parent-admitted input references; external admission is a root input boundary')
        resumed = self._resume(task)
        if resumed:
            state, consumed, start_cycle, terminal = resumed
            if terminal:
                return state
        else:
            prepared = prepare_contract(task)
            state = ScientificState.from_task(prepared)
            if prepared.external_work_results:
                from bioma.runtime.external_work_results import import_external_work_results
                imported = import_external_work_results(prepared.external_work_results)
                state = state.model_copy(update={"observations": imported})
                for row in imported:
                    source_id = row.value["provenance"]["external_import"]["source_work_result_id"]
                    source = next(ref for ref in prepared.external_work_results
                                  if ref.work_result_id == source_id)
                    self._append(task.id, "ExternalWorkResultImported", {
                        "observation": row.model_dump(mode="json"),
                        "source_reference": source.model_dump(mode="json")},
                        "external-work-result:" + row.id)
            contract, plan = contract_for(state), plan_for(state)
            if contract is None or plan is None:
                return _terminal_state(state, ControlTerminal.FAILED,
                                       "adaptive_v3 requires ResearchContract v2 and ResearchPlan v2")
            consumed, start_cycle = ConsumedControlBudget(), 0
            self._append(task.id, "AdaptiveContractPlanBootstrapped", {
                "contract_ref": contract.identity, "contract_version": contract.version,
                "plan_identity": list(plan.version_identity),
                "task": prepared.model_dump(mode="json")}, "adaptive-bootstrap")
            self._checkpoint(task.id, state, consumed, start_cycle)

        for cycle in range(start_cycle, self.budget.max_control_cycles):
            contract, plan = contract_for(state), plan_for(state)
            if contract is None or plan is None:
                return self._finish(task.id, state, consumed, cycle,
                                    ControlTerminal.FAILED, "contract or plan missing")
            self.runtime.refresh_providers()
            unresolved = tuple(state.status_reason.get("unresolved_effect_refs", ()))
            remaining = max(0, self.budget.max_control_cycles - consumed.cycles)
            # A request may fail after its snapshot was committed but before the
            # next checkpoint. Reuse that instant; recompute all other fields so
            # changed inputs still trigger the Ledger's idempotency protection.
            snapshots = [e for e in self.runtime.ledger.events(task.id)
                if e.kind == "DecisionSnapshot" and
                (e.idempotency_key == f"decision-snapshot:{cycle}" or
                 e.idempotency_key.startswith(f"decision-snapshot:{cycle}:"))]
            saved_snapshot = snapshots[-1] if snapshots else None
            snapshot_time = self.clock()
            if saved_snapshot:
                from bioma.scientific_control.forecast import DecisionSnapshot
                snapshot_time = DecisionSnapshot.model_validate(saved_snapshot.payload).timestamp
            snapshot = decision_snapshot(contract=contract, plan=plan, state=state,
                registry=self.runtime.capability_registry,
                active_agents=set(self.runtime.agents),
                available_resources=self.runtime.available_resources,
                unresolved_effect_refs=unresolved, remaining_budget=float(remaining),
                timestamp=snapshot_time, provenance_refs=(f"control-cycle:{cycle}",))
            snapshot_key = saved_snapshot.idempotency_key if saved_snapshot else f"decision-snapshot:{cycle}"
            if saved_snapshot and saved_snapshot.payload != snapshot.model_dump(mode="json"):
                changed = [key for key, value in snapshot.model_dump(mode="json").items()
                           if saved_snapshot.payload.get(key) != value]
                proposed_already = any(e.idempotency_key == f"option-proposals:{cycle}"
                    or (e.kind == "OptionProposal" and e.payload.get("snapshot_ref") ==
                        DecisionSnapshot.model_validate(saved_snapshot.payload).identity)
                    for e in self.runtime.ledger.events(task.id))
                if not set(changed) <= {"runtime_fingerprint", "available_resources"} or proposed_already:
                    raise ValueError("Resume snapshot inputs changed: " + ", ".join(changed))
                # Refresh only an unproposed cycle; preserve its prior snapshot.
                snapshot_key = f"decision-snapshot:{cycle}:runtime:{snapshot.runtime_fingerprint}"
            self._append(task.id, "DecisionSnapshot", snapshot.model_dump(mode="json"), snapshot_key)
            progress = contract_progress(contract, state)
            if (progress.terminal_mandatory_total and
                    progress.terminal_mandatory_credited == progress.terminal_mandatory_total and
                    self._completion_allowed(state, contract)):
                return self._finish(task.id, state, consumed, cycle,
                                    ControlTerminal.COMPLETED,
                                    "all mandatory terminal contract obligations are credited")
            if unresolved:
                return self._finish(task.id, state, consumed, cycle,
                                    ControlTerminal.SUSPENDED,
                                    "unresolved effect requires reconciliation")

            capabilities = self.runtime.capability_registry.all()
            from bioma.runtime.telemetry import observe, notify_observer
            def proposal_telemetry(event):
                self._append(task.id, "LLMTelemetry", event,
                    "llm-telemetry:" + str(cycle) + ":" + str(event.get("type")) + ":" +
                    str(event.get("timestamp")))
                notify_observer(event_sink, event)
            with observe(proposal_telemetry, agent="scientific-controller",
                    phase="scientific_option_proposal", task_id=task.id, control_cycle=cycle):
                proposed = self.option_generator.generate(
                    snapshot, contract, plan, state, capabilities)
            audit = getattr(self.option_generator, "last_audit", None)
            if audit:
                self._append(task.id, getattr(self.option_generator, "audit_event_kind", "ScientificOptionProposalAudit"), audit,
                    f"scientific-option-proposal-audit:{cycle}")
            proposed = validate_generated_options(proposed, snapshot=snapshot,
                contract=contract, plan=plan)[:self.budget.max_option_proposals_per_cycle]
            consumed = consumed.model_copy(update={"cycles": consumed.cycles + 1,
                                                    "proposals": consumed.proposals + len(proposed)})
            self._append(task.id, "OptionProposal", {
                "snapshot_ref": snapshot.identity,
                "options": [row.model_dump(mode="json") for row in proposed]},
                f"option-proposals:{cycle}")
            if not proposed:
                state=state.model_copy(update={'status_reason':{**state.status_reason,
                    'option_generation_diagnostic':audit or {'termination_cause':'NO_OPTIONS_FROM_GENERATOR'}}})
                return self._finish(task.id, state, consumed, cycle + 1,
                                    ControlTerminal.BLOCKED, "no admissible scientific option")

            grounded, option_by_grounding = [], {}
            unavailable_bindings = state.status_reason.get("control_failed_bindings", ())
            failed_bindings = {tuple(row) for row in unavailable_bindings}
            assessors = {peer: agent.assess for peer, agent in self.runtime.agents.items()}
            grounding_rows = []
            for option in proposed:
                result = ground_option(option, contract=contract, plan=plan, state=state,
                    registry=self.runtime.capability_registry, assessors=assessors,
                    active_agents=set(self.runtime.agents),
                    available_resources=self.runtime.available_resources)
                grounding_rows.append({"option_ref": option.identity,
                    "result": result.model_dump(mode="json")})
                for candidate in result.alternatives:
                    if len(grounded) >= self.budget.max_grounding_candidates_per_cycle:
                        break
                    if any((step.provider_id, step.method_id) in failed_bindings
                           for step in candidate.steps):
                        continue
                    if len(candidate.steps) == 1 and completed_action(state,
                            option.target_contract_obligation_refs, candidate.steps[0].provider_id,
                            candidate.steps[0].method_id, json.loads(candidate.steps[0].input_bindings_json)):
                        self._append(task.id, 'RepeatedScientificActionSkipped', {
                            'option_ref': option.identity, 'grounding_ref': candidate.identity,
                            'reason': 'Completed same obligation, Method and inputs with no new information'},
                            'repeat-skip:' + str(cycle) + ':' + candidate.identity)
                        continue
                    grounded.append(candidate)
                    option_by_grounding[candidate.identity] = option
            consumed = consumed.model_copy(update={"groundings": consumed.groundings + len(grounded)})
            self._append(task.id, "Grounding", {"snapshot_ref": snapshot.identity,
                "results": grounding_rows}, f"groundings:{cycle}")
            if not grounded:
                needs_input = any(any(code in {"RESOURCE_UNAVAILABLE", "INPUT_UNAVAILABLE"}
                    for code in row["result"].get("failures", ())) for row in grounding_rows)
                return self._finish(task.id, state, consumed, cycle + 1,
                    ControlTerminal.NEEDS_INPUT if needs_input else ControlTerminal.BLOCKED,
                    "no executable grounding")

            forecasts = []
            for candidate in grounded:
                if consumed.forecasts + len(forecasts) >= self.budget.max_forecast_calls:
                    break
                forecasts.append(self.outcome_forecaster.forecast(snapshot,
                    option_by_grounding[candidate.identity], candidate, contract, state))
            consumed = consumed.model_copy(update={"forecasts": consumed.forecasts + len(forecasts)})
            self._append(task.id, "OutcomeForecast", {"snapshot_ref": snapshot.identity,
                "forecasts": [row.model_dump(mode="json") for row in forecasts]},
                f"forecasts:{cycle}")
            if not forecasts:
                return self._finish(task.id, state, consumed, cycle + 1,
                                    ControlTerminal.DEFERRED, "forecast backend unavailable or budget exhausted")

            decision = self.controller.decide(snapshot, tuple(grounded), tuple(forecasts),
                horizon=self.budget.rollout_horizon,
                current_snapshot_identity=snapshot.identity)
            self._append(task.id, "ControllerDecision", decision.model_dump(mode="json"),
                         f"controller-decision:{cycle}")
            if not decision.selected_grounded_option_identity:
                outcome = ControlTerminal.DEFERRED if decision.status == "DEFER" else ControlTerminal.BLOCKED
                return self._finish(task.id, state, consumed, cycle + 1, outcome, decision.reason)

            selected = next(row for row in grounded
                            if row.identity == decision.selected_grounded_option_identity)
            option = option_by_grounding[selected.identity]
            execution_id = "execution:" + hashlib.sha256(
                f"{task.id}:{cycle}:{selected.identity}".encode()).hexdigest()
            record = new_execution_record(option, selected,
                execution_instance_id=execution_id, now=self.clock())
            from bioma.llm import ModelBudgetUnavailable
            try:
                record = OptionExecutor(self.runtime, clock=self.clock).execute(option, selected,
                    contract=contract, plan=plan, record=record)
            except ModelBudgetUnavailable as exc:
                self._append(task.id, "OptionExecutionDeferred", {
                    "execution_ref": execution_id, "option_ref": option.identity,
                    "grounding_ref": selected.identity, "reason": str(exc),
                    "child_task_refs": ["option." + hashlib.sha256(
                        f"{execution_id}:{step.step_id}".encode()).hexdigest()
                        for step in selected.steps],
                }, f"option-execution-deferred:{cycle}")
                # Earlier steps may have committed work before a later dispatch
                # hit the allowance. Keep those results in the parent checkpoint.
                for step in selected.steps:
                    child_id = "option." + hashlib.sha256(
                        f"{execution_id}:{step.step_id}".encode()).hexdigest()
                    child = self.runtime.ledger.replay_state(child_id)
                    if child is not None:
                        state = _merge_committed(state, child)
                return self._finish(task.id, state, consumed, cycle + 1,
                    ControlTerminal.BUDGET_EXHAUSTED,
                    "dispatch allowance unavailable during option execution: " + str(exc))
            before_execution_state = state
            # Committed execution feedback belongs to the next decision even if
            # scientific qualification is unavailable, rejected or interrupted.
            child_states = {}
            for step in selected.steps:
                child_id = "option." + hashlib.sha256(
                    f"{execution_id}:{step.step_id}".encode()).hexdigest()
                child = self.runtime.ledger.replay_state(child_id)
                if child is not None:
                    child_states[child_id] = child
                    state = _merge_committed(state, child)
            if record.lifecycle is OptionLifecycle.SUSPENDED:
                self._observe_execution(task.id, cycle, record)
                self._append(task.id, "OptionExecutionRecord", record.model_dump(mode="json"),
                             f"option-execution:{cycle}")
                state = state.model_copy(update={"status_reason": {
                    **state.status_reason, "unresolved_effect_refs": list(record.unresolved_effect_refs)}})
                return self._finish(task.id, state, consumed, cycle + 1,
                                    ControlTerminal.SUSPENDED, record.status_reason)
            if record.lifecycle is OptionLifecycle.COMPLETED:
                qualified_refs = []
                qualification_errors = []
                for child_id, child in child_states.items():
                    child_refs = tuple(ref for ref in record.result_refs
                        if any(o.id == ref and o.kind == 'work_result' for o in child.observations))
                    if self.result_qualifier is not None and child_refs:
                        before_events = len(self.runtime.ledger.events(child_id))
                        try:
                            qualified_refs.extend(self.result_qualifier.qualify(
                                self.runtime, child_id, child_refs))
                        except ModelBudgetUnavailable as exc:
                            # Preserve execution and any already committed qualification;
                            # a review budget stop is not a scientific execution failure.
                            state = _merge_committed(state, self.runtime.ledger.replay_state(child_id))
                            self._append(task.id, "WorkResultQualificationDeferred", {
                                "execution_ref": execution_id, "result_refs": list(record.result_refs),
                                "reason": str(exc)}, f"qualification-deferred:{cycle}")
                            self._observe_execution(task.id, cycle, record)
                            self._append(task.id, "OptionExecutionRecord", record.model_dump(mode="json"),
                                         f"option-execution:{cycle}")
                            return self._finish(task.id, state, consumed, cycle + 1,
                                ControlTerminal.BUDGET_EXHAUSTED,
                                "execution retained; scientific qualification allowance unavailable: " + str(exc))
                        qualification_errors.extend(event.payload for event in
                            self.runtime.ledger.events(child_id)[before_events:]
                            if event.kind == 'WorkResultQualificationError')
                        state = _merge_committed(state, self.runtime.ledger.replay_state(child_id))
                if record.result_refs:
                    qualification_status = ("PARTIALLY_QUALIFIED" if qualified_refs and qualification_errors else
                        "QUALIFIED" if qualified_refs else "ERROR" if qualification_errors else
                        "REJECTED" if self.result_qualifier is not None else "NOT_CONFIGURED")
                    self._append(task.id, "WorkResultQualificationOutcome", {
                        "execution_ref": execution_id, "result_refs": list(record.result_refs),
                        "qualified_evidence_refs": sorted(set(qualified_refs)),
                        "status": qualification_status,
                        "qualification_model": getattr(self.result_qualifier, "model_id", None),
                        "errors": qualification_errors,
                    }, f"work-result-qualification:{cycle}")
                if qualified_refs:
                    record = record.model_copy(update={"evidence_refs": tuple(sorted(set(
                        (*record.evidence_refs, *qualified_refs))))})
            self._observe_execution(task.id, cycle, record)
            self._append(task.id, "OptionExecutionRecord", record.model_dump(mode="json"),
                         f"option-execution:{cycle}")

            if record.lifecycle is OptionLifecycle.COMPLETED and len(selected.steps) == 1:
                step = selected.steps[0]
                key = action_information_key(state, option.target_contract_obligation_refs,
                    step.provider_id, step.method_id, json.loads(step.input_bindings_json))
                completed = dict(state.status_reason.get('control_completed_actions', {}))
                completed[key] = {'execution_ref': record.execution_instance_id,
                    'provider_id': step.provider_id, 'method_id': step.method_id,
                    'obligation_refs': list(option.target_contract_obligation_refs),
                    'result_refs': list(record.result_refs)}
                state = state.model_copy(update={'status_reason': {
                    **state.status_reason, 'control_completed_actions': completed}})

            # Operational feedback is not scientific Evidence. Preserve blocked
            # receipts too: an unchanged scientific state must not erase why the
            # previous action failed to obtain an accepted result.
            feedback = list(state.status_reason.get("control_execution_feedback", ()))
            feedback.append({"execution_ref": record.execution_instance_id,
                "lifecycle": record.lifecycle.value, "reason": record.status_reason,
                "receipt_refs": list(record.receipt_refs),
                "evidence_refs": list(record.evidence_refs),
                "result_refs": list(record.result_refs),
                "qualification_errors": qualification_errors if record.lifecycle is OptionLifecycle.COMPLETED else [],
                "bindings": [[step.provider_id, step.method_id] for step in selected.steps]})
            state = state.model_copy(update={"status_reason": {
                **state.status_reason, "control_execution_feedback": feedback[-8:]}})

            cause = (RevisionTrigger.OPTION_COMPLETED if record.lifecycle is OptionLifecycle.COMPLETED
                     else RevisionTrigger.EXECUTION_FAILURE if record.lifecycle is OptionLifecycle.FAILED
                     else RevisionTrigger.OPTION_BLOCKED)
            details = {}
            authoritative_refs = tuple(record.evidence_refs or record.receipt_refs or (execution_id,))
            if record.lifecycle is OptionLifecycle.COMPLETED:
                before_progress = contract_progress(contract, before_execution_state)
                after_progress = contract_progress(contract, state)
                before_rows = {row.obligation_ref: row for row in before_progress.obligations}
                after_rows = {row.obligation_ref: row for row in after_progress.obligations}
                targets = set(option.target_contract_obligation_refs)
                changed = {ref for ref in targets if before_rows.get(ref) != after_rows.get(ref)}
                new_evidence = tuple(sorted({item.id for item in state.evidence} -
                                            {item.id for item in before_execution_state.evidence}))
                contradictory = any(after_rows[ref].status == "CONFLICTED" for ref in targets
                                    if ref in after_rows)
                if contradictory:
                    cause = RevisionTrigger.CONTRADICTION
                elif new_evidence or changed:
                    cause = RevisionTrigger.EVIDENCE_UPDATE
                authoritative_refs = new_evidence or authoritative_refs
                details.update({
                    "new_evidence_refs": list(new_evidence),
                    "changed_obligation_refs": sorted(changed),
                    "before_obligations": {ref: before_rows[ref].model_dump(mode="json")
                                            for ref in sorted(targets) if ref in before_rows},
                    "after_obligations": {ref: after_rows[ref].model_dump(mode="json")
                                           for ref in sorted(targets) if ref in after_rows},
                    "before_state_ref": snapshot.committed_state_ref,
                    "after_evidence_snapshot_ref": evidence_snapshot(state),
                })
                self._append(task.id, "AuthoritativeScientificStateTransition", {
                    "execution_ref": execution_id, "cause": cause.value, **details},
                    f"scientific-state-transition:{cycle}")
            if cause is RevisionTrigger.EXECUTION_FAILURE:
                details["execution_failure"] = {
                    "execution_ref": record.execution_instance_id,
                    "reason": record.status_reason,
                    "attempt_refs": list(record.attempt_refs),
                    "failed_step": record.current_step_id,
                }
                failed = list(state.status_reason.get("control_failed_bindings", ()))
                failed.extend([step.provider_id, step.method_id] for step in selected.steps)
                state = state.model_copy(update={"status_reason": {
                    **state.status_reason, "control_failed_bindings": failed,
                    "last_execution_failure": details["execution_failure"]}})
                node = next((n for n in plan.nodes
                    if n.node_id in option.target_plan_node_refs), None)
                replacement = next((b for b in (node.candidate_bindings if node else ())
                    if b.method_id != selected.steps[0].method_id or
                       (b.provider_id and b.provider_id != selected.steps[0].provider_id)), None)
                if replacement:
                    details["replacement_binding"] = replacement.model_dump(mode="json")
            trigger_id = "trigger:" + hashlib.sha256(
                f"{execution_id}:{cause.value}".encode()).hexdigest()
            trigger = RevisionTriggerEvent(trigger_id=trigger_id, cause=cause,
                contract_ref=contract.identity, plan_identity=plan.version_identity,
                decision_snapshot_identity=snapshot.identity,
                authoritative_refs=authoritative_refs,
                affected_node_refs=option.target_plan_node_refs,
                details_json=json.dumps(details), created_at=self.clock(),
                provenance_refs=(record.execution_instance_id,))
            self._append(task.id, "RevisionTrigger", trigger.model_dump(mode="json"),
                         f"revision-trigger:{cycle}")
            proposal = None
            revision_called = False
            if consumed.revisions < self.budget.max_revision_attempts:
                if hasattr(self.revision_policy, "observe_state"):
                    self.revision_policy.observe_state(state)
                revision_called = True
                from bioma.llm import ModelBudgetUnavailable
                try:
                    proposal = self.revision_policy.propose(trigger, snapshot, contract, plan)
                except ModelBudgetUnavailable as exc:
                    self._append(task.id, "PlanRevisionDeferred", {
                        "reason": str(exc), "trigger_ref": trigger.trigger_id,
                        "plan_identity": list(plan.version_identity),
                        "state_version": state.version,
                    }, f"plan-revision-deferred:{cycle}")
                    return self._finish(task.id, state, consumed, cycle + 1,
                        ControlTerminal.BUDGET_EXHAUSTED,
                        "model allowance unavailable during plan revision: " + str(exc))
                consumed = consumed.model_copy(update={"revisions": consumed.revisions + 1})
            if proposal is not None:
                revision, plan = apply_revision_proposal(proposal, trigger=trigger,
                    snapshot=snapshot, contract=contract, plan=plan)
                gateway = dict(state.gateway)
                gateway["research_plan"] = plan.model_dump(mode="json")
                gateway["research_plan_ref"] = list(plan.version_identity)
                state = state.model_copy(update={"gateway": gateway})
                self._append(task.id, "PlanRevision", {"proposal": proposal.model_dump(mode="json"),
                    "revision": revision.model_dump(mode="json"),
                    "plan_identity": list(plan.version_identity)}, f"plan-revision:{cycle}")
            else:
                self._append(task.id, "PlanRevision", {"retained": True,
                    "plan_identity": list(plan.version_identity),
                    "policy_called": revision_called,
                    "reason": ("bounded policy found no justified edit" if revision_called
                               else "revision attempt allowance exhausted or disabled")}, f"plan-revision:{cycle}")
            self._checkpoint(task.id, state, consumed, cycle + 1)

            # Evidence admitted by the final permitted execution must receive the
            # same completion decision as evidence admitted in an earlier cycle.
            # Without this post-execution check, a contract completed on the last
            # cycle was incorrectly reported as budget exhausted.
            progress = contract_progress(contract, state)
            if (cycle + 1 >= self.budget.max_control_cycles and
                    progress.terminal_mandatory_total and
                    progress.terminal_mandatory_credited == progress.terminal_mandatory_total and
                    self._completion_allowed(state, contract)):
                return self._finish(task.id, state, consumed, cycle + 1,
                                    ControlTerminal.COMPLETED,
                                    "all mandatory terminal contract obligations are credited")

        return self._finish(task.id, state, consumed, self.budget.max_control_cycles,
                            ControlTerminal.BUDGET_EXHAUSTED,
                            "control-cycle budget exhausted before contract completion")

    def _completion_allowed(self, state, contract):
        if self.completion_policy is None:
            return not state.gateway.get('goal_completion_policy_required', False)
        allowed = bool(self.completion_policy.allow_completion(state, contract))
        self._append(state.task_id, 'GoalCompletionDecision', {'allowed': allowed,
            'contract_ref': contract.identity, 'state_version': state.version},
            'goal-completion:' + str(state.version))
        return allowed

    def _observe_execution(self, task_id, cycle, record):
        for kind, refs in (("WP1PermitObserved", record.permit_refs),
                           ("ExecutionReceiptObserved", record.receipt_refs),
                           ("ScientificCommitObserved", record.evidence_refs)):
            self._append(task_id, kind, {"execution_ref": record.execution_instance_id,
                "refs": list(refs)}, f"{kind}:{cycle}")

    def _finish(self, task_id, state, consumed, cycle, outcome, reason):
        terminal = _terminal_state(state, outcome, reason)
        self._checkpoint(task_id, terminal, consumed, cycle, outcome)
        self._append(task_id, "ScientificControlTerminated", {"outcome": outcome.value,
            "reason": reason, "consumed_budget": consumed.model_dump(mode="json")},
            "adaptive-terminal")
        return terminal
