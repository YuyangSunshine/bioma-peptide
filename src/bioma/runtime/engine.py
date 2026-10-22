from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from bioma.runtime.deadline import DEFAULT_EXECUTION_SECONDS
from bioma.capabilities import CapabilityRegistry, CapabilityRouter
from bioma.core.deliverables import delivery_status
from bioma.core.harness import AgentHarness, ScopeViolation, runtime_recovery_action
from bioma.core.models import (
    AgentMessage,
    Evidence,
    ScientificState,
    ScientificTask,
    StateDelta,
    TaskRequest,
    utc_now,
)
from bioma.core.verification import VerificationEngine, VerificationError
from bioma.runtime.telemetry import observe
from bioma.runtime.ledger import RunLedger


def _budget_pause_seconds(events, now):
    """Exclude only journaled budget-wait intervals; preserve execution elapsed."""
    from datetime import datetime
    paused = None
    seconds = 0.0
    for event in events:
        timestamp = datetime.fromisoformat(event.created_at).timestamp()
        if event.kind == 'RuntimeBudgetPaused' and paused is None:
            paused = timestamp
        elif event.kind == 'RuntimeBudgetResumed' and paused is not None:
            seconds += max(0.0, timestamp - paused)
            paused = None
    return seconds + (max(0.0, now-paused) if paused is not None else 0.0), paused is not None


class RuntimeErrorBase(RuntimeError):
    pass


class UnknownAgentError(RuntimeErrorBase):
    pass


class RuntimeDeadlockError(RuntimeErrorBase):
    pass


class RuntimeLimitError(RuntimeErrorBase):
    pass


class RoutingIntegrityError(RuntimeErrorBase):
    pass


RuntimeEventSink = Callable[[dict[str, Any]], None]


@dataclass
class _RunContext:
    state: ScientificState | None = None
    message: AgentMessage | None = None
    event_sink: RuntimeEventSink | None = None


def _pending_deliverables(deliverables: list[dict[str, Any]], state: ScientificState) -> list[str]:
    return [item["id"] for item in delivery_status(deliverables, state) if item["status"] != "verified"]


def _recovery_action(exc: Exception, attempt: int) -> str:
    """Recovery policy for one failed attempt.

    Under the experimental failure-resilience ablation nothing is retried or
    repaired: every failure is an immediate terminal outcome. The returned
    sentinel is neither `retry`/`repair` nor `blocked`, so the caller re-raises.
    """
    from bioma.core.protocol_policy import active_policy
    if not active_policy().failure_resilience:
        return 'fail'
    return runtime_recovery_action(exc, attempt)


def _collapse_proposal(decision):
    """Under the experimental decentralized-proposal ablation the runtime
    dispatches one provider directly, so no offer and no commitment exists."""
    from bioma.core.protocol_policy import active_policy
    if active_policy().decentralized_proposal:
        return decision
    from bioma.capabilities.router import RouteDecision
    return RouteDecision(decision.request, (), (), decision.messages[:1],
                         decision.qualification)


class DecentralizedRuntime:
    """A non-agent event loop for peer delivery and verified state commits."""

    def __init__(
        self,
        agents: Iterable[Any],
        *,
        max_events: int | None = None,
        verifier: VerificationEngine | None = None,
        harness: AgentHarness | None = None,
        available_resources: Iterable[str] | None = None,
        task_deadline_seconds: float = DEFAULT_EXECUTION_SECONDS,
        ledger_path: str | Path | None = ".bioma/run-ledger.sqlite3",
        provider_trust=None,
        provider_principal=None,
        memory_service=None,
        method_condition: str = "v2_d0",
        capability_authorizations: Iterable[tuple[str, str, str]] = (),
        support_use_resources=(),
    ) -> None:
        from bioma.runtime.support_use import SupportUseResource
        self.support_use_resources = tuple(support_use_resources)
        if any(not isinstance(r, SupportUseResource) for r in self.support_use_resources):
            raise ValueError("Support-use resources must be trusted host bindings")
        agent_list = list(agents)
        if method_condition != "v2_d0":
            raise ValueError("method_condition must be v2_d0")
        self.method_condition = method_condition
        self.capability_authorizations = frozenset(tuple(item) for item in capability_authorizations)
        if any(len(item) != 3 or any(not isinstance(v, str) or not v for v in item)
               for item in self.capability_authorizations):
            raise ValueError("Capability authorizations require explicit (task_id, family, method_id) triples")
        self._agent_implementations = agent_list[:]
        # Registration validates the executable contract; governance does not
        # depend on a concrete scientific implementation or historical role base.
        self.agents: dict[str, Any] = {}
        for agent in agent_list:
            for peer_id in agent.peer_ids:
                if peer_id in self.agents:
                    raise ValueError(f"Duplicate peer id: {peer_id!r}.")
                self.agents[peer_id] = agent
        self.verifier = verifier or VerificationEngine()
        self.harness = harness or AgentHarness()
        self.capability_registry = CapabilityRegistry(
            capability for agent in agent_list for capability in agent.capabilities
        )
        self.capability_router = CapabilityRouter(self.capability_registry,
            {peer: agent.assess for peer, agent in self.agents.items()})
        self.available_resources = (
            None if available_resources is None else set(available_resources)
        )
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be positive.")
        self.max_events = max_events
        self.task_deadline_seconds = task_deadline_seconds
        self.ledger = RunLedger(ledger_path) if ledger_path is not None else None
        from bioma.memory import MemoryService
        self.memory = memory_service or (MemoryService(self.ledger.path.with_name(self.ledger.path.stem + '-memory.sqlite3'))
                                         if self.ledger else None)
        from bioma.runtime.workers import VersionedProviderPool
        self.provider_pool = VersionedProviderPool(
            self.ledger.path.with_name(self.ledger.path.stem + '-evaluations.sqlite3') if self.ledger else None, trust=provider_trust)
        for agent in agent_list:
            self.register_provider(agent, stage='stable', principal=provider_principal)

    def register_provider(self, agent, *, stage='shadow', principal=None):
        if any(c.__module__ == 'bioma.node' and c.__name__ == 'RemoteAgent' for c in type(agent).__mro__):
            raise ValueError('RemoteAgent production execution is BLOCKED: direct-backend provenance is not established')
        if any(c.__module__ in {'bioma.agents.queryagent', 'bioma.agents.predagent', 'bioma.agents.comagent', 'bioma.agents.proagent', 'bioma.agents.operagent'} for c in type(agent).__mro__):
            raise ValueError('Legacy role execution is retired; use a direct MethodBinding')
        if agent not in self._agent_implementations:
            self._agent_implementations.append(agent)
        from bioma.runtime.workers import CapabilityProvider
        for peer_id in agent.peer_ids:
            if peer_id in self.agents and self.agents[peer_id] is not agent:
                raise ValueError("Provider replacement requires a distinct versioned peer identity")
        pool = self.provider_pool
        previous = (dict(pool._providers), dict(pool._manifests), dict(pool._evaluations))
        try:
            for capability in agent.capabilities:
                def execute(case, peer=agent):
                    from bioma.runtime.projections import LocalAgentView
                    return peer.handle(LocalAgentView.model_validate(case['view']),
                        AgentMessage.model_validate(case['message'])).model_dump(mode='json')
                pool.register(CapabilityProvider(capability.agent_id,
                    getattr(agent, 'provider_kind', 'role'), capability, execute, release_stage=stage), principal=principal)
            self.refresh_providers()
        except Exception:
            pool._providers, pool._manifests, pool._evaluations = previous
            raise
        for peer_id in agent.peer_ids:
            self.agents[peer_id] = agent

    def refresh_providers(self):
        self.capability_registry = CapabilityRegistry(self.provider_pool.capabilities())
        self.capability_router = CapabilityRouter(self.capability_registry,
            {peer: agent.assess for peer, agent in self.agents.items()})

    def _sync_remote_memory(self, task_id):
        if not self.ledger or not any(hasattr(agent,'confirm_commit') for agent in self.agents.values()):
            return
        events=self.ledger.events(task_id)
        acknowledged={e.payload['event_id'] for e in events if e.kind=='MemoryCommitAcknowledged'}
        for event in events:
            agent=self.agents.get(event.actor)
            if (event.kind!='TransitionCommitted' or event.event_id in acknowledged
                or not event.payload['delta'].get('transition') or not hasattr(agent,'confirm_commit')):
                continue
            try:
                agent.confirm_commit(event)
                self.ledger.append(task_id,'MemoryCommitAcknowledged','harness',{'event_id':event.event_id},
                    idempotency_key='memory-ack:'+event.event_id)
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning('Remote memory acknowledgement remains pending: %s',type(exc).__name__)


    def run(
        self, task: ScientificTask, *, event_sink: RuntimeEventSink | None = None,
        continuation: dict | None = None,
    ) -> ScientificState:
        """Run with best-effort external observers and required ledger persistence."""
        if task.external_work_results and task.gateway.get('scoped_capability_context'):
            raise ValueError('Scoped work must use parent-admitted input references; external admission is a root input boundary')
        from bioma.core.research import prepare_contract
        supplied_contract = task.gateway.get('research_contract')
        if supplied_contract and supplied_contract.get('schema_version') != 'research-contract-v2':
            raise ValueError('Historical research contracts are read-only; new execution requires research-contract-v2')
        initialized = next((e for e in self.ledger.events(task.id) if e.kind == 'RunInitialized'), None) if self.ledger else None
        if initialized and not task.gateway.get('research_contract'):
            saved_task = initialized.payload['task']
            supplied = task.model_dump(mode='json')
            saved_public = {**saved_task, 'gateway': {k: v for k, v in saved_task.get('gateway', {}).items()
                if k not in {'research_contract', 'acceptance_protocol', 'research_plan', 'research_plan_ref'}}}
            if saved_public != supplied:
                raise ValueError('Task ID reused with different inputs')
            task = ScientificTask.model_validate(saved_task)
        restored_contract = task.gateway.get('research_contract')
        if restored_contract and restored_contract.get('schema_version') != 'research-contract-v2':
            raise ValueError('Historical research contracts are read-only; use RunLedger.read_historical')
        # Never silently migrate a historical run's immutable task contract.
        governed = (bool(task.gateway) and task.initial_capability == 'research.problem_formulation') or task.gateway.get('research_contract') or task.gateway.get('acceptance_mode') in {'work', 'research'} or any(
            c.id == 'scientific.evidence_review' for a in self.agents.values() for c in a.capabilities)
        if governed and (initialized is None or initialized.payload['task'].get('gateway', {}).get('research_contract')):
            task = prepare_contract(task)
        self.refresh_providers()
        if continuation is not None and self.ledger is None:
            raise ValueError("Explicit continuation requires a durable ledger")
        if self.ledger is None:
            return self._run_bounded(task, event_sink=event_sink)
        from bioma.runtime.task_lease import task_lease
        from bioma.runtime.effects import EffectManager, effect_scope
        with task_lease(self.ledger, task.id), effect_scope(EffectManager(self.ledger), task.id):
            return self._run_bounded(task, event_sink=event_sink, continuation=continuation)

    def _run_bounded(self, task, *, event_sink=None, continuation=None):
        from bioma.runtime.deadline import deadline
        context = _RunContext(event_sink=event_sink)
        with deadline(self.task_deadline_seconds):
            try:
                return self._run(task, context=context, event_sink=event_sink, continuation=continuation)
            except Exception as exc:
                from bioma.llm import ModelBudgetUnavailable
                if isinstance(exc, ModelBudgetUnavailable):
                    if self.ledger:
                        self.ledger.append(task.id, 'RuntimeBudgetPaused', 'harness', {
                            'execution_status': getattr(exc, 'execution_status', 'BUDGET_UNAVAILABLE'),
                            'checkpoint': getattr(exc, 'checkpoint', None), 'reason': str(exc)})
                    # No scientific failure Evidence and no irreversible terminal state.
                    raise
                if context.state is not None:
                    # A commit can succeed before acknowledgement fails. The ledger
                    # decides which results and terminal status must be preserved.
                    if self.ledger:
                        context.state = self.ledger.replay_state(task.id) or context.state
                    if context.state.status == "running":
                        reason = {"code": "runtime_failure", "error_type": type(exc).__name__,
                                  "message": str(exc),
                                  "agent": context.message.recipient if context.message else "harness"}
                        failure = Evidence(kind="execution_failure", source="harness",
                            claim="Execution stopped with an unhandled runtime error.",
                            method="runtime_exception", value={**reason,
                                "source_message_id": context.message.id if context.message else None})
                        approved = self._commit_terminal(context, outcome="failed", reason=reason, evidence=[failure])
                        try:
                            self._publish_terminal(context, approved)
                        except Exception:
                            logging.getLogger(__name__).exception("Terminal state committed; notification failed")
                raise

    def _commit_terminal(self, context, *, outcome, reason, evidence=()):
        state = context.state
        message = AgentMessage(sender="harness", recipient="harness", message_type="runtime." + outcome,
                               causation_id=context.message.id if context.message else None)
        delta = StateDelta(agent_id="harness", source_message_id=message.id,
                          expected_version=state.version, outcome=outcome,
                          status_reason=reason, evidence=list(evidence))
        result, receipt = self.verifier.verify_with_receipt(state, delta,
            expected_agent="harness", source_message=message)
        if not result.ok:
            raise VerificationError(" ".join(result.reasons))
        approved = delta.model_copy(deep=True, update={"status": "verified"})
        context.state = (self.ledger.commit_transition(state, approved, message, receipt)
                         if self.ledger else state.commit(approved))
        return approved

    @staticmethod
    def _publish_terminal(context, delta):
        event = {"type": "state.committed", "agent": "harness",
                 "state_version": context.state.version, "state": context.state.model_dump(mode="json")}
        event["delta"] = delta.model_dump(mode="json")
        _emit(context.event_sink, event)

    def compensate_tool(self, task_id, effect_key, tool, *, reason):
        """Execute an explicitly selected, tool-owned compensation under the task lease."""
        if self.ledger is None or not reason or tool.compensate_effect is None:
            raise ValueError("Compensation requires a ledger, reason and tool-specific adapter")
        from bioma.runtime.task_lease import task_lease
        from bioma.runtime.effects import EffectManager
        with task_lease(self.ledger, task_id):
            completed = next((e for e in self.ledger.events(task_id)
                if e.kind == "EffectCompleted" and e.payload.get("effect_key") == effect_key), None)
            if completed is None:
                raise ValueError("Reconcile the original effect before selecting compensation")
            original = completed.payload["contract"]
            if original.get("tool") != tool.name or original.get("version") != tool.version:
                raise ValueError("Compensation tool contract mismatch")
            return EffectManager(self.ledger).compensate(task_id, effect_key,
                {"original": original, "reason": reason},
                lambda: tool.compensate_effect(completed.payload["result"]),
                reconcile=tool.reconcile_compensation)

    def _run(
        self,
        task: ScientificTask,
        *,
        context: _RunContext,
        continuation: dict | None = None,
        event_sink: RuntimeEventSink | None = None,
    ) -> ScientificState:
        # Tool results arrive through the same event channel as runtime events.
        # Keep a bounded in-memory checkpoint so a successful external lookup
        # cannot be lost when a later LLM response is malformed or incomplete.
        tool_checkpoints: list[dict[str, Any]] = []
        downstream_sink = event_sink

        def capture_event(event: dict[str, Any]) -> None:
            if event.get("type") == "tool.completed" and isinstance(event.get("observation"), dict):
                if event["observation"].get("success") is True:
                    tool_checkpoints.append(event)
            if self.ledger is not None:
                self.ledger.append(
                    task.id,
                    str(event.get("type") or "RuntimeEvent"),
                    str(event.get("agent") or "harness"),
                    event,
                )
            from bioma.runtime.telemetry import notify_observer
            notify_observer(downstream_sink, event)

        event_sink = context.event_sink = capture_event
        from bioma.runtime.execution import DurableExecution
        from bioma.runtime.admission import TransitionAdmission
        if self.memory and self.ledger:
            self.memory.sync(self.ledger, task.id)
        self._sync_remote_memory(task.id)
        admission = TransitionAdmission(self.ledger, available_resources=self.available_resources,
                                        memory=self.memory, provider_check=self.provider_pool.check_assignment,
                                        capability_runtime=self)
        execution = DurableExecution(self.ledger, event_sink, admission=admission) if self.ledger else None
        prepared = {}
        submitted_remote = set()
        state = ScientificState.from_task(task)
        scoped = task.gateway.get("scoped_capability_context") or {}
        parent_task_ref = scoped.get("parent_task_ref")
        if task.gateway.get("exact_grounded_binding") and parent_task_ref:
            if self.ledger is None or not self.ledger.verify_chain(parent_task_ref):
                raise ValueError("scoped parent ledger is unavailable or invalid")
            parent_events = self.ledger.events(parent_task_ref)
            checkpoints = [row for row in parent_events if row.kind == "AdaptiveControlCheckpoint"]
            if not checkpoints:
                raise ValueError("scoped parent has no committed controller checkpoint")
            parent = ScientificState.model_validate(checkpoints[-1].payload["state"])
            refs = set(task.inputs.get("input_refs", []))
            observations = [row for row in parent.observations if row.id in refs]
            evidence = [row for row in parent.evidence if row.id in refs]
            from bioma.core.source_inputs import scoped_source_refs
            source_refs = scoped_source_refs(parent.inputs, task.inputs)
            resolved = {row.id for row in observations} | {row.id for row in evidence} | source_refs
            if refs - resolved:
                raise ValueError("scoped input reference is absent from parent checkpoint")
            grounding_ref = scoped.get("grounding_ref")
            if not any(row.kind == "Grounding" and any(
                    item.get("result", {}).get("status") == "GROUNDED" and any(
                        alt.get("schema_version") == "grounded-option-v1"
                        for alt in item.get("result", {}).get("alternatives", []))
                    for item in row.payload.get("results", [])) for row in parent_events):
                raise ValueError("scoped parent has no grounded controller action")
            artifact_ids = {ref for o in observations for ref in o.artifact_ids}
            artifact_ids.update(a['id'] for o in observations if o.kind == 'work_result'
                for a in o.value.get('artifacts', []) if isinstance(a, dict) and a.get('id'))
            artifact_ids.update(ref for e in evidence for ref in e.artifact_ids)
            artifacts = [a for a in parent.artifacts if a.id in artifact_ids]
            state = state.model_copy(update={"observations": observations, "evidence": evidence,
                                             "artifacts": artifacts})
            self.ledger.append(task.id, "ScopedScientificInputsProjected", "harness", {
                "parent_task_ref": parent_task_ref, "parent_checkpoint_event": checkpoints[-1].event_id,
                "input_refs": sorted(refs), "observation_refs": [row.id for row in observations],
                "evidence_refs": [row.id for row in evidence], "source_refs": sorted(source_refs), "grounding_ref": grounding_ref},
                idempotency_key="scoped-input-projection")
        if task.gateway.get('provider_requirement'):
            from bioma.core.query_contract import verify_query_spec
            from bioma.core.scientific_capabilities import CapabilityRequirement
            from bioma.core.models import ControlRecord
            from bioma.core.research import fingerprint
            spec = verify_query_spec(task.objective, task.gateway.get('query_spec') or {})
            from bioma.runtime.query_compiler import QueryContractCompiler
            QueryContractCompiler.validate_intake(task, spec)
            requirement = CapabilityRequirement.model_validate(task.gateway['provider_requirement'])
            record = ControlRecord.model_validate(task.gateway['query_control_record'])
            if (record.source != 'query_contract_compiler' or record.kind != 'query_spec'
                    or record.subject_hash != fingerprint(spec.model_dump(mode='json'))
                    or requirement.objective != task.objective
                    or requirement.capability_family != task.initial_capability
                    or requirement.constraints.get('method_id') != task.initial_method_id):
                raise ValueError('Compiled query authority or capability binding changed')
            state.control_records.append(record)
        started_at = time.monotonic()
        from bioma.core.deliverables import task_contracts
        deliverables = task_contracts(task)
        active_agents = set(task.active_agents or self.agents)
        unknown_agents = active_agents - self.agents.keys()
        if unknown_agents:
            raise UnknownAgentError(f"Unknown active agents: {sorted(unknown_agents)}.")
        if task.initial_capability is None and task.initial_agent not in active_agents:
            raise UnknownAgentError(
                f"Initial agent {task.initial_agent!r} is not active."
            )
        state = state.model_copy(update={"active_agents": sorted(active_agents)})
        initial_ttl = (
            max(16, task.max_cycles * 8 + 8)
            if task.initial_capability == "research.problem_formulation"
            else task.max_cycles * 4 + 1
        )
        from bioma.core.research import contract_for
        research = contract_for(task)
        if research:
            from bioma.core.research_planning import repair_limits
            initial_ttl = max(initial_ttl, 16 + repair_limits(task)[1] * 4)
        event_limit = self.max_events or max(128, initial_ttl)
        initial_payload = {
            "objective": task.objective,
            "inputs": task.inputs,
            "success_criteria": task.success_criteria,
            **({"input_refs": list(task.inputs.get("input_refs", []))}
               if task.inputs.get("input_refs") else {}),
            **({"desired_deliverables": task.gateway.get("desired_deliverables", [])}
               if task.gateway.get("provider_requirement") else {}),
            **({"next_requirements": task.gateway.get("provider_next_requirements", []),
                "query_spec": task.gateway.get("query_spec", {}),
                "query_spec_control_id": task.gateway.get("query_control_record", {}).get("id")}
               if task.gateway.get("provider_requirement") else {}),
        }
        prior = self.ledger.events(task.id) if self.ledger else ()
        saved = next((e for e in reversed(prior) if e.kind in {'TransitionCommitted', 'RunInitialized'}), None)
        if continuation is not None:
            # An explicit caller-authorized continuation is a new verified scheduling
            # transition, never a rewrite of a failed result or a budget reset.
            required_continuation = {'id', 'message_id', 'expected_version', 'reason'}
            allowed_continuation = required_continuation | {'clarification'}
            if (not saved or not required_continuation <= set(continuation)
                    or not set(continuation) <= allowed_continuation):
                raise ValueError('Continuation requires a recorded task and an exact continuation contract')
            if not all(isinstance(continuation[k], str) and continuation[k].strip()
                       for k in ('id', 'message_id', 'reason')):
                raise ValueError('Continuation identity, source and reason must be nonempty')
            if type(continuation['expected_version']) is not int or continuation['expected_version'] < 0:
                raise ValueError('Continuation expected_version must be a nonnegative integer')
            existing = next((e for e in prior if e.kind == 'TransitionCommitted'
                and e.payload['message']['message_type'] == 'runtime.continuation'
                and e.payload['message']['payload'].get('id') == continuation['id']), None)
            if existing:
                if existing.payload['message']['payload'] != continuation:
                    raise ValueError('Continuation ID reused with different inputs')
            else:
                initial = next(e for e in prior if e.kind == 'RunInitialized')
                if initial.payload['task'] != task.model_dump(mode='json') or not self.ledger.verify_chain(task.id):
                    raise ValueError('Continuation task identity or ledger integrity mismatch')
                previous = self.ledger.replay_state(task.id)
                if previous is None or previous.status not in {'failed', 'blocked'}:
                    raise ValueError('Only a failed or blocked task can be explicitly continued')
                if previous.version != continuation['expected_version'] or saved.payload.get('outbox'):
                    raise ValueError('Continuation version mismatch or pending work exists')
                source = next((m for e in prior for m in e.payload.get('outbox', [])
                               if m['id'] == continuation['message_id']), None)
                if source is None:
                    raise ValueError('Continuation source message was not previously scheduled')
                terminal_message = saved.payload['message']
                failed_message_id = (terminal_message.get('causation_id')
                    if terminal_message['message_type'] in {'runtime.failed', 'runtime.blocked'}
                    else terminal_message['id'])
                if continuation['message_id'] != failed_message_id:
                    raise ValueError('Continuation must target the currently failed work, not earlier completed work')
                original = AgentMessage.model_validate(source)
                if original.recipient not in active_agents:
                    raise ValueError('Continuation provider is no longer active')
                command = AgentMessage(sender='harness', recipient='harness',
                    message_type='runtime.continuation', payload=continuation,
                    causation_id=original.id, ttl=original.ttl + 1)
                from uuid import uuid4
                resumed_payload = dict(original.payload)
                clarification = continuation.get('clarification')
                if clarification is not None:
                    if not isinstance(clarification, dict) or set(clarification) != {
                            'field', 'question', 'answer', 'sources'}:
                        raise ValueError('Clarification must bind one field, question, answer and source list')
                    if not all(isinstance(clarification[key], str) and clarification[key].strip()
                               for key in ('field', 'question', 'answer')):
                        raise ValueError('Clarification field, question and answer must be nonempty')
                    if (not isinstance(clarification['sources'], list) or not clarification['sources']
                            or not all(isinstance(item, dict) and item.get('identity') and item.get('sha256')
                                       for item in clarification['sources'])):
                        raise ValueError('Clarification requires versioned source identities')
                    resumed_payload['clarification_reply'] = clarification
                resumed = original.model_copy(update={'id': str(uuid4()), 'sender': 'harness',
                    'causation_id': command.id, 'idempotency_key': None,
                    'payload': resumed_payload})
                delta = StateDelta(agent_id='harness', source_message_id=command.id,
                    expected_version=previous.version, messages=[resumed],
                    status_reason={'code': 'explicit_continuation', **continuation})
                checked, receipt = self.verifier.verify_with_receipt(previous, delta,
                    expected_agent='harness', source_message=command)
                if not checked.ok:
                    raise VerificationError(' '.join(checked.reasons))
                self.ledger.commit_transition(previous,
                    delta.model_copy(update={'status': 'verified'}), command, receipt)
                prior = self.ledger.events(task.id)
                saved = next(e for e in reversed(prior) if e.kind == 'TransitionCommitted')
        if not saved and task.external_work_results:
            if self.ledger is None:
                raise ValueError('External WorkResult admission requires a ledger')
            from bioma.runtime.external_work_results import import_external_work_results
            imported = import_external_work_results(task.external_work_results)
            state = state.model_copy(update={'observations': imported})
            initial_payload['input_refs'] = list(dict.fromkeys([
                *initial_payload.get('input_refs', []), *[o.id for o in imported]]))
            for reference, observation in zip(task.external_work_results, imported):
                self.ledger.append(task.id, 'ExternalWorkResultImported', 'harness', {
                    'observation': observation.model_dump(mode='json'),
                    'source_reference': reference.model_dump(mode='json')},
                    idempotency_key='external-work-result:' + observation.id)
        if not saved:
            _emit(event_sink, {"type": "goal.submitted", "agent": "user", "state_version": state.version, "task": task.model_dump(mode="json")})
            if deliverables:
                _emit(event_sink, {"type": "execution_plan.deliverables", "agent": "harness", "state_version": 0, "deliverables": deliverables, "deadline_seconds": self.task_deadline_seconds})
        if saved:
            initial = next((e for e in prior if e.kind == 'RunInitialized'), None)
            if initial is None:
                raise ValueError('Legacy run lacks RunInitialized; use a new task ID or explicitly migrate the historical ledger')
            if not self.ledger.verify_chain(task.id):
                raise ValueError('Run ledger hash chain is invalid')
            now = time.time()
            # Continuations have their own explicitly authorized execution window.
            # Restarts retain its original timestamp, never refresh the deadline.
            window = next((e for e in reversed(prior) if e.kind == 'TransitionCommitted'
                and e.payload['message']['message_type'] == 'runtime.continuation'), None)
            from datetime import datetime
            window_start = datetime.fromisoformat(window.created_at).timestamp() if window else initial.payload.get('started_at_unix', now)
            window_events = [e for e in prior if not window or e.sequence > window.sequence]
            paused_seconds, paused = _budget_pause_seconds(window_events, now)
            started_at -= max(0.0, now - window_start - paused_seconds)
            if paused:
                self.ledger.append(task.id, 'RuntimeBudgetResumed', 'harness',
                    {'excluded_budget_wait_seconds': paused_seconds, 'execution_time_preserved': True})
            if initial.payload['task'] != task.model_dump(mode='json'):
                raise ValueError('Task ID reused with different inputs')
            state = (self.ledger.replay_state(task.id) if saved.kind == 'TransitionCommitted'
                     else ScientificState.model_validate(saved.payload['after']))
            queue = deque(AgentMessage.model_validate(m) for m in saved.payload['outbox'])
            tool_checkpoints.extend(e.payload for e in prior if e.kind == 'tool.completed' and isinstance(e.payload.get('observation'), dict) and e.payload['observation'].get('success') is True)
            context.state = state
            _emit(event_sink, {'type': 'runtime.resumed', 'agent': 'harness', 'state_version': state.version, 'pending_messages': len(queue), 'state': state.model_dump(mode='json')})
        elif task.initial_capability:
            request = TaskRequest(
                requester="query_contract_compiler" if task.gateway.get('provider_requirement') else "user",
                required_capability=task.initial_capability,
                objective=task.objective,
                message_type=task.initial_message_type or (
                    "query.submitted"
                    if task.initial_capability == "research.problem_formulation"
                    else "task.submitted"
                ),
                payload={**initial_payload, **({'method_id': task.initial_method_id} if task.initial_method_id else {})},
            )
            exact = task.gateway.get("exact_grounded_binding") or {}
            decision = self.capability_router.route(
                request, active_agents=active_agents,
                available_resources=self.available_resources, ttl=initial_ttl,
                capability_binding=exact.get("capability_id"),
                provider_binding=exact.get("provider_id"),
                method_binding=exact.get("method_id"),
                implementation_binding=exact.get("implementation_identity"))
            decision = _collapse_proposal(decision)
            self._emit_route(event_sink, decision, state)
            state = state.model_copy(
                update={
                    "task_requests": [request],
                    "offers": list(decision.offers),
                    "commitments": list(decision.commitments),
                }
            )
            if not decision.messages and state.status == "running":
                state = state.model_copy(
                    update={
                        "status": "blocked",
                        "status_reason": {
                            "code": "no_capability",
                            "task_request_id": request.id,
                            "required_capability": request.required_capability,
                            "qualification": list(decision.qualification),
                        },
                    }
                )
            queue = deque(decision.messages)
        else:
            queue = deque(
                [
                    AgentMessage(
                        sender="user",
                        recipient=task.initial_agent,
                        message_type=task.initial_message_type or "task.submitted",
                        payload={**initial_payload, **({'method_id': task.initial_method_id} if task.initial_method_id else {})},
                        ttl=initial_ttl,
                    )
                ]
            )

        if self.ledger and not saved:
            self.ledger.append(task.id, 'RunInitialized', 'harness', {
                'task': task.model_dump(mode='json'), 'started_at_unix': time.time(), 'after': state.model_dump(mode='json'),
                'outbox': [m.model_dump(mode='json') for m in queue]}, idempotency_key='run:init')
        context.state = state
        handled = sum(e.kind == 'agent.started' for e in prior) if saved else 0
        renegotiated = {}
        # Reassignment is a durable outbox replacement, including a crash before
        # the replacement provider commits its first result.
        for event in prior:
            if event.kind == 'CapabilityReassigned' and saved and event.sequence > saved.sequence:
                replacements = [AgentMessage.model_validate(m) for m in event.payload['messages']]
                queue = deque(n for old in queue for n in (replacements if old.id == event.payload['old_message_id'] else [old]))
                for item in replacements:
                    renegotiated[item.id] = event.payload
        recovery_attempts: dict[str, int] = {}
        while state.status == "running":
            elapsed = time.monotonic() - started_at
            from bioma.runtime.deadline import remaining
            if elapsed > self.task_deadline_seconds or remaining() == 0:
                failure = Evidence(
                    kind="execution_timeout",
                    claim="The task exceeded its monotonic execution deadline before all deliverables were verified.",
                    value={"elapsed_seconds": round(elapsed, 3), "deadline_seconds": self.task_deadline_seconds, "pending_deliverables": _pending_deliverables(deliverables, state)},
                    source="harness",
                    method="task_deadline",
                )
                approved = self._commit_terminal(context, outcome="failed", evidence=[failure],
                    reason={"code": "execution_timeout", "deadline_seconds": self.task_deadline_seconds})
                _emit(event_sink, {"type": "runtime.deadline_exceeded", "agent": "harness", "state_version": state.version, "elapsed_seconds": round(elapsed, 3), "deadline_seconds": self.task_deadline_seconds})
                self._publish_terminal(context, approved)
                return context.state
            if not queue:
                raise RuntimeDeadlockError(
                    "The runtime has no pending peer message and no agent completed the task."
                )
            if handled >= event_limit:
                raise RuntimeLimitError(
                    f"The runtime exceeded its {event_limit}-event safety limit."
                )

            # Submit remote commitments before their dependencies complete.
            # Their local fact rules may activate work without another planner call.
            from bioma.runtime.acceptance import bind_delivery_message
            from bioma.runtime.rules import RequirementRules
            queue = deque(bind_delivery_message(state, m) for m in queue)
            for pending in queue:
                if not RequirementRules(self.ledger).ready(pending, state, deliverables):
                    continue
                peer = self.agents.get(pending.recipient)
                if hasattr(peer, 'prepare') and pending.id not in submitted_remote:
                    view = _authorized_state_projection(state, pending.recipient, ledger=self.ledger, message=pending, backend=peer)
                    try:
                        self.provider_pool.check_assignment(pending)
                        peer.prepare(view, pending)
                        submitted_remote.add(pending.id)
                    except (OSError, ValueError) as exc:
                        _emit(event_sink, {"type": "node.unavailable", "agent": pending.recipient, "reason": str(exc)})

            # Execute independent input-bound branches concurrently. Completion
            # is later merged under the run lease and reverified against current state.
            if execution and not prepared:
                verified = {r['id'] for r in delivery_status(deliverables, state) if r['status'] == 'verified'}
                assignments = []
                for pending in queue:
                    contracts = pending.payload.get('delivery_contracts', [])
                    own = {c['id'] for c in contracts}
                    needs = {d for c in contracts for d in c.get('depends_on', [])} - own
                    peer = self.agents.get(pending.recipient)
                    safe = getattr(peer, 'parallel_safe', lambda s, m: False)(state, pending) if peer else False
                    if contracts and safe and needs <= verified and RequirementRules(self.ledger).ready(pending, state, deliverables):
                        view = _authorized_state_projection(state, pending.recipient, ledger=self.ledger, message=pending, backend=peer)
                        assignments.append((peer, view, pending))
                if len(assignments) > 1:
                    with observe(event_sink):
                        results = execution.parallel(assignments)
                    prepared.update({m.id: (v, d) for (_, v, m), d in zip(assignments, results)})
            from bioma.runtime.rules import RequirementRules
            ready_message = next((m for m in queue if RequirementRules(self.ledger).ready(m, state, deliverables)), None)
            if ready_message is None:
                from bioma.core.acceptance import protocol_for, completion_status
                if protocol_for(state) is not None:
                    reason = {'code': 'acceptance_incomplete', 'acceptance': completion_status(state),
                              'message': 'Pending assignments have unsatisfied evidence prerequisites.',
                              'pending_message_ids': [m.id for m in queue]}
                    terminal = self._commit_terminal(context, outcome='blocked', reason=reason)
                    self._publish_terminal(context, terminal)
                    return context.state
                raise RuntimeDeadlockError("Required evidence is missing; no requirement is currently enabled")
            queue.remove(ready_message)
            message = context.message = ready_message
            recipient = message.recipient
            if recipient not in active_agents:
                raise UnknownAgentError(
                    f"Message targets disabled agent {recipient!r}."
                )
            agent = self.agents.get(recipient)
            if agent is None:
                raise UnknownAgentError(
                    f"Message {message.id!r} resolved to unregistered agent {recipient!r}."
                )

            if execution is None:
                _emit(
                    event_sink,
                    {
                        "type": "agent.started",
                        "agent": recipient,
                        "message": {
                            **message.model_dump(mode="json"),
                        },
                        "state": state.model_dump(mode="json"),
                    },
                )
            snapshot = _authorized_state_projection(state, recipient, ledger=self.ledger, message=message, backend=self.agents.get(recipient))
            recovery_key = message.task_request_id or message.id
            attempt = recovery_attempts.get(recovery_key, 0)
            try:
                from bioma.core.methods import validate_method_binding
                validate_method_binding(agent, message)
                with observe(
                    event_sink,
                    agent=recipient,
                    message_id=message.id,
                    state_version=state.version,
                ):
                    if message.id in prepared:
                        original_view, delta = prepared.pop(message.id)
                        if isinstance(delta, Exception):
                            raise delta
                    else:
                        original_view = snapshot
                        delta = (execution.execute(agent, snapshot, message.model_copy(deep=True))
                                 if execution else admission.execute(agent, snapshot, message.model_copy(deep=True)))
                    from bioma.runtime.deadline import check
                    check()
                    if execution:
                        original_view = execution.contexts.get(message.id, original_view)
                    if delta.expected_version != state.version:
                        from bioma.runtime.admission import revalidate_reads
                        revalidate_reads(delta, snapshot, message)
                        # A provider completion is local work, not research closure.
                        local_delta = delta.model_copy(update={'outcome': 'continue'}) if original_view.gateway.get('research_contract') and delta.outcome == 'completed' else delta
                        local_result, local_receipt = self.verifier.verify_with_receipt(original_view, local_delta,
                            expected_agent=recipient, source_message=message)
                        if not local_result.ok:
                            raise ValueError("Independent branch failed local verification")
                        if any(k not in {"metrics", "predictions"} for k in delta.updates):
                            self.ledger.append(state.task_id, "BranchConflict", recipient,
                                {"message_id": message.id, "updates": delta.updates})
                            raise ValueError("Conflicting branch update requires reconciliation")
                        updates = dict(delta.updates)
                        if "metrics" in updates:
                            merged = dict(updates["metrics"])
                            for k, value in merged.items():
                                if isinstance(value, dict) and isinstance(state.metrics.get(k), dict):
                                    overlap = set(value) & set(state.metrics[k])
                                    if any(value[x] != state.metrics[k][x] for x in overlap):
                                        self.ledger.append(state.task_id, "BranchConflict", recipient, {"metric": k})
                                        raise ValueError("Conflicting branch metric")
                                    merged[k] = {**state.metrics[k], **value}
                            updates["metrics"] = merged
                        self.ledger.append(state.task_id, "BranchRevalidated", recipient,
                            {"message_id": message.id, "local_receipt": local_receipt.model_dump(mode="json"),
                             "source_version": delta.expected_version, "target_version": state.version})
                        delta = delta.model_copy(update={"expected_version": state.version, "updates": updates})
            except Exception as exc:
                from bioma.llm import ModelBudgetUnavailable
                if isinstance(exc, ModelBudgetUnavailable):
                    raise  # Await explicit budget restoration; no scientific repair/retry.
                from bioma.runtime.workers import ProviderUnavailable
                if isinstance(exc, ProviderUnavailable):
                    previous_reassignments = [e for e in self.ledger.events(state.task_id)
                        if e.kind == 'CapabilityReassigned' and e.payload['request_id'] == message.task_request_id] if self.ledger else []
                    request = next((r for r in state.task_requests if r.id == message.task_request_id), None)
                    if not exc.execution_started and request and len(previous_reassignments) < 2 and attempt < 2:
                        self.refresh_providers()
                        decision = _collapse_proposal(self.capability_router.route(
                            request, active_agents=active_agents,
                            available_resources=self.available_resources, ttl=max(1,message.ttl-1)))
                        if decision.messages:
                            data = {'old_message_id':message.id, 'request_id':request.id,
                                'messages':[m.model_dump(mode='json') for m in decision.messages],
                                'offers':[o.model_dump(mode='json') for o in decision.offers],
                                'commitments':[c.model_dump(mode='json') for c in decision.commitments]}
                            if self.ledger:
                                self.ledger.append(state.task_id,'CapabilityReassigned','harness',data,
                                                   idempotency_key='reassign:'+message.id)
                            for item in decision.messages:
                                renegotiated[item.id] = data
                            queue.extendleft(reversed(decision.messages))
                            recovery_attempts[recovery_key] = attempt+1
                            continue
                    reason = {'code':'capability_lost_during_execution' if exc.execution_started else 'no_live_provider',
                              'message':str(exc), 'recovery':'reconcile' if exc.execution_started else 'register_or_renew_provider'}
                    terminal = self._commit_terminal(context, outcome='blocked', reason=reason)
                    self._publish_terminal(context, terminal)
                    return context.state
                from bioma.runtime.deadline import ExecutionTimeout
                if isinstance(exc, ExecutionTimeout):
                    # Re-enter the common verified timeout transition; no late delta commits.
                    started_at = time.monotonic() - self.task_deadline_seconds - 1
                    continue
                from bioma.runtime.effects import EffectUncertain
                if isinstance(exc, EffectUncertain):
                    delta = StateDelta(agent_id=recipient, source_message_id=message.id,
                        expected_version=state.version, outcome="failed",
                        evidence=[Evidence(kind="execution_failure", source=recipient,
                            claim="External operation outcome could not be reconciled; replay was prevented.",
                            method="effect_reconciliation", value={"error": str(exc)})],
                        status_reason={"code": "external_effect_uncertain"})
                else:
                    action = _recovery_action(exc, attempt)
                    if action in {"retry", "repair"}:
                        recovery_attempts[recovery_key] = attempt + 1
                        delay = min(2**attempt, 4) if action == "retry" else 0
                        _emit(
                            event_sink,
                            {
                                "type": "runtime.recovery",
                                "agent": recipient,
                                "state_version": state.version,
                                "action": action,
                                "attempt": attempt + 1,
                                "delay_seconds": delay,
                                "error_type": type(exc).__name__,
                                "reason": str(exc),
                            },
                        )
                        if delay:
                            time.sleep(delay)
                        feedback = {
                            **message.payload,
                            "runtime_feedback": {
                                "attempt": attempt + 1,
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                                "instruction": (
                                    "Correct the invalid output and reuse successful prior "
                                    "evidence; do not repeat completed external work."
                                ),
                            },
                        }
                        queue.appendleft(
                            message.model_copy(
                                update={"payload": feedback, "ttl": max(1, message.ttl - 1)}
                            )
                        )
                        handled += 1
                        continue
                    if action == "blocked":
                        reason = {"code": "runtime_dependency_unavailable",
                                  "error_type": type(exc).__name__, "message": str(exc)}
                        approved = self._commit_terminal(context, outcome="blocked", reason=reason)
                        _emit(
                            event_sink,
                            {
                                "type": "runtime.blocked",
                                "agent": recipient,
                                "state_version": state.version,
                                "error_type": type(exc).__name__,
                                "reason": str(exc),
                            },
                        )
                        self._publish_terminal(context, approved)
                        return context.state
                    raise
            local_outcome = delta.outcome
            from bioma.runtime.acceptance import stage_output, activate, finalize
            from bioma.core.acceptance import protocol_for
            acceptance = protocol_for(state)
            delta = stage_output(state, delta, message)
            delta = _activate_provider_requirements(state, delta, message)
            for item in delta.evidence:
                trace = item.value.get("tool_trace") if isinstance(item.value, dict) else None
                if item.kind == "prediction" and isinstance(trace, (list, tuple)) and not item.value.get("live_tool_events"):
                    if any(step.get("name") in {"skills_list", "taxonomy_list", "taxonomy_skills"} for step in trace if isinstance(step, dict)):
                        _emit(event_sink, {"type": "skills.catalogued", "agent": recipient, "state_version": state.version})
                    for step in trace:
                        if not isinstance(step, dict):
                            continue
                        if step.get("name") == "skill_view" and isinstance(step.get("observation"), dict) and step["observation"].get("success"):
                            _emit(event_sink, {"type": "skill.loaded", "agent": recipient, "state_version": state.version, "skill": step["observation"].get("name")})
                        _emit(event_sink, {"type": "tool.completed", "agent": recipient, "state_version": state.version, "tool": step.get("name"), "observation": step.get("observation")})
                    _emit(event_sink, {"type": "fusion.completed", "agent": recipient, "state_version": state.version, "fusion_rounds": item.value.get("fusion_rounds")})
            if deliverables and delta.outcome not in {'blocked', 'failed'}:
                prospective = state.model_copy(update={
                    'evidence': [*state.evidence, *delta.evidence],
                    'artifacts': [*state.artifacts, *delta.artifacts],
                    'task_requests': [*state.task_requests, *delta.task_requests]})
                generated = RequirementRules(self.ledger).propose(message, prospective, deliverables, requester=recipient)
                if generated:
                    delta = delta.model_copy(update={'task_requests': [*delta.task_requests, *generated], 'outcome': 'continue'})
                    _emit(event_sink, {'type': 'requirements.generated', 'agent': recipient,
                        'reason': 'uncovered_delivery_contract', 'request_ids': [r.id for r in generated]})
            local_analysis_failure = False
            if acceptance:
                from bioma.core.acceptance import is_delivery
                local_analysis_failure = (acceptance.mode == 'research' and recipient != 'queryagent'
                    and message.message_type != 'research.review.requested' and not is_delivery(state, message))
            if ((message.payload.get('repair_gap_id') or local_analysis_failure)
                    and acceptance and acceptance.mode == 'research' and delta.outcome in {'blocked', 'failed'}
                    and delta.status_reason.get('code') != 'external_effect_uncertain'):
                # Preserve the failed attempt as evidence and return it to Com;
                # only Com decides whether another bounded repair is justified.
                failure = Evidence(kind='execution_failure', source=recipient, method='research_repair_attempt',
                    claim='The requested evidence repair could not be completed.',
                    value={'repair_gap_id': message.payload.get('repair_gap_id'), 'outcome': delta.outcome,
                           'reason': delta.status_reason}, parent_ids=[message.payload['gap_evidence_id']] if message.payload.get('gap_evidence_id') else [])
                delta = delta.model_copy(update={'evidence': [*delta.evidence, failure], 'outcome': 'continue', 'status_reason': {}})
            delta = activate(state, delta, message, queue)
            delta = self._route_requests(
                delta,
                source_message=message,
                active_agents=active_agents,
                event_sink=event_sink,
                state=state,
            )
            if message.id in renegotiated:
                from bioma.core.models import TaskOffer, Commitment
                data = renegotiated.pop(message.id)
                delta = delta.model_copy(update={
                    'offers':[*delta.offers,*[TaskOffer.model_validate(o) for o in data['offers']]],
                    'commitments':[*delta.commitments,*[Commitment.model_validate(c) for c in data['commitments']]]})
            if delta.outcome == "completed" and deliverables:
                prospective = state.model_copy(update={
                    "evidence": [*state.evidence, *delta.evidence],
                    "artifacts": [*state.artifacts, *delta.artifacts],
                })
                pending = _pending_deliverables(deliverables, prospective)
                if pending and not queue and not delta.messages:
                    delta = delta.model_copy(update={
                        "outcome": "blocked",
                        "status_reason": {"code": "incomplete_deliverables", "pending": pending},
                        "evidence": [*delta.evidence, Evidence(kind="incomplete_deliverables", claim="The task cannot complete because required deliverables remain unverified.", value={"pending": pending}, source=recipient, method="deliverable_dag")],
                    })
            # A package task has multiple independently committed deliveries.
            # One provider finishing must not discard messages already queued for
            # the remaining requested capabilities.
            if delta.outcome == "completed" and (queue or delta.messages):
                delta = delta.model_copy(update={"outcome": "continue"})
            delta = finalize(state, delta, message, queue, local_outcome)
            try:
                delta = self.harness.enforce(state, delta, message)
            except ScopeViolation as exc:
                action = _recovery_action(exc, attempt)
                if action == "repair":
                    recovery_attempts[recovery_key] = attempt + 1
                    _emit(
                        event_sink,
                        {
                            "type": "runtime.recovery",
                            "agent": recipient,
                            "state_version": state.version,
                            "action": "repair",
                            "attempt": attempt + 1,
                            "error_type": type(exc).__name__,
                            "reason": str(exc),
                        },
                    )
                    queue.appendleft(
                        message.model_copy(
                            update={
                                "payload": {
                                    **message.payload,
                                    "runtime_feedback": {
                                        "error_type": type(exc).__name__,
                                        "message": str(exc),
                                        "instruction": "Produce and cite the missing required evidence before terminating.",
                                    },
                                },
                                "ttl": max(1, message.ttl - 1),
                            }
                        )
                    )
                    handled += 1
                    continue
                _emit(
                    event_sink,
                    {
                        "type": "verification.rejected",
                        "agent": recipient,
                        "state_version": state.version,
                        "delta_outcome": delta.outcome,
                        "evidence_kinds": [item.kind for item in delta.evidence],
                        "reason": str(exc),
                    },
                )
                raise
            proposal_hash = hashlib.sha256(
                json.dumps(
                    delta.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            _emit(
                event_sink,
                {
                    "type": "proposal.created",
                    "agent": recipient,
                    "state_version": state.version,
                    "proposal_hash": proposal_hash,
                    "evidence_count": len(delta.evidence),
                    "artifact_count": len(delta.artifacts),
                    "request_count": len(delta.task_requests),
                },
            )
            _emit(
                event_sink,
                {
                    "type": "verification.started",
                    "agent": recipient,
                    "state_version": state.version,
                    "delta_outcome": delta.outcome,
                },
            )
            try:
                approved = self.verifier.approve(
                    state,
                    delta,
                    expected_agent=recipient,
                    source_message=message,
                )
            except Exception as exc:
                _emit(
                    event_sink,
                    {
                        "type": "verification.rejected",
                        "agent": recipient,
                        "state_version": state.version,
                        "delta_outcome": delta.outcome,
                        "evidence_kinds": [item.kind for item in delta.evidence],
                        "reason": str(exc),
                    },
                )
                raise
            _emit(
                event_sink,
                {
                    "type": "verification.approved",
                    "agent": recipient,
                    "state_version": state.version,
                    "checks": "transition_integrity",
                    "verification_protocol": "deterministic-transition-v1",
                    "proposal_hash": proposal_hash,
                },
            )
            result, receipt = self.verifier.verify_with_receipt(
                state, delta, expected_agent=recipient, source_message=message,
            )
            if not result.ok:
                raise RuntimeError("Receipt verification disagrees with approval")
            state = (self.ledger.commit_transition(state, approved, message, receipt, tuple(queue))
                     if self.ledger is not None else state.commit(approved))
            context.state = state
            if self.memory and self.ledger:
                self.memory.sync(self.ledger, state.task_id)
            self._sync_remote_memory(state.task_id)
            if deliverables:
                _emit(event_sink, {"type": "execution_plan.progress", "agent": "harness", "state_version": state.version, "deliverables": delivery_status(deliverables, state)})
            _emit(
                event_sink,
                {
                    "type": "state.committed",
                    "agent": recipient,
                    "delta": approved.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                },
            )
            queue.extend(approved.messages)
            handled += 1

        return state

    def _route_requests(
        self,
        delta: StateDelta,
        *,
        source_message: AgentMessage,
        active_agents: set[str],
        event_sink: RuntimeEventSink | None,
        state: ScientificState,
    ) -> StateDelta:
        if delta.offers or delta.commitments:
            raise RoutingIntegrityError(
                "Agents may propose TaskRequest objects, but only the Harness router "
                "may create TaskOffer and Commitment records."
            )
        if delta.task_requests and delta.messages:
            raise RoutingIntegrityError(
                "A capability request must be recipient-free; the Harness creates its "
                "delivery messages after qualification."
            )
        if source_message.task_request_id is not None and delta.messages:
            raise RoutingIntegrityError(
                "Capability-routed agents cannot bypass the Harness with direct messages."
            )
        if not delta.task_requests:
            return delta
        self.refresh_providers()
        offers = list(delta.offers)
        commitments = list(delta.commitments)
        messages = list(delta.messages)
        missing_required: list[TaskRequest] = []
        rejected_qualifications = {}
        routed = [(request, self.capability_router.route(
            request,
            active_agents=(active_agents if source_message.ttl > 1 else set()),
            available_resources=self.available_resources,
            ttl=max(1, source_message.ttl - 1)))
            for request in delta.task_requests]
        for request, decision in routed:
            self._emit_route(event_sink, decision, state)
            offers.extend(decision.offers)
            commitments.extend(decision.commitments)
            messages.extend(decision.messages)
            if request.required and not decision.messages:
                missing_required.append(request)
                rejected_qualifications[request.id] = list(decision.qualification)
        outcome = delta.outcome
        if missing_required:
            outcome = "blocked"
            messages = []
        elif outcome == "continue" and not messages:
            outcome = "completed"
        return delta.model_copy(
            update={
                "task_requests": [request for request, _ in routed],
                "offers": offers,
                "commitments": commitments,
                "messages": messages,
                "outcome": outcome,
                "status_reason": (
                    {
                        "code": "no_capability",
                        "requests": [
                            {
                                "task_request_id": request.id,
                                "required_capability": request.required_capability,
                                "qualification": rejected_qualifications[request.id],
                            }
                            for request in missing_required
                        ],
                    }
                    if missing_required
                    else delta.status_reason
                ),
            }
        )

    def _emit_route(self, event_sink, decision, state: ScientificState) -> None:
        common = {"agent": decision.request.requester, "state_version": state.version}
        _emit(
            event_sink,
            {
                "type": "task.requested",
                **common,
                "task_request": decision.request.model_dump(mode="json"),
            },
        )
        _emit(
            event_sink,
            {
                "type": "qualification.filtered",
                **common,
                "required_capability": decision.request.required_capability,
                "selection_mode": decision.request.selection_mode,
                "candidates": list(decision.qualification),
            },
        )
        _emit(
            event_sink,
            {
                "type": "capability.offers",
                **common,
                "required_capability": decision.request.required_capability,
                "offers": [item.model_dump(mode="json") for item in decision.offers],
            },
        )
        for commitment in decision.commitments:
            _emit(
                event_sink,
                {
                    "type": "commitment.created",
                    "agent": commitment.agent_id,
                    "state_version": state.version,
                    "commitment": commitment.model_dump(mode="json"),
                },
            )
            _emit(
                event_sink,
                {
                    "type": "lease.acquired",
                    "agent": commitment.agent_id,
                    "state_version": state.version,
                    "task_request_id": commitment.task_request_id,
                    "capability_id": commitment.capability_id,
                    "lease_expires_at": commitment.lease_expires_at.isoformat()
                    if commitment.lease_expires_at
                    else None,
                },
            )


def _activate_provider_requirements(state, delta, message):
    """Turn typed provider continuations into runtime-owned assignments."""
    if delta.outcome in {'failed', 'blocked'}:
        return delta
    from uuid import NAMESPACE_URL, uuid5
    from bioma.core.models import Observation
    from bioma.core.scientific_capabilities import WorkResult
    requests = []
    known = {row.id for row in state.task_requests}
    for raw in delta.updates.get('observations', []):
        observation = raw if isinstance(raw, Observation) else Observation.model_validate(raw)
        if observation.kind != 'work_result':
            continue
        work = WorkResult.model_validate(observation.value)
        for requirement in work.requirements:
            request_id = str(uuid5(NAMESPACE_URL,
                state.task_id + ':provider-requirement:' + observation.id + ':' + requirement.capability_family))
            if request_id in known:
                continue
            message_type = requirement.constraints.get('message_type', 'task.submitted')
            method_id = requirement.constraints.get('method_id')
            payload = {'objective': requirement.objective,
                'inputs': message.payload.get('inputs', {}),
                'input_refs': requirement.input_refs,
                'execution_requirements': requirement.effective_execution_requirements().model_dump(mode='json'),
                'result_requirements': requirement.result_requirements,
                'method_id': method_id,
                'query_spec': message.payload.get('query_spec', {}),
                'query_spec_control_id': message.payload.get('query_spec_control_id'),
                'next_requirements': state.gateway.get('provider_continuations', {}).get(method_id, [])}
            requests.append(TaskRequest(id=request_id, requester=delta.agent_id,
                required_capability=requirement.capability_family,
                objective=requirement.objective, message_type=message_type, payload=payload,
                budget=requirement.budget, deadline=requirement.deadline,
                risk_level=requirement.risk_level, correlation_id=message.correlation_id or message.id,
                causation_id=message.id))
            known.add(request_id)
    if not requests:
        return delta
    return delta.model_copy(update={'task_requests': [*delta.task_requests, *requests],
                                    'outcome': 'continue'})


def _emit(event_sink: RuntimeEventSink | None, event: dict[str, Any]) -> None:
    if event_sink is not None:
        event_sink({"timestamp": utc_now().isoformat(), **event})


def _authorized_state_projection(state: ScientificState, agent_id: str, *, ledger=None, message=None, backend=None) -> ScientificState:
    """Project an assignment using its registered execution owner, never its name."""
    from bioma.runtime.projections import assignment_fields, local_agent_view
    allowed, review_authority = assignment_fields(backend, agent_id, message)
    if message is not None:
        return local_agent_view(ledger, state, message, allowed,
                                review_authority=review_authority)
    defaults = ScientificState.from_task(ScientificTask(objective=state.objective, id=state.task_id)).model_dump()
    return ScientificState.model_validate({key: getattr(state, key) if key in allowed else value
        for key, value in defaults.items()}).model_copy(deep=True)
