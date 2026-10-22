"""Deterministic activation from delivery facts, without role-specific next steps."""
from bioma.core.deliverables import delivery_status


def unmet_dependencies(contracts, verified_ids):
    own = {c['id'] for c in contracts}
    return {d for c in contracts for d in c.get('depends_on', [])} - own - set(verified_ids)


class RequirementRules:
    def __init__(self, ledger=None):
        self.ledger = ledger

    def ready(self, request_message, state, all_contracts):
        from bioma.core.acceptance import protocol_for, is_delivery, current_review_record
        protocol = protocol_for(state)
        if protocol:
            from bioma.core.acceptance import dependency_records
            if not dependency_records(state, request_message)[0]:
                return False
        if protocol and protocol.mode == 'research' and is_delivery(state, request_message) and current_review_record(state) is None:
            return False
        contracts = request_message.payload.get('delivery_contracts', [])
        if not contracts:
            return True
        verified = {c['id'] for c in delivery_status(all_contracts, state) if c['status'] == 'verified'}
        missing = unmet_dependencies(contracts, verified)
        if not missing and self.ledger:
            key = 'activate:' + request_message.id
            if not any(e.idempotency_key == key for e in self.ledger.events(state.task_id)):
                self.ledger.append(state.task_id, 'RequirementActivated', 'harness',
                    {'message_id': request_message.id, 'satisfied_dependencies': sorted(
                        {d for c in contracts for d in c.get('depends_on', [])} & verified)},
                    idempotency_key=key)
        return not missing


    def propose(self, source_message, state, contracts, *, requester):
        """Create recipient-free requests for uncovered facts, using contracts only."""
        from bioma.core.models import TaskRequest
        from uuid import NAMESPACE_URL, uuid5
        covered = {c['id'] for request in state.task_requests
                   for c in request.payload.get('delivery_contracts', [])}
        covered.update(c['id'] for c in source_message.payload.get('delivery_contracts', []))
        groups = {}
        for row in delivery_status(contracts, state):
            if row['status'] == 'verified' or row['id'] in covered:
                continue
            contract = next(c for c in contracts if c['id'] == row['id'])
            groups.setdefault((contract['capability'], contract.get('method_id') or '', contract['message_type'], contract['input_hash']), []).append(contract)
        requests = []
        for (capability, method_id, message_type, input_hash), rows in sorted(groups.items()):
            identity = ':'.join([state.task_id, capability, method_id, message_type, input_hash])
            requests.append(TaskRequest(id=str(uuid5(NAMESPACE_URL, identity)),
                requester=requester, required_capability=capability, objective=state.objective,
                message_type=message_type,
                payload={'inputs': rows[0].get('input_bindings', {}), 'delivery_contracts': rows,
                         'method_id': method_id or None},
                correlation_id=source_message.correlation_id or source_message.id,
                causation_id=source_message.id))
        return requests
