"""Runtime activation and closure; agents submit local work, not global status."""
from uuid import NAMESPACE_URL, uuid5

from bioma.core.acceptance import (protocol_for, is_delivery, current_review_record,
    completion_status, attach_assignment, required_ids)
from bioma.core.research import contract_for, fingerprint, review_request, completion_assessment


def prospective(state, delta):
    return state.model_copy(update={'evidence': [*state.evidence, *delta.evidence],
        'artifacts': [*state.artifacts, *delta.artifacts], 'constraints': [*state.constraints, *delta.constraints],
        'task_requests': [*state.task_requests, *delta.task_requests]})


def bind_delivery_message(state, message):
    policy = protocol_for(state)
    if policy:
        from bioma.core.acceptance import dependency_records
        ready, references = dependency_records(state, message)
        if ready and references:
            message = message.model_copy(update={'payload': {**message.payload, 'dependency_evidence_ids': references}})
    if policy and policy.mode == 'research' and is_delivery(state, message):
        review = current_review_record(state)
        if review:
            return message.model_copy(update={'payload': {**message.payload, 'accepted_review_evidence_id': review.id}})
    return message


def _basis(state, message, *, check_files=True):
    policy = protocol_for(state)
    if not policy or policy.mode != 'research' or not is_delivery(state, message):
        return None
    assessment = completion_assessment(state, check_files=check_files)
    review = current_review_record(state, check_files=check_files)
    if (not assessment or not assessment.complete or not review
            or review.id != message.payload.get('accepted_review_evidence_id')):
        raise ValueError('Final delivery requires the current accepted research basis')
    return {'protocol_id': fingerprint(policy.model_dump(mode='json')),
            'review_evidence_id': review.id, 'task_request_id': message.task_request_id}


def validate_delivery_delta(state, delta, message, *, check_files=True):
    policy = protocol_for(state)
    if policy is None:
        return
    basis = _basis(state, message, check_files=check_files)
    if basis:
        declared = {k for r in contract_for(state).requirements if r.id in policy.delivery_requirement_ids for k in r.evidence_kinds}
        derived = {'artifact_analysis', 'scientific_visualization', 'evidence_graph_synthesis', 'delivery'}
        if delta.constraints or set(delta.updates) - {'metrics', 'agent_status'}:
            raise ValueError('Delivery cannot change accepted scientific state')
        if any(e.kind not in declared | derived | {'execution_failure'} for e in delta.evidence):
            raise ValueError('New scientific work must be submitted before research acceptance, not hidden in delivery')
    for e in delta.evidence:
        value = e.value if isinstance(e.value, dict) else {}
        if value.get('delivery_basis'):
            if not basis or value['delivery_basis'] != basis or basis['review_evidence_id'] not in e.parent_ids:
                raise ValueError('Invalid or unauthorized delivery basis')
    for a in delta.artifacts:
        if a.metadata.get('delivery_basis'):
            if not basis or a.metadata['delivery_basis'] != basis or not set(a.evidence_ids).intersection(e.id for e in delta.evidence):
                raise ValueError('Invalid delivery artifact basis')


def stage_output(state, delta, message):
    """Seal a delivery against its accepted basis; prohibit owner-supplied bypass flags."""
    if not protocol_for(state):
        return delta
    if any(isinstance(e.value, dict) and 'delivery_basis' in e.value for e in delta.evidence) or any(
            'delivery_basis' in a.metadata for a in delta.artifacts):
        raise ValueError('Delivery basis is runtime-owned metadata')
    basis = _basis(state, message)
    if basis and delta.outcome not in {'blocked', 'failed'}:
        for e in delta.evidence:
            e.value = {**(e.value if isinstance(e.value, dict) else {'value': e.value}), 'delivery_basis': basis}
            if basis['review_evidence_id'] not in e.parent_ids:
                e.parent_ids.append(basis['review_evidence_id'])
        for a in delta.artifacts:
            a.metadata['delivery_basis'] = basis
        validate_delivery_delta(state, delta, message)
    return delta


def activate(state, delta, message, queue):
    policy = protocol_for(state)
    if policy is None or delta.outcome in {'failed', 'blocked'}:
        return delta
    delta.task_requests = [attach_assignment(state, r) for r in delta.task_requests]
    view = prospective(state, delta)
    if policy.mode in {'work', 'research'}:
        from bioma.core.models import TaskRequest
        from bioma.core.evidence_acceptance import structural_assessment
        covered = {i for r in view.task_requests for i in (r.payload.get('research_assignment') or {}).get('requirement_ids', [])}
        covered.update((message.payload.get('research_assignment') or {}).get('requirement_ids', []))
        from bioma.core.research_planning import execution_requirements_for
        for requirement in execution_requirements_for(state):
            if (policy.mode == 'research' and requirement.id in policy.delivery_requirement_ids
                    and current_review_record(view) is None):
                continue
            if requirement.id in covered or requirement.id not in required_ids(contract_for(state)) or not requirement.capability:
                continue
            by_id = {r.id:r for r in contract_for(state).requirements}
            if any(structural_assessment(view, by_id[parent]).status != 'satisfied'
                   for parent in requirement.depends_on):
                continue
            if structural_assessment(view, by_id[requirement.id]).status == 'satisfied':
                continue
            from bioma.core.handoffs import dependency_bindings
            parent_ids = {e for parent in requirement.depends_on
                          for e in structural_assessment(view, by_id[parent]).evidence_ids}
            parents = [e for e in view.evidence if e.id in parent_ids or e.kind == 'query_interpretation']
            request = TaskRequest(id=str(uuid5(NAMESPACE_URL, state.task_id + ':work:' + requirement.id)),
                requester=delta.agent_id, required_capability=requirement.capability,
                objective=requirement.description, message_type=requirement.message_type,
                payload={'assignment_objective': requirement.description,
                    **dependency_bindings(parents),
                    'dependency_evidence_ids': sorted(parent_ids),
                    'input_refs': sorted(parent_ids),
                    'method_id': requirement.method_id,
                    'inputs': requirement.input_bindings or state.inputs,
                    'execution_requirements': requirement.execution_requirements,
                    'research_assignment': {'contract_id': contract_for(state).identity, 'version': contract_for(state).version,
                        'requirement_ids': [requirement.id], 'acceptance': [requirement.model_dump(mode='json')]}})
            if requirement.delivery_id:
                from bioma.core.deliverables import task_contracts
                request.payload['delivery_contracts'] = [c for c in task_contracts(state) if c['id'] == requirement.delivery_id]
            if request.id not in {r.id for r in view.task_requests}:
                from bioma.core.acceptance import bind_assignment_dependencies
                delta.task_requests.append(bind_assignment_dependencies(view, request))
        if policy.mode == 'work':
            return delta
    # A deferred publisher does not create another scientific review merely by
    # formatting already accepted evidence. Changed science still invalidates it.
    if (message.message_type != 'research.review.requested' and not message.payload.get('review_scope')
            and current_review_record(view) is None
            and not any(m.message_type == 'research.review.requested' or m.payload.get('review_scope')
                        for m in [*queue, *delta.task_requests])
            and (any(e.kind != 'query_interpretation' for e in delta.evidence) or delta.outcome == 'completed'
                 or (delta.task_requests and all(is_delivery(view, r) for r in delta.task_requests)))):
        delta.task_requests.append(review_request(view, message, requester=delta.agent_id))
        delta.outcome = 'continue'
    return delta


def finalize(state, delta, message, queue, local_outcome):
    if not protocol_for(state):
        return delta
    view = prospective(state, delta)
    status = completion_status(view)
    assignments = dict((state.metrics.get('acceptance') or {}).get('assignments', {}))
    assignments[message.task_request_id or message.id] = {
        'state_version': state.version + 1,
        'owner': delta.agent_id, 'reported_outcome': local_outcome,
        'stage': 'delivery' if is_delivery(state, message) else 'research_review' if message.message_type == 'research.review.requested' else 'work',
        'evidence_ids': [e.id for e in delta.evidence], 'artifact_ids': [a.id for a in delta.artifacts]}
    status['assignments'] = assignments
    delta.updates = {**delta.updates, 'metrics': {**delta.updates.get('metrics', {}), 'acceptance': status}}
    if delta.outcome not in {'blocked', 'failed'}:
        if queue or delta.messages:
            delta.outcome = 'continue'
        elif status['complete']:
            delta.outcome, delta.status_reason = 'completed', {}
        else:
            delta.outcome = 'blocked'
            delta.status_reason = {'code': 'acceptance_incomplete', 'acceptance': status}
    return delta
