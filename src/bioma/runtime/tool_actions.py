"""Subordinate tool attempts: admission before dispatch, observation before commit.

These events do not commit scientific state. The enclosing method's verifier and
ledger commit remain authoritative. EffectManager alone owns effect deduplication.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from bioma.core.research import fingerprint
from bioma.runtime.telemetry import emit

_scope = ContextVar('bioma_tool_action_scope', default=None)
_active = ContextVar('bioma_active_tool_action', default=None)
_action_identity = ContextVar('bioma_harness_action_identity', default=None)


def current_tool_action():
    """Recorded dispatch identity, never a permission or proof of file access."""
    value = _active.get()
    return dict(value) if value else None


@contextmanager
def action_identity_scope(identity):
    token = _action_identity.set(identity)
    try:
        yield
    finally:
        _action_identity.reset(token)


@contextmanager
def tool_action_scope(proposal, record, guard):
    token = _scope.set((proposal, record, guard))
    try:
        yield
    finally:
        _scope.reset(token)


def invoke(name, arguments, action, *, version='unversioned', external=False,
           resources=(), action_instance_id=None, tool_grant_ref=None,
           tool_grant_context=None):
    from bioma.backends.analysis.processes import check_analysis_paths
    check_analysis_paths(arguments)
    from bioma.llm import reserve_tool_dispatch
    scope = _scope.get()
    attempt = str(uuid4())
    action_instance_id = action_instance_id or _action_identity.get()
    if scope is None:
        reserve_tool_dispatch(name)
        token = _active.set(dict(attempt_id=attempt, tool=name, tool_version=version,
            input_hash=fingerprint(arguments), action_instance_id=action_instance_id,
            proposal_id=None, authority='standalone; no method authorization asserted'))
        try:
            return action()
        finally:
            _active.reset(token)
    parent, record, guard = scope
    base = dict(attempt_id=attempt, proposal_id=parent.identity, tool=name,
        tool_version=version, input_hash=fingerprint(arguments),
        base_version=parent.base_version, action_instance_id=action_instance_id,
        external_effect=external, resources=list(resources),
        tool_grant_ref=tool_grant_ref, tool_grant_context=tool_grant_context,
        scientific_commit='pending_parent_verification')

    def publish(kind, **extra):
        payload = {**base, **extra}
        record(parent.task_id, kind, parent.proposer, payload)
        emit('protocol.tool_action', agent=parent.proposer, stage=kind, **payload)

    publish('ToolActionProposed')
    try:
        guard()
        publish('ToolActionPermitted')
        reserve_tool_dispatch(name)
        token = _active.set(base)
        try:
            result = action()
        finally:
            _active.reset(token)
        guard()  # Late results after revocation cannot become accepted observations.
        publish('ToolActionObserved')
        return result
    except Exception as exc:
        # Never assert that a timed-out external effect did not occur.
        publish('ToolActionUnresolved' if external else 'ToolActionFailed',
                error_type=type(exc).__name__)
        raise
