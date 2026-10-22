from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .ledger import LedgerEvent, replay
from bioma.core.models import ScientificState, ScientificTask


@dataclass(frozen=True)
class ProjectionPolicy:
    agent_id: str
    readable_kinds: frozenset[str]
    allow_own_events: bool = True
    redact_fields: frozenset[str] = frozenset({"api_key", "token", "secret"})


def authorized_projection(events: Iterable[LedgerEvent], policy: ProjectionPolicy) -> dict[str, Any]:
    selected = []
    for event in events:
        if event.kind not in policy.readable_kinds and not (policy.allow_own_events and event.actor == policy.agent_id):
            continue
        payload = {key: value for key, value in event.payload.items() if key.casefold() not in policy.redact_fields}
        selected.append(LedgerEvent(event.sequence, event.event_id, event.run_id, event.kind, event.actor, payload, event.idempotency_key, event.previous_hash, event.event_hash, event.created_at))
    graph = replay(selected)
    graph["agent_id"] = policy.agent_id
    graph["source_event_count"] = len(selected)
    return graph


# Field classes describe access, not the scientific credibility of their contents.
# Reference-scoped records require explicit input/dependency IDs below. No family
# grant overrides that scope. Unknown execution owners get lifecycle metadata only.
_LIFECYCLE_FIELDS = frozenset({"task_id", "objective", "version", "status", "cycle", "max_cycles"})
_CONTRACT_FIELDS = frozenset({"active_agents", "intent", "scope_reason", "initial_capability",
    "initial_method_id", "initial_message_type", "constraints", "success_criteria",
    "task_requests", "offers", "commitments", "control_records"})
_REFERENCED_FIELDS = frozenset({"inputs", "evidence", "artifacts", "observations"})
_CAPABILITY_FIELDS = {
    "scientific.research": _REFERENCED_FIELDS | {"gateway"},
    "scientific.compute": _REFERENCED_FIELDS | {"gateway", "predictions"},
    "scientific.study_design": _REFERENCED_FIELDS | {"predictions"},
    "scientific.execution": _REFERENCED_FIELDS,
    "scientific.evidence_assessment": _REFERENCED_FIELDS | {"gateway", "predictions", "metrics"},
}


def assignment_fields(backend, recipient, message):
    """Resolve a host-registered Method's field ceiling; payload claims grant nothing."""
    from bioma.backends.base import ScientificBackend
    if (not isinstance(backend, ScientificBackend) or message is None
            or backend.provider_id != recipient or message.recipient != recipient):
        return _LIFECYCLE_FIELDS, False
    binding = backend.bindings.get(message.payload.get("method_id"))
    if (binding is None or not binding.method.available
            or binding.method.message_type != message.message_type):
        return _LIFECYCLE_FIELDS, False
    fields = _CAPABILITY_FIELDS.get(backend.family)
    if fields is None or binding.method.result.responsibility != backend.family:
        return _LIFECYCLE_FIELDS, False
    review = (backend.family == "scientific.evidence_assessment"
              and binding.method.id == "assess_evidence"
              and "EpistemicQualification" in binding.method.result.evidence_kinds)
    return _LIFECYCLE_FIELDS | _CONTRACT_FIELDS | fields, review


class LocalAgentView(ScientificState):
    """Compatibility view containing only the authorized event projection."""


def local_agent_view(ledger, state, message, readable_fields, *, review_authority=False):
    events = ledger.events(state.task_id, kinds=('TransitionCommitted', 'RunInitialized'), latest=True) if ledger else ()
    committed = next((e for e in reversed(events) if e.kind in {'TransitionCommitted', 'RunInitialized'}), None)
    source = ScientificState.model_validate(committed.payload['after']) if committed else state
    data = ScientificState.from_task(ScientificTask(id=state.task_id, objective=state.objective)).model_dump()
    for field in readable_fields:
        if field in type(source).model_fields:
            data[field] = getattr(source, field)
    data["inputs"] = {}
    data["metrics"] = {}
    # Protocol envelopes provide explicit reference and input grants. A whole
    # task's input dictionary is never needed by a scoped producer.
    inputs = message.payload.get('inputs')
    if isinstance(inputs, dict) and "inputs" in readable_fields:
        data['inputs'] = inputs
    control_refs = {value for key, value in message.payload.items()
                    if key.endswith('control_id') and isinstance(value, str)}
    data['control_records'] = [row for row in source.control_records if row.id in control_refs]
    contracts = message.payload.get('delivery_contracts', [])
    dependencies = {d for c in contracts for d in c.get('depends_on', [])}
    references = {v for k, v in message.payload.items() if k.endswith('evidence_id') and isinstance(v, str)}
    references.update(v for v in message.payload.get('dependency_evidence_ids', []) if isinstance(v, str))
    references.update(v for v in message.payload.get('input_refs', []) if isinstance(v, str) and v in {e.id for e in source.evidence})
    references.update(e.id for e in source.evidence if isinstance(e.value, dict)
        and dependencies.intersection(e.value.get('delivery_ids', [])))
    by_id = {e.id: e for e in source.evidence}
    from bioma.core.research import contract_for
    contract = contract_for(source)
    review = (review_authority and contract is not None
              and message.payload.get("review_scope") == contract.identity)
    if review:
        # Explicit read grant for this run's immutable research contract. Other
        # agents and ordinary analysis assignments keep their narrow projection.
        references.update(by_id)
        data['inputs'] = source.inputs
        if 'research_review' in source.metrics:
            data['metrics'] = {'research_review': source.metrics['research_review']}
    pending = list(references)
    while pending:
        item = by_id.get(pending.pop())
        if item:
            for parent in item.parent_ids:
                if parent not in references:
                    references.add(parent)
                    pending.append(parent)
    data['evidence'] = [e for e in source.evidence if e.id in references]
    data['artifacts'] = [a for a in source.artifacts if references.intersection(a.evidence_ids)]
    data['predictions'] = [p for p in source.predictions if p.get('evidence_id') in references]
    observation_ids = {e.value.get('observation_id') for e in data['evidence'] if isinstance(e.value, dict)}
    observation_ids.update(v for v in message.payload.get('input_refs', []) if isinstance(v, str))
    data['observations'] = [o for o in source.observations if o.id in observation_ids]
    # Committed raw results may have files before scientific qualification.
    # Carry only the files referenced by explicitly granted observations.
    artifact_ids = {ref for o in data['observations'] for ref in o.artifact_ids}
    artifact_ids.update(a['id'] for o in data['observations'] if o.kind == 'work_result'
        for a in o.value.get('artifacts', []) if isinstance(a, dict) and a.get('id'))
    if 'artifacts' in readable_fields:
        data['artifacts'] = [a for a in source.artifacts
            if a.id in artifact_ids or references.intersection(a.evidence_ids)]

    # Remove coordination history unrelated to this assignment.
    data['task_requests'] = [r for r in source.task_requests if r.id == message.task_request_id]
    data['offers'] = [o for o in source.offers if o.task_request_id == message.task_request_id]
    data['commitments'] = [c for c in source.commitments if c.task_request_id == message.task_request_id]
    if review:
        # Full-contract review must inspect raw work and its files even before
        # qualification; visibility is not scientific credit or tool permission.
        data['observations'] = source.observations
        data['artifacts'] = source.artifacts
        data['predictions'] = source.predictions
        data['task_requests'] = source.task_requests
        data['commitments'] = source.commitments
    # Derived relationships may narrow, but must never enlarge, the field grant.
    defaults = ScientificState.from_task(ScientificTask(id=state.task_id, objective=state.objective)).model_dump()
    for field in data:
        if field not in readable_fields:
            data[field] = defaults[field]
    return LocalAgentView.model_validate(data).model_copy(deep=True)


def merge_committed_child(parent: ScientificState, child: ScientificState):
    """Project independently committed child WP2/WP3 results; invent nothing."""
    update = {}
    for field in ("evidence", "artifacts", "constraints", "observations", "provenance",
                  "task_requests", "offers", "commitments"):
        current = list(getattr(parent, field))
        known = {item.id for item in current}
        current.extend(item for item in getattr(child, field) if item.id not in known)
        update[field] = current
    if all(update[field] == getattr(parent, field) for field in update):
        return parent
    return parent.model_copy(update={**update, "version": parent.version + 1,
                                     "status": "running",
                                     "status_reason": dict(parent.status_reason)})
