"""Explicit, use-scoped historical support checks under the current WP1 permit.

Resources are trusted-host configuration, never model inputs or retrieved memory.
No scientific state is committed here; observations follow normal Action tracing.
"""
from dataclasses import asdict, dataclass
import time
from typing import Callable
from pydantic import Field

from bioma.core.models import StrictModel, ScientificState
from bioma.core.scientific_applicability import assess_support_use
from bioma.core.source_inputs import verify_source_identities
from bioma.core.research import fingerprint
from bioma.runtime.ledger import RunLedger


class SupportUseRequest(StrictModel):
    claim_id: str = Field(min_length=1)
    support_link_id: str = Field(min_length=1)
    use: str = Field(min_length=1, description="Exact intended use admitted by the host; not an authorization.")


@dataclass(frozen=True)
class SupportUseResource:
    """Host-authorized read of one historical support bundle for one new use.

    resolve independently checks the selected bundle and actual source bytes,
    returning (baseline identities, current identities, complete dependency keys)
    or None. It must not infer semantics or completeness from model declarations.
    This is a bounded resource binding, not a new scientific result registry.
    """
    target_task_id: str
    recipient: str
    use: str
    claim_id: str
    support_link_id: str
    source_ledger: str
    source_task_id: str
    source_event_hash: str
    expires_at: float
    checker_id: str
    resolve: Callable | None = None


def admitted_request_schema():
    """Expose only host-authorized exact identities, without granting access.

    The live request is still reauthorized independently; schema discovery does
    not extend expiry and does not pick a scientific action for the Agent.
    """
    from typing import Literal
    from pydantic import create_model
    from bioma.runtime.admission import current_execution_permit, ExecutionAuthorizationError
    from bioma.runtime.capability_requests import _scope
    try:
        runtime, state, _ = _scope()
        recipient = current_execution_permit().provider_id
    except (ExecutionAuthorizationError, ValueError):
        return SupportUseRequest
    rows = [r for r in runtime.support_use_resources if r.target_task_id == state.task_id
            and r.recipient == recipient and r.expires_at > time.time()]
    if not rows:
        return SupportUseRequest
    fields = {}
    for name in ('claim_id', 'support_link_id', 'use'):
        values = tuple(sorted({getattr(row, name) for row in rows}))
        fields[name] = (Literal[values], Field(description='Exact host-admitted ' + name))
    return create_model('AdmittedSupportUseRequest', __base__=SupportUseRequest, **fields)


def check_support_use(arguments):
    from bioma.runtime.capability_requests import _scope
    from bioma.runtime.admission import current_execution_permit, check_execution
    runtime, state, message = _scope()
    request = SupportUseRequest.model_validate(arguments)
    permit = current_execution_permit()
    candidates = [r for r in runtime.support_use_resources
        if r.target_task_id == state.task_id and r.recipient == permit.provider_id
        and r.use == request.use and r.claim_id == request.claim_id
        and r.support_link_id == request.support_link_id and r.expires_at > time.time()]
    if len(candidates) != 1:
        raise ValueError("No unique live host authorization for this support use")
    resource = candidates[0]
    historical = RunLedger.read_historical(resource.source_ledger, resource.source_task_id)
    event = next((e for e in historical['events'] if
        e['event_hash'] == resource.source_event_hash and e['kind'] == 'TransitionCommitted'), None)
    if event is None:
        raise ValueError("Authorized historical support event unavailable")
    source_state = ScientificState.model_validate(event['payload']['after'])
    # Only the explicitly selected support bundle is passed to the checker.
    by_id = {e.id: e for e in source_state.evidence}
    link = by_id.get(request.support_link_id)
    ids = {request.claim_id, request.support_link_id}
    if link is not None:
        ids.update(link.parent_ids)
    bundle = source_state.model_copy(deep=True, update={
        'evidence': [e.model_copy(deep=True) for e in source_state.evidence if e.id in ids]})
    if not runtime.ledger.verify_chain(state.task_id):
        raise ValueError("Current research Ledger integrity failed")
    events = [e for e in runtime.ledger.events(state.task_id)
              if e.kind in {'RunInitialized', 'TransitionCommitted'}]
    if not events:
        raise ValueError("Current research has no recorded input version")
    current_event = events[-1]
    current_state = ScientificState.model_validate(current_event.payload['after'])
    # A model-edited in-memory input cannot replace the admitted version.
    if fingerprint(current_state.inputs) != fingerprint(state.inputs):
        raise ValueError("Current inputs differ from the recorded research version")
    baseline_sources = source_state.inputs.get('source_inputs', [])
    current_sources = current_state.inputs.get('source_inputs', [])
    identity = {'source_task_id': resource.source_task_id,
        'source_event_id': event['event_id'], 'source_event_hash': event['event_hash'],
        'current_task_id': state.task_id, 'current_event_id': current_event.event_id,
        'current_event_hash': current_event.event_hash,
        'baseline_source_refs': [s.get('id') for s in baseline_sources],
        'current_source_refs': [s.get('id') for s in current_sources],
        'checker_id': resource.checker_id,
        'revision_scope': 'recorded source-version pair; not an asserted scientific correction'}
    gap = 'applicable_checker_unavailable'
    resolved = None
    if resource.resolve is not None:
        try:
            old = verify_source_identities(baseline_sources)
            new = verify_source_identities(current_sources)
            if not old or not new:
                gap = 'recorded_source_identity_missing'
            else:
                # Do not expose unrelated history or unrecorded current fields.
                resolved = resource.resolve(tuple(bundle.evidence), request.use, tuple(old), tuple(new))
                gap = 'complete_dependencies_not_verified'
                # Catch modification during an independent check as well.
                verify_source_identities(baseline_sources)
                verify_source_identities(current_sources)
        except (OSError, ValueError) as exc:
            gap = 'source_or_dependency_check_failed:' + type(exc).__name__
            resolved = None
    baseline, current, dependencies = ({}, {}, None) if resolved is None else resolved
    assessment = assess_support_use(source_state, claim_id=request.claim_id,
        support_link_id=request.support_link_id, use=request.use,
        baseline=baseline, current=current, check_dependencies=lambda *_: dependencies)
    check_execution()
    if resource.expires_at <= time.time():
        raise ValueError("Support-use authorization expired during check")
    # Detect historical/current record changes while the checker was running.
    after = RunLedger.read_historical(resource.source_ledger, resource.source_task_id)
    if after['last_event_hash'] != historical['last_event_hash']:
        raise ValueError("Historical record advanced during support check; retry against explicit version")
    latest = [e for e in runtime.ledger.events(state.task_id)
              if e.kind in {'RunInitialized', 'TransitionCommitted'}]
    if not runtime.ledger.verify_chain(state.task_id) or latest[-1].event_hash != current_event.event_hash:
        raise ValueError("Current research version changed during support check")
    result = asdict(assessment)
    if result['historical_status'] != 'supported':
        result['status'] = 'unknown'
        result['gaps'] = sorted(set(result['gaps']) | {'historical_claim_not_supported'})
    if resolved is None:
        result['status'] = 'unknown'
        result['gaps'] = sorted(set(result['gaps']) | {gap})
    return {**result, 'revision': identity,
        'dependencies': sorted(dependencies) if isinstance(dependencies, frozenset) else [],
        'baseline_conditions': baseline, 'current_conditions': current,
        'authority': 'use-scoped assessment only; no execution permission or new Evidence',
        'historical_state_changed': False}
