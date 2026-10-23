"""One pre-execution boundary shared by local, durable and HTTP worker paths."""
from __future__ import annotations

import inspect
import time
from contextlib import ExitStack
from contextvars import ContextVar
from functools import lru_cache

from bioma.core.action_contracts import check_predicates, prospective_state
from bioma.core.research import fingerprint
from bioma.core.transitions import ExecutionPermit, ExecutionResult, TransitionProposal, check_read_set


_execution_check = ContextVar('bioma_execution_check', default=None)
_execution_permit = ContextVar('bioma_execution_permit', default=None)


class ExecutionAuthorizationError(ValueError):
    """The current permit no longer authorizes starting another operation."""


def check_execution(tool=None):
    check = _execution_check.get()
    if check:
        check(tool)


def current_execution_permit():
    """Return the live WP1 permit only while its admitted action is executing."""
    permit = _execution_permit.get()
    if permit is None:
        raise ExecutionAuthorizationError('No live WP1 execution context')
    return permit


@lru_cache(maxsize=2048)
def callable_hash(func):
    try:
        return fingerprint(inspect.getsource(func)) if func else ''
    except (OSError, TypeError):
        return fingerprint(repr(func))


def implementation_hash(agent):
    registry = getattr(agent, 'tool_registry', None)
    tools = []
    for tool in registry.all() if registry else ():
        tools.append((tool.name, tool.version, tool.result_kind, callable_hash(tool.func), callable_hash(tool.result_validator)))
    skills = getattr(agent, 'skill_registry', None)
    return fingerprint({'capabilities': [c.model_dump(mode='json') for c in agent.capabilities],
        'tools': tools, 'generation':getattr(registry, 'generation', 0),
        'skills':getattr(skills, 'generation', 0)})


def _rejection_code(exc: Exception) -> str:
    """Typed admission rejection, so failures never collapse into one `failed`."""
    text = str(exc)
    for marker, code in (
        ('Risk ceiling exceeded', 'risk_above_ceiling'),
        ('precondition_failed', 'precondition_failed'),
        ('initial_invariant', 'initial_invariant_failed'),
        ('Resource unavailable', 'resource_unavailable'),
        ('durable execution authority', 'authority_unavailable'),
        ('differs from its accepted assignment', 'proposal_contract_mismatch'),
        ('expands method write authority', 'write_authority_expansion'),
    ):
        if marker in text:
            return code
    return 'admission_rejected'


def _failure_status(exc: Exception) -> str:
    """Distinguish failed / timed out / uncertain / unauthorized execution."""
    from bioma.runtime.deadline import ExecutionTimeout
    from bioma.runtime.effects import EffectUncertain
    if isinstance(exc, ExecutionTimeout):
        return 'timed_out'
    if isinstance(exc, EffectUncertain):
        return 'uncertain'
    if isinstance(exc, ExecutionAuthorizationError):
        return 'mandate_invalid'
    return 'failed'


def _permit_lifetime(method_id):
    """Issue long investigation mandates against the already authorized deadline.

    This is initial admission, never a renewal of an expired/revoked permit.
    Other methods and calls without a runtime deadline keep the 600s default.
    """
    if method_id == 'continuous_scientific_investigation':
        from bioma.runtime.deadline import remaining
        seconds = remaining()
        if seconds is not None:
            return min(1800.0, max(0.0, seconds))
    return 600.0


class TransitionAdmission:
    def __init__(self, ledger=None, *, available_resources=None, memory=None, provider_check=None,
                 capability_runtime=None):
        self.ledger = ledger
        self.available_resources = available_resources
        self.memory = memory
        self.provider_check = provider_check
        self.capability_runtime = capability_runtime

    def record(self, task_id, kind, actor, payload, key=None):
        if self.ledger:
            return self.ledger.append(task_id, kind, actor, payload, idempotency_key=key)

    def _next_attempt(self, task_id, proposal_id):
        """Attempts are numbered per proposal from the journal, never in memory."""
        if self.ledger is None:
            return 1
        return 1 + sum(
            1 for event in self.ledger.events(task_id, kinds=('ExecutionPermitted',))
            if event.kind == 'ExecutionPermitted'
            and event.payload.get('proposal_id') == proposal_id
        )

    def execute(self, agent, state, message):
        if any(c.__module__ == 'bioma.node' and c.__name__ == 'RemoteAgent' for c in type(agent).__mro__):
            raise ExecutionAuthorizationError('RemoteAgent production execution is BLOCKED: direct-backend provenance is not established')
        if any(c.__module__ in {'bioma.agents.queryagent', 'bioma.agents.predagent', 'bioma.agents.comagent', 'bioma.agents.proagent', 'bioma.agents.operagent'} for c in type(agent).__mro__):
            raise ExecutionAuthorizationError('Legacy role execution is retired; use a direct MethodBinding')
        from bioma.core.methods import validate_method_binding, validate_method_result
        validate_method_binding(agent, message)
        if self.provider_check:
            self.provider_check(message)
        context = self.memory.context(message.recipient, state, message) if self.memory else {}
        if context.get('memories'):
            from bioma.runtime.telemetry import emit
            recalled = {'recipient':message.recipient, 'state_version':state.version,
                'memories':[{k:m[k] for k in ('id','owner','shared','layer','source_event','evidence_refs','authority')}
                            for m in context['memories']]}
            self.record(state.task_id, 'MemoryRecalled', message.recipient, recalled)
            emit('memory.recalled', **recalled)
        proposal = agent.propose(state.model_copy(deep=True), message.model_copy(deep=True), context)
        if not isinstance(proposal, TransitionProposal):
            raise ValueError('Agent must propose a typed transition before execution')
        expected = agent.default_proposal(state, message, context)
        # The proposal must bind the accepted registered contract; a provider
        # may narrow its write set, but cannot replace the admitted method.
        for field in ('task_id', 'message_id', 'proposer', 'base_version', 'observation_hash',
                      'capability_id', 'capability_version', 'method_id', 'action', 'input_hash',
                      'read_set', 'supporting_evidence', 'result_contract', 'contract', 'risk'):
            if getattr(proposal, field) != getattr(expected, field):
                raise ValueError('Proposal differs from its accepted assignment: ' + field)
        if not set(proposal.write_set) <= set(expected.write_set):
            raise ValueError('Proposal expands method write authority')
        self.record(state.task_id, 'TransitionProposed', message.recipient,
                    proposal.model_dump(mode='json'), 'proposal:' + proposal.identity)
        attempt = self._next_attempt(state.task_id, proposal.identity)
        symbolic = {'state': state.model_dump(mode='json'), 'message': message.model_dump(mode='json')}
        capability = next((c for c in agent.capabilities if c.id == proposal.capability_id), None)
        resources = proposal.contract.resources
        rule_refs = ['transition-proposal-v1', 'scientific-action-v1']
        try:
            if capability is not None:
                from bioma.capabilities.registry import RISK_ORDER
                if RISK_ORDER[proposal.risk] > RISK_ORDER[capability.max_risk_level]:
                    raise ValueError('Risk ceiling exceeded: proposed ' + proposal.risk +
                                     ' exceeds capability ceiling ' + capability.max_risk_level)
            check_predicates(proposal.contract.preconditions, symbolic, 'precondition_failed')
            check_predicates(proposal.contract.invariants, {**symbolic, 'after': symbolic['state']}, 'initial_invariant')
            if resources and (self.available_resources is None or not set(resources) <= set(self.available_resources)):
                raise ValueError('Resource unavailable: an explicit authoritative inventory is required')
            if resources and self.ledger is None:
                raise ValueError('Resource actions require durable execution authority')
            # Admission issues the execution mandate. The mandate carries the
            # authority ceiling and the effects the executor may produce (I3/I5).
            permitted = sorted(set(proposal.write_set) & set(proposal.contract.writable_fields))
            lifetime = _permit_lifetime(proposal.method_id)
            if proposal.method_id == 'continuous_scientific_investigation':
                rule_refs.append('continuous-runtime-deadline-bound-v1')
            permit = ExecutionPermit(proposal_id=proposal.identity, provider_id=message.recipient,
                implementation_hash=implementation_hash(agent), contract_hash=fingerprint(proposal.contract.model_dump(mode='json')),
                admitted_version=state.version, expires_at=time.time() + lifetime,
                capability_id=proposal.capability_id, target=proposal.target or ','.join(permitted),
                allowed_effects=permitted, risk_ceiling=capability.max_risk_level if capability is not None else proposal.risk,
                evidence_obligations=proposal.evidence_obligations,
                admission_rule_refs=rule_refs,
                issued_at=time.time(), revocable=True, attempt=attempt)
            self.record(state.task_id, 'ExecutionPermitted', 'harness', permit.model_dump(mode='json'),
                        'permit:' + proposal.identity + ':' + str(attempt))
        except Exception as exc:
            self.record(state.task_id, 'TransitionRejected', 'harness',
                        {'proposal_id': proposal.identity, 'phase': 'admission', 'attempt': attempt,
                         'code': _rejection_code(exc), 'reason': str(exc)})
            raise
        with ExitStack() as leases:
            if resources:
                from bioma.runtime.task_lease import task_lease
                from bioma.runtime.ledger import RunLedger
                for resource in sorted(set(resources)):
                    leases.enter_context(task_lease(RunLedger(self.ledger.path), state.task_id,
                                                   lease_key='action-resource:' + resource))
            from bioma.memory.context import agent_context
            def guard(tool=None):
                from bioma.runtime.deadline import check
                check()
                if permit.expires_at <= time.time():
                    raise ExecutionAuthorizationError('Execution permit expired; reproposal required before another operation')
                if self.provider_check:
                    self.provider_check(message)
                if implementation_hash(agent) != permit.implementation_hash:
                    raise ExecutionAuthorizationError('Capability changed during execution; tool call rejected')
                registry = getattr(agent, 'tool_registry', None)
                if tool is not None and registry is not None and registry.get(tool.name) is not tool:
                    from bioma.capabilities.scoped import is_scoped_metadata_tool
                    if not is_scoped_metadata_tool(tool):
                        raise ExecutionAuthorizationError('Tool was removed or replaced; cached tool call rejected')
            started_at = time.time()
            token = _execution_check.set(guard)
            permit_token = _execution_permit.set(permit)
            try:
                if self.provider_check:
                    self.provider_check(message)
                if permit.expires_at <= time.time() or implementation_hash(agent) != permit.implementation_hash:
                    raise ValueError('Execution permit expired or implementation changed')
                if self.memory:
                    self.memory.begin(proposal, context)
                from bioma.runtime.tool_actions import tool_action_scope
                from bioma.runtime.capability_requests import capability_request_scope
                with agent_context(context), tool_action_scope(proposal, self.record, guard), \
                        capability_request_scope(self.capability_runtime, state, message):
                    delta = agent.handle(state.model_copy(deep=True), message.model_copy(deep=True))
                if delta.transition is not None:
                    raise ValueError('Agent cannot supply its own admission proof')
                if set(delta.updates) - set(proposal.write_set):
                    raise ValueError('Actual effects exceed proposed write set')
                if delta.status != 'proposed':
                    raise ValueError('Agent cannot self-verify a transition')
                if implementation_hash(agent) != permit.implementation_hash:
                    raise ValueError('Implementation changed during execution; result requires reconciliation')
                if self.provider_check:
                    self.provider_check(message)
                validate_method_result(message, delta)
                raw_delta = delta
                from bioma.runtime.boundaries import normalize_work_result
                delta = normalize_work_result(state, proposal, raw_delta)
                after = prospective_state(state, delta)
                check_predicates(proposal.contract.invariants,
                    {**symbolic, 'result': delta.model_dump(mode='json'), 'after': after}, 'invariant_failed')
                if delta.outcome not in {'failed', 'blocked'}:
                    check_predicates(proposal.contract.expected_effects,
                        {**symbolic, 'result': delta.model_dump(mode='json'), 'after': after}, 'effect_failed')
                result = ExecutionResult(proposal_id=proposal.identity,
                    delta_hash=fingerprint(delta.model_dump(mode='json')), work_outcome=delta.outcome,
                    evidence_ids=[e.id for e in delta.evidence], artifact_ids=[a.id for a in delta.artifacts],
                    executor=message.recipient, started_at=started_at, ended_at=time.time(),
                    actual_effects=sorted(delta.updates), execution_status='completed',
                    tool_receipts=[], device_receipts=[],
                    idempotency_key=message.idempotency_key or message.id, attempt=attempt)
                self.record(state.task_id, 'ExecutionResultRecorded', message.recipient, result.model_dump(mode='json'),
                            'result:' + proposal.identity + ':' + str(attempt))
                proof = {'proposal': proposal.model_dump(mode='json'),
                    'permit': permit.model_dump(mode='json'), 'result': result.model_dump(mode='json'),
                    'owner_delta': delta.model_dump(mode='json')}
                if raw_delta != delta:
                    proof['raw_owner_delta'] = raw_delta.model_dump(mode='json')
                return delta.model_copy(update={'transition': proof})
            except Exception as exc:
                from bioma.runtime.workers import ProviderUnavailable
                if isinstance(exc, ProviderUnavailable):
                    exc.execution_started = True
                from bioma.llm import ModelBudgetUnavailable
                budget_pending = isinstance(exc, ModelBudgetUnavailable)
                status = getattr(exc, 'execution_status', 'BUDGET_UNAVAILABLE') if budget_pending else _failure_status(exc)
                failure = {'proposal_id': proposal.identity, 'mandate_id': permit.mandate_id,
                           'attempt': attempt, 'attempt_id': permit.attempt_id, 'status': status,
                           'error_type': type(exc).__name__, 'reason': str(exc)}
                self.record(state.task_id, 'ExecutionDeferred' if budget_pending else 'ExecutionAttemptFailed', message.recipient,
                            {**failure, **({'checkpoint': getattr(exc, 'checkpoint', None)} if budget_pending else {})},
                            ('attempt-deferred:' if budget_pending else 'attempt-failed:') + proposal.identity + ':' + str(attempt))
                if status == 'uncertain':
                    # An unresolved external effect must be reconciled, never
                    # silently retried: record that obligation explicitly.
                    required = {'proposal_id': proposal.identity, 'attempt': attempt,
                                'attempt_id': permit.attempt_id, 'effect_status': 'uncertain',
                                'reason': str(exc)}
                    # Journal only: the engine mirrors its own event stream into
                    # the ledger under the same kind, so also emitting here would
                    # double-record the same obligation.
                    self.record(state.task_id, 'reconciliation.required', message.recipient, required,
                                'reconciliation-required:' + proposal.identity + ':' + str(attempt))
                if self.memory:
                    self.memory.fail(proposal, str(exc))
                raise
            finally:
                _execution_permit.reset(permit_token)
                _execution_check.reset(token)


def revalidate_reads(delta, state, message):
    if delta.transition:
        check_read_set(TransitionProposal.model_validate(delta.transition['proposal']), state, message)
