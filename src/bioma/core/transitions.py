"""Execution proposals and permits precede actions; results never authorize themselves."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field

from bioma.core.community import Contract
from bioma.core.action_contracts import ScientificActionContract
from bioma.core.research import fingerprint


class TransitionProposal(Contract):
    schema_version: Literal['transition-proposal-v1'] = 'transition-proposal-v1'
    task_id: str
    message_id: str
    proposer: str
    base_version: int = Field(ge=0)
    observation_hash: str
    capability_id: str | None = None
    capability_version: str
    method_id: str | None = None
    action: str
    input_hash: str
    read_set: dict[str, str]
    write_set: list[str]
    supporting_evidence: list[str]
    contract: ScientificActionContract
    result_contract: dict[str, Any] = Field(default_factory=dict)
    risk: Literal['low', 'medium', 'high'] = 'low'
    memory_refs: list[str] = Field(default_factory=list)
    # Layer A link: the commitment this proposal discharges, when the caller
    # was assigned through the collaboration protocol. Optional so that
    # unassigned local proposals remain valid.
    commitment_id: str | None = None
    # The state surface this transition intends to change (advisory; the
    # authoritative confinement is `write_set`).
    target: str | None = None
    rationale_ref: str | None = None

    @property
    def identity(self):
        return fingerprint(self.model_dump(mode='json'))

    @property
    def proposal_ref(self) -> str:
        return 'proposal:' + self.identity

    @property
    def evidence_obligations(self) -> list[str]:
        """Mandatory evidence this transition promised to produce."""
        return list(self.result_contract.get('verification_obligations', []))


class ExecutionPermit(Contract):
    """ScientificExecutionMandate: runtime-issued execution authority.

    A permit is created only by a successful admission and is bound to exactly
    one proposal. It carries the authority ceiling the executor may not exceed.
    """

    proposal_id: str
    provider_id: str
    implementation_hash: str
    contract_hash: str
    admitted_version: int
    expires_at: float
    decision: Literal['authorized'] = 'authorized'
    capability_id: str | None = None
    target: str | None = None
    allowed_effects: list[str] = Field(default_factory=list)
    resource_budget: float | None = Field(default=None, ge=0.0)
    risk_ceiling: Literal['low', 'medium', 'high'] = 'low'
    evidence_obligations: list[str] = Field(default_factory=list)
    admission_rule_refs: list[str] = Field(default_factory=list)
    issued_at: float | None = None
    revocable: bool = True
    # Attempt counter within one proposal. A proposal has exactly one mandate
    # identity (`mandate_id`), but may be admitted more than once: each
    # admission is a distinct attempt, so retries and failures stay traceable.
    attempt: int = Field(default=1, ge=1)

    @property
    def mandate_id(self) -> str:
        """The mandate is identified by the one proposal it authorizes."""
        return 'mandate:' + self.proposal_id

    @property
    def attempt_id(self) -> str:
        """The identity of one admission-and-execution attempt."""
        return 'attempt:' + self.proposal_id + ':' + str(self.attempt)

    def authorizes(self, proposal) -> bool:
        """A mandate authorizes exactly its originating proposal (I4)."""
        return self.proposal_id == proposal.identity

    def covers(self, effects) -> bool:
        """Effect confinement: actual effects must stay inside the mandate (I5)."""
        return set(effects) <= set(self.allowed_effects)

    def live(self, *, now: float) -> bool:
        return self.decision == 'authorized' and now < self.expires_at


class ExecutionResult(Contract):
    """ScientificTransitionReceipt: what actually happened during execution.

    A receipt records observed reality. It never mutates state by itself; only
    verification over a receipt can produce a commit.
    """

    proposal_id: str
    delta_hash: str
    work_outcome: Literal['continue', 'completed', 'blocked', 'failed']
    evidence_ids: list[str]
    artifact_ids: list[str]
    executor: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    actual_effects: list[str] = Field(default_factory=list)
    execution_status: Literal[
        'completed', 'failed', 'timed_out', 'uncertain'
    ] | None = None
    tool_receipts: list[dict[str, Any]] = Field(default_factory=list)
    device_receipts: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = None
    # The attempt this result belongs to, so a receipt is never attributed to
    # the wrong attempt of the same proposal.
    attempt: int = Field(default=1, ge=1)

    @property
    def receipt_id(self) -> str:
        """Stable receipt identity for replay/duplicate suppression (I8)."""
        return ('receipt:' + self.proposal_id + ':' + str(self.attempt) + ':'
                + self.delta_hash[:16])

    @property
    def mandate_id(self) -> str:
        return 'mandate:' + self.proposal_id

    @property
    def attempt_id(self) -> str:
        return 'attempt:' + self.proposal_id + ':' + str(self.attempt)


class CommitDecision(Contract):
    proposal_id: str
    from_version: int
    to_version: int
    receipt_id: str
    decision: Literal['committed'] = 'committed'


def _evidence_identity(evidence):
    """Transport-free scientific projection of committed evidence."""
    return {
        'id': evidence.id,
        'kind': evidence.kind,
        'claim': evidence.claim,
        'value': evidence.value,
        'source': evidence.source,
        'method': evidence.method,
        'confidence': evidence.confidence,
        'parent_ids': evidence.parent_ids,
        'artifact_ids': evidence.artifact_ids,
    }


def execution_identity(delta):
    """Scientific owner output identity independent of artifact transport paths."""
    return fingerprint({'evidence':[_evidence_identity(e) for e in delta.evidence],
        'artifacts':[{'id':a.id,'kind':a.kind,'source':a.source,'evidence_ids':a.evidence_ids,
                     'sha256':a.metadata.get('sha256')} for a in delta.artifacts],
        'updates':delta.model_dump(mode='json')['updates'], 'outcome':delta.outcome})


def read_set(state, message):
    values = {key:value for key,value in {
        'inputs': message.payload.get('inputs', state.inputs),
        'gateway': state.gateway, 'success_criteria': state.success_criteria}.items() if value}
    if state.protocol:
        values['protocol'] = state.protocol
    for key, value in state.metrics.items():
        if key not in {'acceptance', 'research_review'}:
            values['metric:' + key] = value
    for item in state.predictions:
        values['prediction:' + str(item.get('evidence_id', fingerprint(item)))] = item
    for kind in ('evidence', 'observations', 'constraints'):
        for item in getattr(state, kind):
            values[kind + ':' + item.id] = item.model_dump(mode='json')
    return {key: fingerprint(value) for key, value in values.items()}


def check_read_set(proposal, state, message):
    current = read_set(state, message)
    if any(current.get(key) != value for key, value in proposal.read_set.items()):
        raise ValueError('Transition read conflict: observed scientific inputs changed; reproposal required')


def verify_transition_proof(state, delta, message):
    """Verify the owner output separately from runtime coordination additions.

    The durable authority additionally checks that this permit/result actually
    occurred in its journal. Historical verification does not expire old permits.
    """
    if delta.transition is None:
        return
    from bioma.core.models import StateDelta
    from bioma.core.action_contracts import check_predicates, prospective_state
    proof = delta.transition
    proposal = TransitionProposal.model_validate(proof['proposal'])
    permit = ExecutionPermit.model_validate(proof['permit'])
    result = ExecutionResult.model_validate(proof['result'])
    owner = StateDelta.model_validate(proof['owner_delta'])
    raw_owner = StateDelta.model_validate(proof.get('raw_owner_delta', proof['owner_delta']))
    if not (proposal.identity == permit.proposal_id == result.proposal_id):
        raise ValueError('Transition proof identity mismatch')
    if (proposal.task_id != state.task_id or proposal.message_id != message.id or
        proposal.proposer != delta.agent_id or permit.provider_id != delta.agent_id or
        owner.agent_id != delta.agent_id or owner.source_message_id != message.id or
        owner.expected_version != proposal.base_version or permit.admitted_version != proposal.base_version or
        proposal.base_version > state.version or owner.status != 'proposed' or owner.transition is not None):
        raise ValueError('Transition proof assignment mismatch')
    if permit.contract_hash != fingerprint(proposal.contract.model_dump(mode='json')):
        raise ValueError('Transition contract hash mismatch')
    if result.delta_hash != fingerprint(owner.model_dump(mode='json')) or result.work_outcome != owner.outcome:
        raise ValueError('Execution result hash mismatch')
    if result.evidence_ids != [e.id for e in owner.evidence] or result.artifact_ids != [a.id for a in owner.artifacts]:
        raise ValueError('Execution result manifest mismatch')
    if raw_owner != owner:
        from bioma.runtime.boundaries import normalize_work_result
        if normalize_work_result(state, proposal, raw_owner) != owner:
            raise ValueError('Work result authority normalization mismatch')
        from bioma.core.methods import validate_method_result
        validate_method_result(message, raw_owner)
    if not set(owner.updates) <= set(proposal.write_set) <= set(proposal.contract.writable_fields):
        raise ValueError('Transition exceeds admitted write authority')
    # Delivery provenance is a deterministic runtime seal, not a scientific
    # edit by the producer. Recompute it instead of permitting arbitrary extras.
    from bioma.runtime.acceptance import stage_output
    sealed = stage_output(state, owner.model_copy(deep=True), message)
    for field in ('evidence', 'artifacts', 'constraints'):
        actual = {item.id:item.model_dump(mode='json') for item in getattr(delta, field)}
        originals = {item.id:item.model_dump(mode='json') for item in getattr(owner, field)}
        if any(actual.get(item.id) not in (item.model_dump(mode='json'), originals[item.id]) for item in getattr(sealed, field)):
            raise ValueError('Owner scientific result changed after execution: ' + field)
    def contains(actual, original):
        if isinstance(original, dict):
            return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k,v in original.items())
        return actual == original
    for key, value in owner.model_dump(mode='json')['updates'].items():
        actual = delta.model_dump(mode='json')['updates'].get(key)
        if key in state.MERGE_FIELDS and isinstance(value, dict):
            if not contains(actual, value):
                raise ValueError('Owner state effects changed: ' + key)
        elif actual != value:
            raise ValueError('Owner state effects changed: ' + key)
    check_read_set(proposal, state, message)
    context = {'state':state.model_dump(mode='json'), 'message':message.model_dump(mode='json'),
               'result':owner.model_dump(mode='json'), 'after':prospective_state(state, owner)}
    check_predicates(proposal.contract.preconditions, context, 'commit_precondition')
    check_predicates(proposal.contract.invariants, context, 'commit_invariant')
    if owner.outcome not in {'failed', 'blocked'}:
        check_predicates(proposal.contract.expected_effects, context, 'commit_effect')


def propose_transition(self, state, message, context=None):
    from bioma.core.transitions import TransitionProposal, read_set
    from bioma.core.action_contracts import ScientificActionContract
    from bioma.core.research import fingerprint
    from bioma.core.methods import method_candidates
    selected = message.payload.get('selected_capability') or (message.payload.get('accepted_offer') or {}).get('capability_id')
    specs = [c for c in self.capabilities if c.agent_id == message.recipient and
             (c.id == selected if selected else message.message_type in c.accepts_message_types)]
    spec = specs[0] if len(specs) == 1 else None
    if spec is None and (selected or any(c.methods for c in self.capabilities)):
        raise ValueError('Transition requires one unambiguous registered capability for this message')
    method = None
    if spec and spec.methods:
        eligible, _ = method_candidates(spec, message.message_type, message.payload)
        method = eligible[0] if eligible else None
        if method is None:
            raise ValueError('No admissible registered method for transition')
    if spec and spec.preconditions:
        raise ValueError('Text preconditions are not executable; migrate to action_contract predicates')
    contract = method.action_contract.model_copy(deep=True) if method else ScientificActionContract()
    if spec:
        contract.resources = sorted(set(contract.resources) | set(spec.resources))
    proposal = TransitionProposal(task_id=state.task_id, message_id=message.id, proposer=message.recipient,
        base_version=state.version, observation_hash=fingerprint(state.model_dump(mode='json')),
        capability_id=spec.id if spec else selected,
        capability_version=str(spec.runtime_profile.get('implementation_version', '1')) if spec else 'unregistered-local',
        method_id=method.id if method else message.payload.get('method_id'), action=message.message_type,
        input_hash=fingerprint(message.payload.get('inputs', state.inputs)), read_set=read_set(state, message),
        write_set=contract.writable_fields, supporting_evidence=[e.id for e in state.evidence],
        contract=contract, result_contract=method.result.model_dump(mode='json') if method else {},
        risk=contract.risk_level, memory_refs=[r['id'] for r in (context or {}).get('memories', [])])
    from bioma.runtime.boundaries import specialize_work_contract
    return specialize_work_contract(state, proposal)
