"""Conservative side-effect reconciliation; never infer absence from timeout."""
from uuid import uuid4
from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
import json

_context = ContextVar("bioma_effect_context", default=None)
_action_identity = ContextVar('bioma_action_instance', default=None)


def current_action_instance_id():
    """Acquisition adapters forward this ID as the device/API idempotency key."""
    value = _action_identity.get()
    if value is None:
        raise RuntimeError('No managed action is being dispatched')
    return value


def plan_current_action(step_id: str) -> str:
    """Persist and return a stable logical action identity before dispatch."""
    context = _context.get()
    if context is None:
        raise RuntimeError('Action planning requires a durable effect_scope')
    manager, run_id = context
    return manager.plan_action(run_id, step_id)

@contextmanager
def effect_scope(manager, run_id):
    token = _context.set((manager, run_id))
    try:
        yield
    finally:
        _context.reset(token)

def execute_tool(tool, kwargs, *, action_instance_id=None):
    context = _context.get()
    if not tool.external_effect:
        return tool.func(**kwargs)
    if context is None:
        if tool.replay_safe:
            return tool.func(**kwargs)
        raise ValueError('External actions require a durable effect_scope')
    manager, run_id = context
    manager.authorize_tool(tool, run_id, kwargs,
                           action_instance_id=action_instance_id)
    contract = {"tool": tool.name, "version": tool.version, "arguments": kwargs}
    if action_instance_id is None:
        if not tool.replay_safe:
            raise ValueError('External actions require a persisted action_instance_id')
        action_instance_id = 'replay-safe:' + hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not isinstance(action_instance_id, str) or not action_instance_id.strip() or len(action_instance_id) > 256:
        raise ValueError('Invalid action_instance_id')
    contract['action_instance_id'] = action_instance_id
    legacy = {k: v for k, v in contract.items() if k != 'action_instance_id'}
    if not tool.replay_safe and any(e.kind.startswith('Effect') and e.payload.get('contract') == legacy
                                   for e in manager.ledger.events(run_id)):
        raise EffectUncertain('Legacy parameter-keyed effect requires explicit migration/reconciliation')
    key = hashlib.sha256(('action-v2:' + action_instance_id).encode()).hexdigest()
    def dispatch():
        token = _action_identity.set(action_instance_id)
        try:
            return tool.func(**kwargs)
        finally:
            _action_identity.reset(token)
    return manager.execute(run_id, key, contract, dispatch, reconcile=tool.reconcile_effect, replay_safe=tool.replay_safe)



class EffectUncertain(RuntimeError):
    pass


class EffectManager:
    def __init__(self, ledger, *, trust=None, principal=None, resources=()):
        self.ledger = ledger
        self.trust, self.principal, self.resources = trust, principal, frozenset(resources)

    def authorize_tool(self, tool, run_id, arguments=None, *, action_instance_id=None):
        arguments = arguments or {}
        if tool.category == 'paid-remote-compute':
            ceiling = arguments.get('max_credits')
            estimate = arguments.get('estimated_credits')
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       and value > 0 for value in (ceiling, estimate)):
                raise PermissionError('Paid remote compute requires credit limits')
            prior = {}
            for event in self.ledger.events(run_id):
                if event.kind != 'EffectStarted':
                    continue
                contract = event.payload.get('contract') or {}
                prior_args = contract.get('arguments') or {}
                prior_id = contract.get('action_instance_id')
                if (contract.get('tool', '').startswith('neurosnap_submit_')
                        and prior_id and prior_id != action_instance_id):
                    prior.setdefault(prior_id, float(prior_args.get('estimated_credits') or 0))
            if sum(prior.values()) + float(estimate) > float(ceiling):
                raise PermissionError('NeuroSnap run credit ceiling would be exceeded')
        if self.trust is None or tool.replay_safe:
            return
        self.trust.require(self.principal, 'effect.execute', run_id=run_id)
        if not tool.resources or not set(tool.resources) <= self.resources:
            raise PermissionError('External action must use declared and leased resources')
        for resource in tool.resources:
            self.trust.require(self.principal, 'resource.use', run_id=run_id, resource=resource)

    def plan_action(self, run_id, step_id):
        """Persist a logical plan step's identity BEFORE dispatching any effect.

        Use a different step_id for a new preparation/replicate/iteration.
        Restoring the same plan step returns the original action identity.
        """
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError('A stable plan step is required')
        from uuid import uuid5, NAMESPACE_URL
        identity = str(uuid5(NAMESPACE_URL, json.dumps(['bioma-action-v1', run_id, step_id])))
        event = self.ledger.append(run_id, 'ActionPlanned', 'harness',
            {'step_id': step_id, 'action_instance_id': identity}, idempotency_key='action-plan:' + identity)
        return event.payload['action_instance_id']

    def execute(self, run_id, key, contract, action, *, reconcile=None, replay_safe=False):
        from bioma.runtime.task_lease import task_lease
        from bioma.runtime.ledger import RunLedger
        contract = {**contract, 'replay_safe': replay_safe}
        lock_ledger = RunLedger(self.ledger.path)
        with task_lease(lock_ledger, run_id, lease_key='effect:' + run_id + ':' + key):
            return self._execute(run_id, key, contract, action, reconcile=reconcile)

    def _execute(self, run_id, key, contract, action, *, reconcile=None):
        # Only effect-lifecycle events define the effect contract. Reconciliation
        # events also carry `effect_key`, but they describe an outcome rather
        # than the effect itself, so they must not be read as contract records.
        events = [e for e in self.ledger.events(run_id)
                  if e.kind.startswith('Effect') and e.payload.get('effect_key') == key]
        for event in events:
            if event.payload.get('contract') != contract:
                raise ValueError('Effect idempotency contract conflict')
        completed = next((e for e in reversed(events) if e.kind == 'EffectCompleted'), None)
        if completed:
            return completed.payload['result']
        recovered = False
        if events:
            started_at = next(e.created_at for e in reversed(events) if e.kind == 'EffectStarted')
            status, result = self._reconcile(run_id, key, contract, started_at, reconcile)
            recovered = status == 'succeeded'
            if not recovered and not contract['replay_safe']:
                raise EffectUncertain('External effect unresolved; automatic replay prohibited')
        if not recovered:
            attempt = sum(e.kind == 'EffectStarted' for e in events) + 1
            started = self.ledger.append(run_id, 'EffectStarted', 'harness',
                {'effect_key': key, 'contract': contract, 'token': str(uuid4()), 'attempt': attempt},
                idempotency_key=f'effect:start:{key}:{attempt}')
            try:
                result = action()
            except Exception as exc:
                self.ledger.append(run_id, 'EffectFailed' if contract['replay_safe'] else 'EffectUncertain', 'harness',
                    {'effect_key': key, 'contract': contract, 'error_type': type(exc).__name__, 'attempt': attempt})
                status, result = self._reconcile(run_id, key, contract, started.created_at, reconcile)
                if status != 'succeeded':
                    if contract['replay_safe']:
                        raise
                    raise EffectUncertain('External effect requires reconciliation') from exc
        self.ledger.append(run_id, 'EffectCompleted', 'harness',
            {'effect_key': key, 'contract': contract, 'result': result}, idempotency_key='effect:done:'+key)
        return result

    # A reconciliation outcome is a protocol event in its own right: the
    # obligation is only closed when the adapter actually reports a verdict.
    _RECONCILIATION_KIND = {'succeeded': 'reconciliation.completed',
                            'failed': 'reconciliation.failed'}

    def _record_reconciliation(self, run_id, key, contract, status):
        kind = self._RECONCILIATION_KIND.get(status, 'reconciliation.required')
        attempt = 1 + sum(
            1 for event in self.ledger.events(run_id)
            if event.kind == kind and event.payload.get('effect_key') == key
        )
        payload = {'effect_key': key, 'contract': contract, 'status': status,
                   'attempt': attempt,
                   'action_instance_id': contract.get('action_instance_id')}
        self.ledger.append(run_id, kind, 'harness', payload,
                           idempotency_key=kind + ':' + key + ':' + str(attempt))

    def _reconcile(self, run_id, key, contract, started_at, adapter):
        try:
            status, result = adapter({**contract, 'effect_started_at': started_at}) if adapter else ('unknown', None)
        except Exception:
            status, result = 'unknown', None
        self.ledger.append(run_id, 'EffectReconciled', 'harness',
            {'effect_key': key, 'contract': contract, 'status': status})
        self._record_reconciliation(run_id, key, contract, status)
        return status, result

    def compensate(self, run_id, key, contract, action, *, reconcile=None):
        # Compensation is another effect and can itself lose its response.
        return self.execute(run_id, 'compensate:'+key, contract, action, reconcile=reconcile)
