"""Neutral controller interface over existing runtime qualification and routing."""
from bioma.capabilities.registry import CapabilityRegistry
from bioma.capabilities.router import CapabilityRouter
from bioma.core.capability_binding import QualifiedCapabilityBinding
from bioma.core.community import OfferProposal
from bioma.core.models import TaskRequest

from bioma.core.scientific_capabilities import FAMILIES, CapabilityRequirement


class BackendRegistry(CapabilityRegistry):
    def __init__(self, providers):
        self.providers = tuple(providers)
        if any(c.family not in FAMILIES for p in self.providers for c in p.capabilities):
            raise ValueError('Only scientific capability providers belong in this registry')
        super().__init__(c for p in self.providers for c in p.capabilities)
        self.router = CapabilityRouter(self, {peer: p.assess for p in self.providers for peer in p.peer_ids})

    @property
    def active_provider_ids(self):
        return {peer for p in self.providers for peer in p.peer_ids}

    def families(self) -> tuple[str, ...]:
        """Enumerate registered, enabled families; availability is request-specific."""
        return tuple(sorted({c.family for c in self.all() if c.enabled}))

    def _request(self, requirement, requester, payload, available_resources):
        requirement = CapabilityRequirement.model_validate(requirement)
        payload = dict(payload or {})
        reserved = {'method_id', 'execution_requirements', 'result_requirements',
                    'recipient', 'provider_id', 'provider_binding', 'accepted_offer',
                    'selected_capability', 'task_request_id', 'research_assignment',
                    'delivery_contracts'}
        if reserved.intersection(payload):
            raise ValueError('Express routing and result restrictions in CapabilityRequirement')
        if not set(requirement.required_resources) <= set(available_resources or ()):
            raise ValueError('Required resources are absent from the runtime inventory')
        required = requirement.effective_execution_requirements()
        payload.update(objective=requirement.objective, input_refs=requirement.input_refs,
                       execution_requirements=required.model_dump(mode='json'),
                       result_requirements=requirement.result_requirements,
                       required_resources=requirement.required_resources)
        if required.method_id:
            payload['method_id'] = required.method_id
        return TaskRequest(requester=requester, required_capability=requirement.capability_family,
            objective=requirement.objective,
            message_type=requirement.constraints.get('message_type', 'task.submitted'),
            payload=payload, budget=requirement.budget, deadline=requirement.deadline,
            risk_level=requirement.risk_level)

    def resolve(self, requirement: CapabilityRequirement, *, requester='scientific_controller',
                payload=None, available_resources=None):
        """Existing deterministic single-provider routing, without scientific ranking."""
        request = self._request(requirement, requester, payload, available_resources)
        return self.router.route(request, active_agents=self.active_provider_ids, ttl=8,
                                 available_resources=available_resources)

    def qualified_bindings(self, requirement: CapabilityRequirement, *,
                           requester='scientific_controller', payload=None,
                           available_resources=None) -> tuple[QualifiedCapabilityBinding, ...]:
        """One accepted method per eligible provider, in existing router order.

        The router's proposals are in-memory only: no execution, ledger write,
        scientific selection, admission or truth commitment occurs here. Use a
        method restriction to enumerate alternatives to a provider's default.
        """
        request = self._request(requirement, requester, payload, available_resources)
        return self._bindings(request, available_resources)

    def _bindings(self, request, available_resources):
        decision = self.router.route(request.model_copy(update={'selection_mode': 'all'}),
            active_agents=self.active_provider_ids, ttl=8, available_resources=available_resources)
        bindings = []
        for message in decision.messages:
            offer = message.payload['accepted_offer']
            capability = next(c for c in self.all() if c.agent_id == message.recipient
                              and c.id == offer['capability_id'])
            method = next(m for m in capability.methods if m.id == offer['method_id'])
            bindings.append(QualifiedCapabilityBinding.from_qualification(
                capability, method, request, OfferProposal.model_validate(offer), decision.qualification))
        return tuple(bindings)

    def ground(self, binding: QualifiedCapabilityBinding, *, available_resources=None):
        """Requalify and return an exact runtime RouteDecision, still subject to WP1.

        This is not a ScientificOption, a controller decision or a permit.
        No fallback to another implementation is allowed for a stale binding.
        """
        binding = QualifiedCapabilityBinding.model_validate(binding)
        if binding.content_reference() != binding.qualification_ref:
            raise ValueError('GROUNDING_STALE: qualification snapshot changed')
        if not set(binding.request.payload.get('required_resources', ())) <= set(available_resources or ()):
            raise ValueError('GROUNDING_STALE: required resources unavailable')
        current = self._bindings(binding.request, available_resources)
        if not any(candidate.qualification_ref == binding.qualification_ref for candidate in current):
            raise ValueError('GROUNDING_STALE: provider/method contract is no longer qualified')
        request = binding.request.model_copy(update={'payload': {
            **binding.request.payload, 'method_id': binding.method_id}})
        decision = self.router.route(request,
            active_agents=self.active_provider_ids, ttl=8, available_resources=available_resources,
            capability_binding=binding.capability_family, provider_binding=binding.provider_id,
            method_binding=binding.method_id, implementation_binding=binding.implementation_identity)
        if len(decision.messages) != 1:
            raise ValueError('GROUNDING_STALE: exact provider binding unavailable')
        offer = decision.messages[0].payload['accepted_offer']
        if (offer['method_id'] != binding.method_id or offer['profile_id'] != binding.profile_id
                or offer['result_contract'] != binding.result_contract.model_dump(mode='json')):
            raise ValueError('GROUNDING_STALE: accepted method/profile/result differs from binding')
        return decision
