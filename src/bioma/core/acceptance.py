"""Immutable acceptance policy: owner work, scientific review, then delivery.

A research contract must carry its matching acceptance protocol.
Unscoped worker tasks use the low-level runtime without research closure.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field

from bioma.core.research import ResearchModel, contract_for, fingerprint


class AcceptanceProtocol(ResearchModel):
    schema_version: Literal['acceptance-v1'] = 'acceptance-v1'
    contract_id: str
    mode: Literal['work', 'research']
    delivery_requirement_ids: list[str] = Field(default_factory=list)


def protocol_for(state):
    raw = state.gateway.get('acceptance_protocol')
    if not raw:
        if contract_for(state) is not None:
            raise ValueError('Research contract requires an acceptance protocol; start a new run')
        return None
    protocol = AcceptanceProtocol.model_validate(raw)
    contract = contract_for(state)
    if contract is None or protocol.contract_id != contract.identity:
        raise ValueError('Acceptance protocol must bind the exact research contract')
    ids = set(protocol.delivery_requirement_ids)
    known = {r.id: r for r in contract.requirements}
    if len(ids) != len(protocol.delivery_requirement_ids) or ids - known.keys():
        raise ValueError('Invalid final delivery requirements')
    if any(known[i].mode == 'claim_assessment' for i in ids):
        raise ValueError('A scientific claim cannot be deferred as file delivery')
    if any(ids.intersection(r.depends_on) for r in contract.requirements if r.id not in ids):
        raise ValueError('Scientific acceptance cannot depend on post-review delivery')
    if protocol.mode == 'work' and any(r.mode == 'claim_assessment' for r in contract.requirements):
        raise ValueError('Scientific claim assessment requires research acceptance')
    return protocol


def acceptance_mode(task):
    mode = task.gateway.get('acceptance_mode', 'auto')
    if mode not in {'auto', 'work', 'research'}:
        raise ValueError('Unknown acceptance mode')
    # A scientific hypothesis/decision or experimental cycle cannot be silently
    # downgraded into a file-existence check by the Gateway.
    if task.intent in {'closed_loop', 'evidence_synthesis'} or any(
            c.get('kind') in {'hypothesis', 'decision'} for c in task.gateway.get('claims', [])):
        return 'research'
    if mode != 'auto':
        return mode
    # File/value obligations are also required for bounded work. Their mere
    # presence must not force every calculation through scientific review.
    return 'research' if task.gateway.get('claims') else 'work'


def bind_protocol(task):
    contract = contract_for(task)
    mode = acceptance_mode(task)
    explicit = task.gateway.get('delivery_requirement_ids')
    if explicit is None:
        explicit = [r.id for r in contract.requirements
                    if r.mode == 'delivery' and r.stage == 'delivery']
        if not any(r.id not in explicit and r.mode != 'claim_assessment' for r in contract.requirements):
            explicit = []
    protocol = AcceptanceProtocol(contract_id=contract.identity, mode=mode, delivery_requirement_ids=explicit)
    result = task.model_copy(update={'gateway': {**task.gateway, 'acceptance_protocol': protocol.model_dump(mode='json')}})
    protocol_for(result)
    return result


def review_requirements(state):
    contract = contract_for(state)
    protocol = protocol_for(state)
    if protocol is None:
        return []
    if protocol.mode == 'work':
        return []
    return [r for r in contract.requirements if r.id not in protocol.delivery_requirement_ids]


def required_ids(contract):
    """A prerequisite of a required node is required even if not independently requested."""
    rows = {r.id:r for r in contract.requirements}
    required = {r.id for r in contract.requirements if r.mandatory}
    pending = list(required)
    while pending:
        for parent in rows[pending.pop()].depends_on:
            if parent not in required:
                required.add(parent)
                pending.append(parent)
    return required


def is_delivery(state, message):
    protocol = protocol_for(state)
    if protocol is None or not protocol.delivery_requirement_ids:
        return False
    final = set(protocol.delivery_requirement_ids)
    assignment = message.payload.get('research_assignment') or {}
    ids = set(assignment.get('requirement_ids', []))
    ids.update('delivery:' + c['id'] for c in message.payload.get('delivery_contracts', []))
    if ids:
        return bool(ids & final) and not (ids - final)
    # Direct entry requests have no requirement assignment yet. Do not gate a
    # producer solely because it also offers a delivery capability.
    return False


def current_review_record(state, *, check_files=True):
    from bioma.core.research import completion_assessment
    review = completion_assessment(state, check_files=check_files)
    if not review or not review.complete:
        return None
    from bioma.core.research import qualified_review_assessment
    for evidence in reversed(state.evidence):
        if evidence.kind != 'research_assessment':
            continue
        if contract_for(state).schema_version == 'research-contract-v2':
            try:
                if qualified_review_assessment(state, evidence, check_files=check_files) == review:
                    return evidence
            except (ValueError, TypeError):
                continue
        elif evidence.source == 'comagent' and evidence.value.get('snapshot_hash') == review.snapshot_hash:
            return evidence
    return None


def delivery_basis_valid(state, evidence):
    """Only an authorized delivery's exact, previously accepted basis is excluded."""
    protocol = protocol_for(state)
    if protocol is None:
        return False
    value = evidence.value if isinstance(evidence.value, dict) else {}
    basis = value.get('delivery_basis') or {}
    if basis.get('protocol_id') != fingerprint(protocol.model_dump(mode='json')):
        return False
    request = next((r for r in state.task_requests if r.id == basis.get('task_request_id')), None)
    if request is None or not is_delivery(state, request):
        return False
    record = next((e for e in state.evidence if e.id == basis.get('review_evidence_id')
                   and e.kind == 'research_assessment'), None)
    if record is None:
        return False
    assessment = record.value.get('assessment', record.value)
    return bool(assessment.get('complete') and assessment.get('contract_id') == protocol.contract_id
                and record.id in evidence.parent_ids)


def completion_status(state, *, check_files=True):
    """Recompute from committed evidence; metrics or an agent's outcome are not authority."""
    from bioma.core.evidence_acceptance import structural_assessment
    from bioma.core.research import completion_assessment
    protocol = protocol_for(state)
    if protocol is None:
        return {'complete': False, 'research': 'not_applicable'}
    contract = contract_for(state)
    rows = [structural_assessment(state, r, check_files=check_files) for r in contract.requirements if r.mode != 'claim_assessment']
    mandatory = required_ids(contract)
    final = set(protocol.delivery_requirement_ids)
    research = 'not_required'
    review = None
    if protocol.mode == 'research':
        review = completion_assessment(state, check_files=check_files)
        research = 'satisfied' if review and review.complete else 'pending'
    statuses = {r.requirement_id:r.status for r in rows}
    if review:
        statuses.update({r.requirement_id:r.status for r in review.requirements})
    for _ in contract.requirements:
        for r in contract.requirements:
            if statuses.get(r.id) == 'satisfied' and any(statuses.get(d) != 'satisfied' for d in r.depends_on):
                statuses[r.id] = 'missing'
    for row in rows:
        if row.status == 'satisfied' and statuses[row.requirement_id] != 'satisfied':
            row.status, row.rationale = 'missing', 'A declared prerequisite remains unaccepted.'
    work = all(r.status == 'satisfied' for r in rows if r.requirement_id in mandatory - final)
    delivery = all(r.status == 'satisfied' for r in rows if r.requirement_id in mandatory & final)
    return {'protocol_id': fingerprint(protocol.model_dump(mode='json')), 'mode': protocol.mode,
            'work': 'satisfied' if work else 'pending', 'research': research,
            'delivery': 'satisfied' if delivery else 'pending',
            'complete': work and delivery and research != 'pending',
            'requirements': [r.model_dump(mode='json') for r in rows]}


def dependency_records(state, message):
    """Resolve only the prerequisite evidence granted to this assignment."""
    from bioma.core.evidence_acceptance import structural_assessment
    contract = contract_for(state)
    if protocol_for(state) is None:
        return True, []
    ids = set((message.payload.get('research_assignment') or {}).get('requirement_ids', []))
    ids.update('delivery:' + c['id'] for c in message.payload.get('delivery_contracts', []))
    rows = {r.id:r for r in contract.requirements}
    from bioma.core.research_planning import scheduling_dependencies
    dependencies = scheduling_dependencies(state, ids)
    pending = list(dependencies)
    while pending:
        for parent in rows[pending.pop()].depends_on:
            if parent not in dependencies:
                dependencies.add(parent)
                pending.append(parent)
    records = []
    for key in dependencies:
        requirement = rows[key]
        if requirement.mode == 'claim_assessment':
            from bioma.core.research import completion_assessment
            review = completion_assessment(state)
            row = next((r for r in review.requirements if r.requirement_id == key), None) if review else None
        else:
            row = structural_assessment(state, requirement)
        if row is None or row.status != 'satisfied':
            return False, []
        records.extend(row.evidence_ids)
    return True, sorted(set(records))


def bind_assignment_dependencies(state, request):
    """Materialize declared prerequisites before capability qualification.

    Both Runtime activation and Com repair use this adapter. Input files come
    only from granted evidence, never from an unscoped scan of the artifact store.
    """
    from pathlib import Path
    from bioma.core.handoffs import dependency_bindings
    ready, references = dependency_records(state, request)
    if not ready or not references:
        return request
    payload = dict(request.payload)
    parents = [e for e in state.evidence if e.id in references]
    payload = {**dependency_bindings(parents), **payload, 'dependency_evidence_ids': references}
    # A qualified prerequisite grants its exact producer record, not every
    # observation or artifact in the run. Protocol execution rechecks that bond.
    producer_refs = {e.value.get('work_result_ref') for e in parents if isinstance(e.value, dict)}
    producer_refs &= {o.id for o in state.observations if o.kind == 'work_result'}
    payload['input_refs'] = list(dict.fromkeys([*payload.get('input_refs', []), *references, *sorted(producer_refs)]))
    if request.required_capability in {'scientific.data_analysis', 'scientific.compute'} and request.message_type == 'task.submitted':
        artifacts = [a for a in state.artifacts if set(references).intersection(a.evidence_ids)
                     and '://' not in a.uri and Path(a.uri).suffix.lower() in {'.json', '.csv', '.tsv', '.txt', '.md'}]
        inputs = dict(payload.get('inputs') or {})
        inputs['files'] = list(dict.fromkeys([*inputs.get('files', []), *(a.uri for a in artifacts)]))
        payload.update(inputs=inputs, input_artifact_ids=[a.id for a in artifacts])
    return request.model_copy(update={'payload': payload})


def attach_assignment(state, request):
    """Bind producer requests without granting them unrelated input obligations."""
    contract = contract_for(state)
    if contract is None or request.payload.get('research_assignment'):
        return request
    inputs = request.payload.get('inputs') or {}
    from bioma.core.research_planning import execution_requirements_for
    strategies = execution_requirements_for(state)
    rows = [r for r in strategies if r.capability and
            r.capability == request.required_capability
            and r.message_type == request.message_type
            and (not r.method_id or not request.payload.get('method_id') or r.method_id == request.payload.get('method_id'))
            and (not r.input_bindings or r.input_bindings == inputs or r.source == 'delivery_contract')]
    deliveries = {c['id'] for c in request.payload.get('delivery_contracts', [])}
    if deliveries:
        rows = [r for r in rows if r.mode != 'delivery' or r.delivery_id in deliveries]
    # One work result cannot establish both an upstream obligation and a
    # downstream obligation that must consume it. Publish only the independent
    # frontier; Runtime activates the dependent work after its evidence commits.
    by_id = {r.id: r for r in contract.requirements}
    by_id.update({r.id: r for r in strategies})
    work_ids = {r.id for r in rows if r.stage == 'work'}
    def has_grouped_ancestor(requirement):
        seen = set()
        pending = list(requirement.depends_on)
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            if key in work_ids:
                return True
            seen.add(key)
            pending.extend(by_id[key].depends_on)
        return False
    rows = [r for r in rows if r.stage != 'work' or not has_grouped_ancestor(r)]
    methods = {r.method_id for r in rows if r.method_id}
    if not request.payload.get('method_id') and len(methods) > 1:
        # Distinct methods are distinct work; do not attach them all to one offer.
        rows = [r for r in rows if not r.method_id]
    from bioma.core.community import merge_execution_requirements
    requirements = merge_execution_requirements(request.payload.get('execution_requirements') or {},
                                                 *(r.execution_requirements for r in rows))
    method = request.payload.get('method_id') or (next(iter(methods)) if len(methods) == 1 else None)
    if method:
        requirements = merge_execution_requirements(requirements, {'method_id': method})
    return request.model_copy(update={'payload': {**request.payload, 'method_id': method,
        'execution_requirements': requirements, 'research_assignment': {
        'contract_id': contract.identity, 'version': contract.version,
        'requirement_ids': [r.id for r in rows], 'acceptance': [r.model_dump(mode='json') for r in rows]}}})


def outcome_summary(state):
    """Read-only product view, not a new completion/admission policy.

    A reviewed work package can be available while the scientific contract is
    unresolved. This view never calls a model or promotes notes into Evidence.
    Existing acceptance remains the only formal contract assessment here.
    """
    import hashlib
    from pathlib import Path

    acceptance = completion_status(state)
    contract = contract_for(state)
    packages = []
    for observation in state.observations:
        if observation.kind != 'work_result':
            continue
        payload = observation.value.get('payload') or {}
        review = payload.get('delivery_review') or {}
        artifacts = observation.value.get('artifacts') or []
        declared = {a['id']: a for a in artifacts if isinstance(a, dict) and a.get('id')}
        current = {a.id: a for a in state.artifacts if a.id in declared}
        reviewed = bool(review.get('review_version') and review.get('sha256') and
                        review.get('issues') == [] and declared)
        invalid = []
        for artifact_id, raw in declared.items():
            actual = current.get(artifact_id)
            expected = (raw.get('metadata') or {}).get('sha256')
            try:
                valid = (actual is not None and actual.uri == raw.get('uri') and expected
                         and actual.metadata.get('sha256') == expected
                         and review.get('sha256', {}).get(actual.uri) == expected
                         and hashlib.sha256(Path(actual.uri).read_bytes()).hexdigest() == expected)
            except OSError:
                valid = False
            if not valid:
                invalid.append(artifact_id)
        # Review also binds inputs, program and execution receipts, not just outputs.
        if reviewed:
            for filename, digest in review['sha256'].items():
                try:
                    if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest:
                        reviewed = False
                        break
                except OSError:
                    reviewed = False
                    break
        packages.append({'work_result_ref': observation.id,
            'artifact_refs': sorted(declared), 'invalid_artifact_refs': sorted(invalid),
            'delivery': ('CURRENT_RECORDED_REVIEW' if reviewed and not invalid else
                         'STALE_OR_MISSING' if review else 'NOT_REVIEWED'),
            'scope': 'recorded package consistency review; not scientific support or original task success'})
    unresolved = [r['requirement_id'] for r in acceptance.get('requirements', [])
                  if r['status'] != 'satisfied']
    if contract:
        # Claim requirements may not appear in structural acceptance rows.
        from bioma.core.research import completion_assessment
        assessment = completion_assessment(state)
        if assessment:
            unresolved = sorted(set(unresolved) | {r.requirement_id for r in assessment.requirements
                                                  if r.status != 'satisfied'})
        else:
            unresolved = sorted(set(unresolved) | {r.id for r in contract.requirements
                                                  if r.mode == 'claim_assessment' and r.mandatory})
    protocol = protocol_for(state)
    formal_delivery = (acceptance.get('delivery', 'not_applicable')
                       if protocol and protocol.delivery_requirement_ids else
                       'not_required' if protocol else 'not_applicable')
    from bioma.evidence.wp2_reconciliation import wp2_claim_projection
    scoped_claims=[wp2_claim_projection(state,e.id,e.claim) for e in state.evidence if e.kind=='scientific_claim']
    return {
        'execution': {'task_status': state.status, 'work_result_refs': [p['work_result_ref'] for p in packages],
                      'reason': dict(state.status_reason)},
        'scientific_support': {'status': ('SATISFIED' if acceptance.get('research') == 'satisfied'
                                         else 'UNRESOLVED' if acceptance.get('research') == 'pending'
                                         else 'NOT_ASSESSED'),
                               'status_scope':'formal_research_contract',
                               'scoped_claim_assessments':scoped_claims,
                               'unresolved_requirement_refs': unresolved,
                               'committed_evidence_refs': [e.id for e in state.evidence]},
        'delivery': {'work_packages': packages, 'contract_complete': acceptance['complete'],
                     'contract_present': contract is not None,
                     'contract_complete_reason': ('No formal scientific contract is installed; not a failed or passed assessment.'
                         if contract is None else 'Evaluated against installed contract requirements.'),
                     'formal_status': formal_delivery},
    }
