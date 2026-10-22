"""Gateway split and temporary strategy-to-legacy-execution adapter.

Acceptance never calls the strategy adapter. It consumes stable obligations
and committed evidence. This module generates no evidence and runs no planner.
"""
import json

from bioma.core.research import EvidenceRequirement, contract_for, fingerprint
from bioma.core.research_contract import (
    AcceptanceObligation, MethodRestriction, ObligationRole, ResearchContract, canonical,
)
from bioma.scientific_control.plan import (
    Lifecycle, MethodBinding, NodeKind, PlanNode, ResearchPlan, read_plan,
)
from bioma.scientific_control.relations import RequiresClause


def validate_plan_contract(contract, plan):
    if plan.contract_ref != contract.identity:
        raise ValueError("plan must bind exact contract identity")
    obligations = {r.id for r in contract.requirements}
    claims = {c['id'] for c in contract.claims}
    execution_constraints = json.loads(contract.execution_constraints_json)
    budget = json.loads(contract.resource_budget_json)
    for field in ('max_total_repairs', 'max_repairs_per_requirement'):
        if field in budget and getattr(plan, field) > budget[field]:
            raise ValueError('plan repair policy exceeds contract budget')
    for node in plan.nodes:
        if set(node.contract_obligation_refs) - obligations or set(node.question_refs) - claims:
            raise ValueError("unknown plan obligation/question reference")
        # Historical superseded choices remain auditable after an amendment.
        if node.lifecycle == Lifecycle.SUPERSEDED:
            continue
        for binding in node.candidate_bindings:
            parameters = json.loads(binding.execution_requirements_json)
            for ref in node.contract_obligation_refs:
                for key, required in execution_constraints.get(ref, {}).items():
                    if key in {'method_id', 'engine', 'responsibility'} or not required:
                        continue
                    actual = parameters.get(key)
                    if (isinstance(required, list) and not set(required) <= set(actual or [])) or (
                            not isinstance(required, list) and actual != required):
                        raise ValueError('plan binding relaxes explicit execution constraint: ' + key)
            for restriction in contract.method_restrictions:
                if restriction.obligation_ref and restriction.obligation_ref not in node.contract_obligation_refs:
                    continue
                for field in ('method_id', 'engine', 'capability'):
                    required = getattr(restriction, field)
                    if required is not None and getattr(binding, field) != required:
                        raise ValueError('plan binding violates explicit user ' + field + ' restriction')
    if plan.entry_binding:
        for restriction in contract.method_restrictions:
            if restriction.obligation_ref is None:
                for field in ('method_id', 'engine', 'capability'):
                    required = getattr(restriction, field)
                    if required is not None and getattr(plan.entry_binding, field) != required:
                        raise ValueError('entry binding violates explicit user restriction')
    return plan


def split_interpretation(task, requirements, claims):
    """One validated interpretation deterministically yields two authorities."""
    explicit = task.gateway.get('user_execution_constraints')
    restrictions = [MethodRestriction.model_validate(r)
                    for r in task.gateway.get('user_method_restrictions', [])]
    constraints = {}
    explicit_roles = task.gateway.get("obligation_roles") or {}
    obligations, nodes, dependencies = [], [], []
    for r in requirements:
        data = r.model_dump(mode='json')
        execution = dict(r.execution_requirements)
        # Semantic execution requirements are explicit constraints, whereas
        # catalog-bound capability/method/message fields are only strategy.
        user = dict((explicit or {}).get(r.id, {})
                    if explicit is not None and r.source != 'entry_contract' else execution)
        constraints[r.id] = user
        if any(user.get(k) for k in ('method_id', 'engine', 'responsibility')):
            restrictions.append(MethodRestriction(obligation_ref=r.id,
                method_id=user.get('method_id'), engine=user.get('engine'),
                capability=user.get('responsibility')))
        stable = {k: v for k, v in data.items() if k in AcceptanceObligation.model_fields}
        stable.update(predicates_json=canonical(data['predicates']),
            artifact_predicates_json=canonical(data['artifact_predicates']),
            input_bindings_json=canonical(data['input_bindings']))
        stable['role'] = explicit_roles.get(r.id) or (
            ObligationRole.QUESTION if r.mode == 'claim_assessment' else
            ObligationRole.DELIVERY if r.mode == 'delivery' else
            ObligationRole.ACHIEVEMENT)
        obligations.append(AcceptanceObligation.model_validate(stable))
        binding = None
        if r.capability:
            applicable = [x for x in restrictions if x.obligation_ref in (None, r.id)]
            selected_method = r.method_id or next((x.method_id for x in applicable if x.method_id), None)
            engine = user.get('engine') or next((x.engine for x in applicable if x.engine), None)
            if engine:
                execution['engine'] = engine
            binding = MethodBinding(binding_id=r.id + ':binding:1', capability=r.capability,
                method_id=selected_method, message_type=r.message_type,
                engine=engine, execution_requirements_json=canonical(execution))
        nodes.append(PlanNode(node_id=r.id, kind=NodeKind.QUESTION if r.mode == 'claim_assessment' else NodeKind.WORK_ITEM,
            description=r.description, contract_obligation_refs=(r.id,), question_refs=tuple(r.claim_ids),
            candidate_bindings=(binding,) if binding else (),
            selected_binding_id=binding.binding_id if binding else None, repair_enabled=r.repair_enabled))
        if r.depends_on:
            dependencies.append(RequiresClause(sources=tuple(r.depends_on), target=r.id))
    from bioma.core.research import scientific_input_identity
    contract = ResearchContract(query=task.objective, input_hash=fingerprint(scientific_input_identity(task)),
        claims_json=canonical(claims), requirements=tuple(obligations),
        scope_json=canonical(task.gateway.get('user_scope') or {}),
        hard_constraints_json=canonical(task.gateway.get('constraints') or []),
        resource_budget_json=canonical(task.gateway.get('user_resource_budget') or {}),
        execution_constraints_json=canonical(constraints), method_restrictions=tuple(restrictions))
    entry = task.gateway.get('selected_entry_capability')
    entry_method = None if task.gateway.get('method_selection') == 'provider' else task.gateway.get('selected_method_id')
    global_restrictions = [r for r in restrictions if r.obligation_ref is None]
    entry_method = entry_method or next((r.method_id for r in global_restrictions if r.method_id), None)
    entry_execution = dict(task.gateway.get('execution_requirements') or {})
    entry_engine = entry_execution.get('engine') or next((r.engine for r in global_restrictions if r.engine), None)
    if entry_engine:
        entry_execution['engine'] = entry_engine
    entry_binding = MethodBinding(binding_id='entry', capability=entry,
        method_id=entry_method, message_type=task.gateway.get('selected_message_type') or 'task.submitted',
        engine=entry_engine, execution_requirements_json=canonical(entry_execution)) if entry else None
    budget = json.loads(contract.resource_budget_json)
    plan = ResearchPlan(plan_id='plan:' + task.id, version=1, contract_ref=contract.identity,
        evidence_snapshot_ref='initial:' + fingerprint({'evidence': [], 'artifacts': [], 'constraints': []}),
        nodes=tuple(nodes), dependencies=tuple(dependencies), entry_binding=entry_binding,
        max_total_repairs=min(8, budget.get('max_total_repairs', 8)),
        max_repairs_per_requirement=min(2, budget.get('max_repairs_per_requirement', 2)))
    return contract, validate_plan_contract(contract, plan)


def plan_for(state):
    contract = contract_for(state)
    if contract is None or contract.schema_version == 'research-contract-v1':
        return None
    raw = state.gateway.get('research_plan')
    if not raw:
        raise ValueError('new contract requires a versioned research plan')
    plan = read_plan(raw)
    if not isinstance(plan, ResearchPlan):
        raise ValueError('new contract requires plan schema 2')
    if state.gateway.get('research_plan_ref') != list(plan.version_identity):
        raise ValueError('research plan identity mismatch')
    return validate_plan_contract(contract, plan)


def execution_requirements_for(state):
    """Temporary legacy request recipes, NOT acceptance authority."""
    contract = contract_for(state)
    if contract is None:
        return []
    plan = plan_for(state)
    if plan is None:
        return contract.requirements
    rows = []
    by_id = {r.id: r for r in contract.requirements}
    for node in plan.nodes:
        if node.lifecycle in {Lifecycle.SUPERSEDED, Lifecycle.BLOCKED}:
            continue
        binding = next((b for b in node.candidate_bindings if b.binding_id == node.selected_binding_id), None)
        clauses = [d for d in plan.dependencies if isinstance(d, RequiresClause) and d.target == node.node_id]
        if len(clauses) > 1:
            raise ValueError('legacy execution adapter cannot choose alternative prerequisite clauses')
        source_nodes = [source_node for clause in clauses for source in clause.sources
                        for source_node in plan.nodes if source_node.node_id == source]
        if any(not n.contract_obligation_refs or n.lifecycle in {Lifecycle.BLOCKED, Lifecycle.SUPERSEDED}
               for n in source_nodes):
            raise ValueError('legacy execution adapter cannot ground unavailable or uncovered prerequisites')
        parent_refs = {ref for clause in clauses for source in clause.sources
                       for source_node in plan.nodes if source_node.node_id == source
                       for ref in source_node.contract_obligation_refs}
        for ref in node.contract_obligation_refs:
            data = by_id[ref].acceptance_payload()
            data.update(repair_enabled=node.repair_enabled)
            data['depends_on'] = sorted(set(data['depends_on']) | parent_refs)
            if binding:
                execution = json.loads(binding.execution_requirements_json)
                execution.update(responsibility=binding.capability, method_id=binding.method_id)
                if binding.engine:
                    execution['engine'] = binding.engine
                data.update(capability=binding.capability, method_id=binding.method_id,
                    message_type=binding.message_type,
                    execution_requirements=execution)
            rows.append(EvidenceRequirement.model_validate(data))
    return rows


def scheduling_dependencies(state, requirement_ids):
    """Planning may delay execution, never add/remove acceptance obligations."""
    ids = set(requirement_ids)
    rows = {r.id: r for r in execution_requirements_for(state)}
    dependencies, pending = set(), list(ids)
    while pending:
        key = pending.pop()
        for parent in rows[key].depends_on if key in rows else ():
            if parent not in dependencies and parent not in ids:
                dependencies.add(parent)
                pending.append(parent)
    return dependencies


def repair_limits(state):
    contract = contract_for(state)
    plan = plan_for(state)
    policy = plan or contract
    return policy.max_repairs_per_requirement, policy.max_total_repairs
