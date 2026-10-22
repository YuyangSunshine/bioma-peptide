"""Durable agent execution bridge used by the Web/Python runtime."""
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
import hashlib
import json
import traceback

from bioma.core.models import StateDelta, Observation, AgentMessage
from uuid import uuid4
from bioma.runtime.distributed import HarnessNode, SQLiteTransport


class DurableExecution:
    def __init__(self, ledger, event_sink=None, *, admission=None):
        self.event_sink = event_sink
        self.ledger = ledger
        self.contexts = {}
        self.node_root = ledger.path.parent / (ledger.path.stem + "-nodes")
        if admission is None:
            from bioma.runtime.admission import TransitionAdmission
            from bioma.memory import MemoryService
            memory = MemoryService(ledger.path.with_name(ledger.path.stem + '-memory.sqlite3'))
            admission = TransitionAdmission(ledger, memory=memory)
        self.admission = admission

    def execute(self, agent, view, message):
        from bioma.core.methods import validate_method_binding
        validate_method_binding(agent, message)
        if self.admission.provider_check:
            self.admission.provider_check(message)
        # Each provider owns its mailbox and journal, rather than sharing one DB.
        node_name = hashlib.sha256(message.recipient.encode()).hexdigest()[:24]
        transport = SQLiteTransport(self.node_root / node_name / 'messages.sqlite3')
        from bioma.runtime.federation import NodeJournal
        journal = NodeJournal(self.node_root / node_name / 'facts.sqlite3', message.recipient)
        payload = {'view': view.model_dump(mode='json'), 'message': message.model_dump(mode='json')}
        # Feedback changes the attempt contract; delivery repeats do not.
        implementation = [cap.model_dump(mode='json') for cap in getattr(agent, 'capabilities', ()) if cap.agent_id == message.recipient]
        identity = {'payload': message.payload, 'implementation': implementation}
        key = message.id + ':' + hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
        topic = 'agent:' + message.recipient + ':' + key
        # Do not include global state version in the retry identity. Persist the
        # original authorized view, and replay that result before revalidation.
        old = transport.load_checkpoint(view.task_id, key)
        original_key, recovery = key, 0
        # A resource pause is not a reusable scientific result. Keep every prior
        # mailbox receipt; the method's hash-bound checkpoint resumes review only.
        while old and old['payload'].get('result', {}).get('budget_pending'):
            recovery += 1
            key = original_key + ':budget-resume:' + str(recovery)
            old = transport.load_checkpoint(view.task_id, key)
        topic = 'agent:' + message.recipient + ':' + key
        if old and 'result' in old['payload']:
            stored = old['payload']['result']
            from bioma.runtime.projections import LocalAgentView
            self.contexts[message.id] = LocalAgentView.model_validate(stored.get('view', payload['view']))
            decoded = self._decode(stored)
            self._publish(journal, view, message, key, decoded)
            return decoded
        with transport._db() as db:
            prior = db.execute('SELECT id,payload FROM messages WHERE run_id=? AND idempotency_key=?', (view.task_id, key)).fetchone()
        if prior:
            persisted = json.loads(prior['payload'])
            if persisted['message'] != payload['message']:
                raise ValueError('Durable assignment identity conflict')
            message_id = prior['id']
        else:
            message_id = transport.publish(view.task_id, topic, payload, key)
        errors = []
        def handler(stored):
            from bioma.runtime.projections import LocalAgentView
            local = LocalAgentView.model_validate(stored["view"])
            delivered = AgentMessage.model_validate(stored["message"])
            self.contexts[message.id] = local
            try:
                from bioma.runtime.telemetry import observe, emit
                with observe(self.event_sink, agent=message.recipient, message_id=message.id, state_version=view.version):
                    emit("agent.started", message=message.model_dump(mode="json"))
                    return {'delta': self.admission.execute(agent, local, delivered).model_dump(mode='json'), 'view': stored['view']}
            except Exception as exc:
                errors.append(exc)
                from bioma.tools.errors import ToolError
                failure = {'error': type(exc).__name__, 'reason': str(exc),
                           'execution_started': getattr(exc, 'execution_started', False),
                           'traceback': traceback.format_exc(),
                           'transport_failure': isinstance(exc, (ConnectionError, TimeoutError))}
                from bioma.llm import ModelBudgetUnavailable
                if isinstance(exc, ModelBudgetUnavailable):
                    failure.update(budget_pending=True, checkpoint=getattr(exc, 'checkpoint', None),
                                   execution_status=getattr(exc, 'execution_status', 'BUDGET_UNAVAILABLE'))
                if isinstance(exc, ToolError):
                    failure['tool_error'] = {name: getattr(exc, name) for name in
                        ('error_type', 'retriable', 'backend', 'retry_after_seconds')}
                return failure
        owner = 'runtime:' + message.recipient + ':' + str(uuid4())
        stop = Event()
        def renew():
            while not stop.wait(5):
                with transport._db() as db:
                    row = db.execute('SELECT attempts FROM messages WHERE id=? AND owner=?', (message_id, owner)).fetchone()
                if row:
                    if not transport.heartbeat(message_id, owner, generation=row['attempts']):
                        if transport.completed_by(message_id, owner, row['attempts']):
                            return
                        errors.append(RuntimeError('Execution lease lost during heartbeat; result cannot be committed.'))
                        return
        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            HarnessNode(owner, transport, {topic}, handler, raise_errors=True).step()
        finally:
            stop.set()
            thread.join()
        result = transport.load_checkpoint(view.task_id, key)
        if errors:
            raise errors[0]
        if result is None:
            raise RuntimeError('Execution lease unavailable; task remains recoverable')
        decoded = self._decode(result['payload']['result'])
        self._publish(journal, view, message, key, decoded)
        return decoded

    @staticmethod
    def _publish(journal, view, message, key, decoded):
        journal.publish(view.task_id + ':' + key, 'ExecutionResult', {
            'run_id': view.task_id, 'message_id': message.id, 'provider': message.recipient,
            'evidence_ids': [e.id for e in decoded.evidence],
            'artifact_hashes': [a.metadata.get('sha256') for a in decoded.artifacts]})

    @staticmethod
    def _decode(result):
        if 'error' in result:
            if result.get('budget_pending'):
                from bioma.llm import ModelBudgetUnavailable, ComputedPendingReview
                if result.get('execution_status') == 'COMPUTED_PENDING_REVIEW':
                    raise ComputedPendingReview(result.get('checkpoint'), result['reason'])
                raise ModelBudgetUnavailable(result['reason'])
            if result['error'] == 'ProviderUnavailable':
                from bioma.runtime.workers import ProviderUnavailable
                raise ProviderUnavailable(result['reason'], execution_started=result.get('execution_started', False))
            from bioma.llm import ModelOutputError
            if result['error'] == 'ModelOutputError':
                raise ModelOutputError(result['reason'])
            if 'tool_error' in result:
                from bioma.tools.errors import ToolError
                raise ToolError(result['reason'], **result['tool_error'])
            from bioma.runtime.effects import EffectUncertain
            if result['error'] == 'EffectUncertain':
                raise EffectUncertain(result['reason'])
            if result.get('transport_failure'):
                raise ConnectionError(result['reason'])
            raise RuntimeError(result['reason'])
        delta = StateDelta.model_validate(result['delta'])
        if 'observations' in delta.updates:
            delta.updates['observations'] = [Observation.model_validate(o) for o in delta.updates['observations']]
        return delta

    def parallel(self, assignments):
        token = getattr(self.ledger.lease_context, 'token', None)
        def run(agent, view, message):
            self.ledger.lease_context.token = token
            try:
                return self.execute(agent, view, message)
            except Exception as exc:
                return exc
            finally:
                self.ledger.lease_context.token = None
        with ThreadPoolExecutor(max_workers=min(4, len(assignments))) as pool:
            futures = [pool.submit(copy_context().run, run, *assignment) for assignment in assignments]
            return [future.result() for future in futures]
