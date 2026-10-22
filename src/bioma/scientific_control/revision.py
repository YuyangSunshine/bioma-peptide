"""Explicit versioned plan edits; no autonomous policy or evidence writes."""
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .plan import Dependency, Lifecycle, MethodBinding, PlanNode, ResearchPlan
from .relations import FrozenModel, Reference


class RevisionTrigger(str, Enum):
    INITIALIZATION = 'INITIALIZATION'
    EVIDENCE_UPDATE = 'EVIDENCE_UPDATE'
    EXECUTION_FAILURE = 'EXECUTION_FAILURE'
    CAPABILITY_CHANGE = 'CAPABILITY_CHANGE'
    CONFLICT = 'CONFLICT'
    CONTRADICTION = 'CONTRADICTION'
    RESOURCE_CHANGE = 'RESOURCE_CHANGE'
    EFFECT_RECONCILED = 'EFFECT_RECONCILED'
    OPTION_COMPLETED = 'OPTION_COMPLETED'
    OPTION_BLOCKED = 'OPTION_BLOCKED'
    USER_AMENDMENT = 'USER_AMENDMENT'


class PlanChanges(FrozenModel):
    add_nodes: tuple[PlanNode, ...] = ()
    update_nodes: tuple[PlanNode, ...] = ()
    supersede_node_ids: tuple[Reference, ...] = ()
    dependencies: tuple[Dependency, ...] | None = None
    entry_binding: MethodBinding | None = None
    evidence_snapshot_ref: Reference | None = None


class PlanRevision(FrozenModel):
    schema_version: Literal['plan-revision-v1'] = 'plan-revision-v1'
    revision_id: Reference
    contract_ref: Reference
    before_plan_version: int = Field(ge=1, strict=True)
    after_plan_version: int = Field(ge=2, strict=True)
    before_plan_identity: tuple[str, int, str]
    trigger_type: RevisionTrigger
    trigger_refs: tuple[Reference, ...] = Field(min_length=1)
    changes: PlanChanges
    rationale: str = Field(min_length=1)
    created_at: datetime

    @model_validator(mode='after')
    def valid_revision(self):
        if self.after_plan_version != self.before_plan_version + 1:
            raise ValueError('plan revision must increment exactly once')
        if self.created_at.tzinfo is None:
            raise ValueError('revision timestamp must be timezone-aware')
        object.__setattr__(self, 'created_at', self.created_at.astimezone(timezone.utc))
        return self


def apply_plan_revision(contract, plan: ResearchPlan, revision: PlanRevision, *,
                        previous_contract=None, amendment=None, authorized_actor=None):
    """Apply an explicit edit to an exact snapshot, preserving replaced work."""
    from bioma.core.research_planning import validate_plan_contract
    if revision.before_plan_identity != plan.version_identity or revision.before_plan_version != plan.version:
        raise ValueError('stale PlanRevision')
    if revision.contract_ref != contract.identity:
        raise ValueError('revision cannot modify the contract')
    if plan.contract_ref != contract.identity:
        if (revision.trigger_type != RevisionTrigger.USER_AMENDMENT or
                contract.previous_contract_ref != plan.contract_ref or
                contract.identity not in revision.trigger_refs):
            raise ValueError('contract change requires explicit amendment lineage')
        from bioma.core.research_contract import amend_contract
        if (previous_contract is None or amendment is None or
                previous_contract.identity != plan.contract_ref or
                amend_contract(previous_contract, amendment, authorized_actor=authorized_actor) != contract):
            raise ValueError('contract rebase requires a valid authorized amendment')
    changes = revision.changes
    old = {n.node_id: n for n in plan.nodes}
    supersede = set(changes.supersede_node_ids)
    if len(supersede) != len(changes.supersede_node_ids) or supersede - old.keys():
        raise ValueError('unknown or duplicate superseded node')
    if any(n.lifecycle == Lifecycle.SUPERSEDED for n in old.values() if n.node_id in supersede):
        raise ValueError('node already superseded')
    if any(n.node_id in old or n.lifecycle != Lifecycle.PENDING for n in changes.add_nodes):
        raise ValueError('new work must be pending with a fresh node identity')
    updates = {node.node_id: node for node in changes.update_nodes}
    if len(updates) != len(changes.update_nodes) or set(updates) - old.keys():
        raise ValueError('updated work must name existing nodes exactly once')
    immutable = {'node_id', 'kind', 'description', 'contract_obligation_refs',
                 'supersedes_node_ref', 'question_refs', 'evidence_gap_refs'}
    if any(any(getattr(node, field) != getattr(old[node.node_id], field) for field in immutable)
           for node in updates.values()):
        raise ValueError('node update cannot rewrite scientific target or lineage')
    if any(n.supersedes_node_ref and n.supersedes_node_ref not in supersede for n in changes.add_nodes):
        raise ValueError('replacement must name a node superseded by this revision')
    if changes.entry_binding is not None and changes.entry_binding != plan.entry_binding:
        entry = changes.entry_binding
        replacements = [n for n in changes.add_nodes if n.supersedes_node_ref in supersede]
        if not any(b.binding_id == n.selected_binding_id and
                   (b.capability, b.method_id, b.provider_id, b.engine, b.message_type) ==
                   (entry.capability, entry.method_id, entry.provider_id, entry.engine, entry.message_type)
                   and set(old[n.supersedes_node_ref].contract_obligation_refs) <= set(n.contract_obligation_refs)
                   for n in replacements for b in n.candidate_bindings):
            raise ValueError('entry replacement requires superseded obligation-preserving work lineage')
    nodes = [n.model_copy(update={'lifecycle': Lifecycle.SUPERSEDED}) if n.node_id in supersede
             else updates.get(n.node_id, n) for n in plan.nodes] + list(changes.add_nodes)
    data = plan.model_dump(mode='json')
    data.update(version=revision.after_plan_version, contract_ref=contract.identity,
                nodes=[n.model_dump(mode='json') for n in nodes])
    if changes.evidence_snapshot_ref is not None:
        data['evidence_snapshot_ref'] = changes.evidence_snapshot_ref
    if changes.dependencies is not None:
        data['dependencies'] = [d.model_dump(mode='json') for d in changes.dependencies]
    if changes.entry_binding is not None:
        data['entry_binding'] = changes.entry_binding.model_dump(mode='json')
    result = ResearchPlan.model_validate(data)
    return validate_plan_contract(contract, result)
