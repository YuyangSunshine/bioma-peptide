"""Typed, closed-world scientific guards. No model-produced executable expressions."""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import Field

from bioma.core.community import Contract


class Predicate(Contract):
    id: str = Field(min_length=1)
    source: Literal['state', 'message', 'result', 'after'] = 'state'
    path: str = Field(min_length=1)
    operator: Literal['exists', 'eq', 'gte', 'lte', 'in', 'nonempty', 'evidence_reference', 'same_as'] = 'exists'
    value: Any = None
    unit: str | None = None


class ScientificActionContract(Contract):
    schema_version: Literal['scientific-action-v1'] = 'scientific-action-v1'
    preconditions: list[Predicate] = Field(default_factory=list)
    invariants: list[Predicate] = Field(default_factory=list)
    expected_effects: list[Predicate] = Field(default_factory=list)
    writable_fields: list[str] = Field(default_factory=lambda: [
        'cycle', 'predictions', 'protocol', 'machine_code', 'observations', 'metrics', 'agent_status'])
    resources: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=lambda: [
        'precondition_failed', 'resource_busy', 'execution_failed', 'execution_uncertain', 'evidence_rejected'])
    recovery_actions: dict[str, str] = Field(default_factory=lambda: {
        'precondition_failed': 'repropose', 'resource_busy': 'wait',
        'execution_failed': 'review_failure', 'execution_uncertain': 'reconcile',
        'evidence_rejected': 'request_evidence'})
    risk_level: Literal['low', 'medium', 'high'] = 'low'


MISSING = object()


def lookup(value, path):
    """Dotted keys/indices, array.length and optional JSON root; never eval."""
    if path == '$':
        return value
    if path.startswith('$.'):
        path = path[2:]
    for part in path.split('.'):
        if not part:
            return MISSING
        if isinstance(value, dict):
            value = value.get(part, MISSING)
        elif isinstance(value, list) and part == 'length':
            value = len(value)
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def evaluate(predicate: Predicate, context: dict) -> bool:
    actual = lookup(context.get(predicate.source, {}), predicate.path)
    if actual is MISSING or actual is None:
        return False
    if predicate.operator == 'evidence_reference':
        return isinstance(actual, str) and any(
            e.get('id') == actual and (predicate.value is None or
                (isinstance(predicate.value,dict) and all(type(lookup(e,k)) is type(v) and lookup(e,k)==v for k,v in predicate.value.items())) or
                (isinstance(predicate.value,str) and e.get('kind') == predicate.value))
            for e in context.get('state', {}).get('evidence', []))
    if predicate.operator == 'same_as':
        reference = predicate.value
        if not isinstance(reference, dict) or reference.get('source') not in {'state','message','result','after'} or not isinstance(reference.get('path'), str):
            return False
        expected = lookup(context.get(reference['source'], {}), reference['path'])
        return expected is not MISSING and type(actual) is type(expected) and actual == expected
    if predicate.unit is not None:
        if not isinstance(actual, dict) or actual.get('unit') != predicate.unit:
            return False
        actual = actual.get('value', MISSING)
    if predicate.operator == 'exists':
        return actual is not MISSING and actual is not None
    if predicate.operator == 'nonempty':
        return isinstance(actual, (str, list, dict)) and bool(actual)
    if predicate.operator == 'eq':
        return type(actual) is type(predicate.value) and actual == predicate.value
    if predicate.operator == 'in':
        return isinstance(predicate.value, list) and any(type(actual) is type(v) and actual == v for v in predicate.value)
    if type(actual) not in (float, int) or type(predicate.value) not in (float, int):
        return False
    if not math.isfinite(actual) or not math.isfinite(predicate.value):
        return False
    return actual >= predicate.value if predicate.operator == 'gte' else actual <= predicate.value


def check_predicates(predicates, context, phase):
    failed = [p.id for p in predicates if not evaluate(p, context)]
    if failed:
        raise ValueError(f'{phase}: symbolic predicates failed or unknown: {failed}')


def prospective_state(state, delta):
    after = state.model_dump(mode='json')
    for key, value in delta.model_dump(mode='json')['updates'].items():
        if key in state.APPEND_FIELDS:
            after[key] = [*after[key], *value]
        elif key in state.MERGE_FIELDS:
            after[key] = {**after[key], **value}
        else:
            after[key] = value
    for key in ('evidence', 'artifacts', 'constraints'):
        after[key] = [*after[key], *[x.model_dump(mode='json') for x in getattr(delta, key)]]
    return after
