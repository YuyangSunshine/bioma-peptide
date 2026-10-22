"""Domain-neutral input-bound delivery graphs and artifact integrity checks.

Concrete methods supply recipes. Responsibility never implies one evidence type,
and generic task inputs are not coerced into amino-acid sequences.
"""
import hashlib
import json
from pathlib import Path



def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def sequences(inputs):
    from bioma.core.sequence_inputs import canonical_sequence
    values = inputs.get('sequences') or [inputs.get('sequence') or inputs.get('protein_sequence')]
    return list(dict.fromkeys(s for value in values if (s := canonical_sequence(value))))


def task_contracts(task):
    from bioma.core.scientific_capabilities import FAMILIES
    if task.initial_capability in FAMILIES:
        # Provider result contracts and runtime delivery own this path. Legacy
        # recipes must not silently introduce Pred/Com dependencies.
        return []
    gateway = task.gateway
    from bioma.core.methods import entry_result_contract
    rows = contracts_for(task.objective, task.inputs, gateway.get('desired_deliverables', []),
        capability_requirements=gateway.get('capability_requirements', []),
        method_id=gateway.get('selected_method_id') or task.initial_method_id,
        result_contract=entry_result_contract(gateway))
    if gateway.get('method_selection') == 'provider':
        rows = [{**row, 'method_id': None} for row in rows]
    return rows


def contracts_for(objective, inputs, desired_deliverables=(), *, capability_requirements=(),
                  method_id=None, result_contract=None):
    from bioma.capabilities.deliveries import method_deliveries
    return method_deliveries(objective, inputs, desired_deliverables,
                            method_id=method_id, result_contract=result_contract)


def contract_groups(contracts):
    """Only assignments with identical method, message and inputs share execution."""
    groups = {}
    for row in contracts:
        key = (row['capability'], row.get('method_id') or '', row['message_type'], row['input_hash'])
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def seal_delivery(delta, contracts):
    """Provider-side sealing; producer evidence must already identify its input."""
    if not contracts:
        return delta
    keys = {c['input_hash'] for c in contracts}
    if len(keys) != 1:
        raise ValueError('One execution cannot seal unrelated input contracts')
    key = next(iter(keys))
    producer = any(c.get('input_identity') == 'sequence_sha256' for c in contracts)
    if producer and not any(isinstance(e.value, dict) and e.value.get('sequence_sha256') == key for e in delta.evidence):
        raise ValueError('Computed evidence does not match delivery input')
    ids = [c['id'] for c in contracts]
    artifacts = []
    for artifact in delta.artifacts:
        content = Path(artifact.uri).read_bytes()
        artifacts.append(artifact.model_copy(update={'metadata': {**artifact.metadata,
            'input_hash': key, 'delivery_ids': ids, 'sha256': hashlib.sha256(content).hexdigest()}}))
    evidence = [e.model_copy(update={'value': {**(e.value if isinstance(e.value, dict) else {'value': e.value}),
        'input_hash': key, 'delivery_ids': ids}}) for e in delta.evidence]
    return delta.model_copy(update={'artifacts': artifacts, 'evidence': evidence})


def delivery_status(contracts, state):
    rows = []
    for c in contracts:
        matching = [e for e in state.evidence if e.kind == c['evidence'] and isinstance(e.value, dict)
            and e.value.get('input_hash') == c['input_hash'] and c['id'] in e.value.get('delivery_ids', [])]
        evidence_ids = {e.id for e in matching}
        kinds = set()
        files = {}
        for a in state.artifacts:
            if (a.metadata.get('input_hash') != c['input_hash'] or c['id'] not in a.metadata.get('delivery_ids', [])
                    or not evidence_ids.intersection(a.evidence_ids)):
                continue
            try:
                if hashlib.sha256(Path(a.uri).read_bytes()).hexdigest() == a.metadata.get('sha256'):
                    kinds.add(a.kind)
                    files[a.kind] = Path(a.uri)
            except OSError:
                pass
        content_ok = True
        if c.get('candidate_spec'):
            try:
                from bioma.core.candidates import verify_candidate_files
                verify_candidate_files(json.loads(files['candidate-design-json'].read_text(encoding='utf-8')),
                    files['candidate-sequences-fasta'].read_text(encoding='utf-8'),
                    c['input_bindings']['sequence'], c['candidate_spec'])
            except (KeyError, ValueError, TypeError, OSError):
                content_ok = False
        ok = bool(matching) and set(c['artifacts']) <= kinds and content_ok
        rows.append({**c, 'status': 'verified' if ok else 'waiting', 'evidence_ok': bool(matching),
                     'artifacts_ok': set(c['artifacts']) <= kinds and content_ok})
    return rows
