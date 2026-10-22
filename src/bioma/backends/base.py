"""Scientific resource ownership and Method contracts on the governed runtime."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from bioma.capabilities.registry import advertise_capabilities
from bioma.core.methods import assess_capability
from bioma.core.transitions import propose_transition
from bioma.core.methods import ExecutionMethod
from bioma.core.models import Artifact, CapabilitySpec, Observation, StateDelta
from bioma.tools.registry import ToolRegistry

from bioma.core.scientific_capabilities import WorkResult


def upstream_work_result_identities(state, refs):
    """Resolve immutable upstream WorkResult observations used by a backend."""
    from bioma.core.research import fingerprint
    rows = []
    for ref in dict.fromkeys(refs):
        observation = next((item for item in state.observations if item.id == ref), None)
        if observation is None:
            continue
        result = WorkResult.model_validate(observation.value)
        rows.append({'observation_id': observation.id, 'source': observation.source,
            'family': result.family, 'kind': result.kind,
            'work_result_identity': fingerprint(result.model_dump(mode='json'))})
    return rows


@dataclass(frozen=True)
class MethodBinding:
    method: ExecutionMethod
    execute: Callable
    tools: tuple = ()


@contextmanager
def method_tool_scope(tools):
    # Compose with WP1 admission; never replace its permit/resource checks.
    from bioma.runtime.admission import ExecutionAuthorizationError, _execution_check
    parent = _execution_check.get()
    allowed = {tool.name: tool for tool in tools}
    def guard(tool=None):
        if parent:
            parent(tool)
        if tool is not None and allowed.get(tool.name) is not tool:
            from bioma.capabilities.scoped import is_scoped_metadata_tool
            if not is_scoped_metadata_tool(tool):
                raise ExecutionAuthorizationError('Tool is outside this method view: ' + tool.name)
    token = _execution_check.set(guard)
    try:
        yield
    finally:
        _execution_check.reset(token)


class BackendIncomplete(ValueError):
    def __init__(self, outcome, reason):
        super().__init__(str(reason))
        self.outcome, self.reason = outcome, reason


class ScientificBackend:
    """One family, independently offered methods, no global planning authority."""
    advertise = advertise_capabilities
    default_proposal = propose_transition
    propose = propose_transition
    provider_kind = 'scientific_capability'
    family: str
    provider_id: str

    def __init__(self, family, provider_id, bindings, *, scoped_skill_resolver=None):
        from bioma.core.scientific_capabilities import FAMILIES
        if family not in FAMILIES:
            raise ValueError("Unknown scientific resource family: " + str(family))
        self.family = family
        self.provider_id = provider_id
        if not self.provider_id:
            raise ValueError("Scientific resource identity must be non-empty")
        self.peer_ids = (self.provider_id,)
        self.capabilities = []
        backends = tuple(bindings)
        self.bindings = {item.method.id: item for item in backends}
        self.scoped_skill_resolver = scoped_skill_resolver
        if len(self.bindings) != len(backends):
            raise ValueError('Duplicate method backend')
        self.tool_registry = ToolRegistry([])
        for backend in backends:
            if backend.method.result.responsibility != self.family:
                raise ValueError('Method belongs to another family')
            for tool in backend.tools:
                if self.family == 'scientific.evidence_assessment':
                    raise ValueError('Evidence assessment methods have no executable tools')
                retrieval = self.family == 'scientific.research' and tool.replay_safe and tool.result_kind == 'retrieved_fact'
                paid_compute = (self.family == 'scientific.compute'
                    and tool.category == 'paid-remote-compute'
                    and tool.external_effect and not tool.replay_safe
                    and tool.result_kind == 'remote_compute_job'
                    and tool.requires_network
                    and {'provider:neurosnap', 'credits:neurosnap'} <= set(tool.resources)
                    and 'neurosnap-exact-service' in tool.tags
                    and sum(tag.startswith('service:') for tag in tool.tags) == 1
                    and tool.result_validator is not None
                    and tool.reconcile_effect is not None)
                if self.family != 'scientific.execution' and tool.external_effect and not (retrieval or paid_compute):
                    raise ValueError('Only execution methods can expose external effects')
                if self.family == 'scientific.study_design' and tool.result_kind not in {'source', 'source_set', 'retrieved_fact'}:
                    raise ValueError('Study design accepts reference tools only')
                self.tool_registry.add(tool)
        self.advertise(CapabilitySpec(id=self.family, family=self.family,
            agent_id=self.provider_id, description=self.family,
            methods=[item.method for item in backends],
            accepts_message_types=list(dict.fromkeys(item.method.message_type for item in backends)),
            runtime_profile={'provider_kind': self.provider_kind}, priority=80,
            max_risk_level=('medium' if any(
                tool.category == 'paid-remote-compute'
                for backend in backends for tool in backend.tools) else 'low')))

    def tool_view(self, method_id):
        return self.bindings[method_id].tools

    def assess(self, request, capability):
        proposal = assess_capability(self, request, capability)
        if proposal.accepted:
            if proposal.method_id not in self.bindings:
                return proposal.model_copy(update={'accepted': False,
                    'reason': 'No concrete MethodBinding is bound to this request'})
            required = set(request.payload.get('execution_requirements', {}).get('required_tools', []))
            available = {tool.name for tool in self.tool_view(proposal.method_id)}
            if not required <= available:
                return proposal.model_copy(update={'accepted': False, 'reason': 'Required tools are outside the selected method view'})
        return proposal

    def handle(self, state, message):
        method_id = message.payload.get('method_id')
        backend = self.bindings.get(method_id)
        if backend is None:
            raise ValueError('An explicitly registered method is required')
        from bioma.core.community import requirements_from
        reasons = backend.method.rejection(message.message_type, message.payload, requirements_from(message.payload))
        if reasons:
            raise ValueError('; '.join(reasons))
        try:
            from contextlib import nullcontext
            grant_context = nullcontext()
            grants = ()
            capability_snapshot_ref = None
            if self.scoped_skill_resolver is not None and backend.tools:
                from bioma.capabilities.scoped import scoped_tool_grant_scope
                from bioma.runtime.admission import current_execution_permit
                permit = current_execution_permit()
                control = state.gateway.get('scoped_capability_context') or {}
                grants = self.scoped_skill_resolver.issue_grants(
                    request_id=message.task_request_id or message.id,
                    parent_scientific_action_ref=(control.get('scientific_action_ref') or
                                                  ('proposal:' + permit.proposal_id)),
                    contract_ref=control.get('contract_ref') or ('task-contract:' + state.task_id),
                    contract_version=int(control.get('contract_version') or 1),
                    plan_ref=control.get('plan_ref') or ('task-plan:' + state.task_id),
                    plan_version=int(control.get('plan_version') or 1),
                    provider_id=self.provider_id, capability_family=self.family,
                    method=backend.method, method_tools=backend.tools, permit=permit,
                    budget=float(message.payload.get('budget')
                        or (message.payload.get('inputs') or {}).get('max_credits')
                        or 0))
                snapshot = self.scoped_skill_resolver.snapshot(provider_id=self.provider_id,
                    capability_family=self.family, method=backend.method, tools=backend.tools)
                capability_snapshot_ref = snapshot.identity
                grant_context = scoped_tool_grant_scope(grants, snapshot)
            with method_tool_scope(backend.tools), grant_context:
                result = backend.execute(state.model_copy(deep=True), message.model_copy(deep=True), backend.tools)
        except BackendIncomplete as exc:
            deferred = exc.outcome in {'pending', 'suspended', 'deferred'}
            reason = ({**exc.reason, 'disposition': exc.outcome}
                      if deferred and isinstance(exc.reason, dict) else exc.reason)
            return StateDelta(agent_id=self.provider_id, source_message_id=message.id,
                expected_version=state.version, outcome='blocked' if deferred else exc.outcome,
                status_reason=reason)
        if (not isinstance(result, WorkResult) or result.family != self.family
                or result.kind not in backend.method.result.evidence_kinds):
            raise ValueError('Backend must return an owned, non-authoritative WorkResult')
        if result.kind == 'ResearchBundle':
            from bioma.core.scientific_capabilities import ResearchBundle
            ResearchBundle.model_validate(result.payload)
        elif result.kind == 'EvidenceAssessment':
            from bioma.core.scientific_capabilities import EvidenceAssessment
            EvidenceAssessment.model_validate(result.payload)
        known_refs = ({row.id for row in state.observations} | {row.id for row in state.evidence}
                      | {row.id for row in state.artifacts})
        for raw in (state.inputs.get('source_inputs', []) if isinstance(state.inputs, dict) else []):
            if isinstance(raw, dict) and raw.get('id'):
                known_refs.add(raw['id'])
        message_inputs = message.payload.get('inputs') or {}
        for raw in message_inputs.get('source_inputs', []) if isinstance(message_inputs, dict) else []:
            if isinstance(raw, dict) and raw.get('id'):
                known_refs.add(raw['id'])
        if not set(result.input_refs) <= known_refs:
            raise ValueError('WorkResult references input identities outside the admitted state or message')
        result = result.model_copy(update={'provenance': {**result.provenance,
            'provider_id': self.provider_id, 'capability_family': self.family,
            'method_id': backend.method.id,
            'result_contract': backend.method.result.model_dump(mode='json'),
            'capability_snapshot_ref': capability_snapshot_ref,
            'tool_grant_refs': [grant.identity for grant in grants]}})
        observation = Observation(kind='work_result', source=self.provider_id,
            method=method_id, value=result.model_dump(mode='json'))
        from bioma.core.scientific_capabilities import CapabilityRequirement
        continuations = []
        for raw in message.payload.get('next_requirements', []):
            requirement = CapabilityRequirement.model_validate(raw)
            continuations.append(requirement.model_copy(update={
                'input_refs': list(dict.fromkeys([
                    *requirement.input_refs, *result.input_refs, observation.id]))}))
        if continuations:
            result = result.model_copy(update={'requirements': continuations})
            observation.value = result.model_dump(mode='json')
        artifacts = []
        import hashlib
        from pathlib import Path
        for raw in result.artifacts:
            artifact = Artifact.model_validate(raw)
            digest = hashlib.sha256(Path(artifact.uri).read_bytes()).hexdigest()
            if artifact.metadata.get('sha256') not in {None, digest}:
                raise ValueError('Backend artifact changed before result verification')
            artifacts.append(artifact.model_copy(update={'source': self.provider_id, 'evidence_ids': [],
                'metadata': {**artifact.metadata, 'sha256': digest, 'work_result_ref': observation.id,
                             'candidate_result_refs': artifact.evidence_ids}}))
        observation.artifact_ids = [item.id for item in artifacts]
        return StateDelta(agent_id=self.provider_id, source_message_id=message.id,
            expected_version=state.version,
            artifacts=artifacts, updates={'observations': [observation.model_dump(mode='json')]}, outcome='completed')
