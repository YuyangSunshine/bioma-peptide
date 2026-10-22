"""Backend-agnostic scientific option proposal and validation."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Protocol

from pydantic import Field, model_validator

from bioma.core.research_contract import ObligationRole, ResearchContract
from .forecast import DecisionSnapshot
from .options import OptionIntent, OptionStep, ScientificOption
from .plan import Lifecycle, ResearchPlan
from .relations import FrozenModel, Reference


class OptionGenerator(Protocol):
    def generate(self, snapshot: DecisionSnapshot, contract: ResearchContract,
                 plan: ResearchPlan, evidence_state, capabilities) -> tuple[ScientificOption, ...]: ...


def _option_id(snapshot, node_id, binding_id, intent):
    raw = f"{snapshot.identity}:{node_id}:{binding_id}:{intent.value}"
    return "option:" + hashlib.sha256(raw.encode()).hexdigest()


def action_information_key(state, targets, provider_id, method_id, inputs):
    """Exact scientific input/version identity, independent of proposal wording.

    Completed work is reusable until data, observations, evidence or parameters
    change. A new obligation or independent Method remains a different action.
    """
    from bioma.core.research import fingerprint
    derived = {'input_refs', 'committed_evidence_refs', '_acceptance_input_hash'}
    return fingerprint({'targets': sorted(targets), 'provider': provider_id, 'method': method_id,
        'inputs': {k:v for k,v in inputs.items() if k not in derived},
        'evidence': [e.model_dump(mode='json') for e in state.evidence],
        'observations': [o.model_dump(mode='json') for o in state.observations],
        'artifacts': [a.model_dump(mode='json') for a in state.artifacts]})


def completed_action(state, targets, provider_id, method_id, inputs):
    key = action_information_key(state, targets, provider_id, method_id, inputs)
    return key in state.status_reason.get('control_completed_actions', {})


def _ready_nodes(plan, snapshot):
    """A plan dependency is a scheduling gate until every source has progress credit."""
    from .relations import RequiresClause
    credited = {row.obligation_ref for row in snapshot.obligation_progress if row.progress_credit}
    nodes = {row.node_id: row for row in plan.nodes}
    blocked = set()
    for dependency in plan.dependencies:
        if not isinstance(dependency, RequiresClause):
            continue
        source_obligations = {ref for node_id in dependency.sources
                              for ref in nodes[node_id].contract_obligation_refs}
        if not source_obligations <= credited:
            blocked.add(dependency.target)
    return {row.node_id for row in plan.nodes} - blocked


def _method_permitted(contract, targets, capability, method):
    """Filter catalog alternatives by stable contract restrictions, when present."""
    applicable = [row for row in contract.method_restrictions
                  if row.obligation_ref is None or row.obligation_ref in targets]
    return not any((row.method_id and row.method_id != method.id) or
                   (row.capability and row.capability != capability.id)
                   for row in applicable)


class PlanDerivedOptionGenerator:
    """Deterministically expose finite strategy candidates already represented by P_t."""

    proposer_id = "plan-derived-proposer-v1"

    def __init__(self, *, max_options: int = 32):
        if max_options < 1:
            raise ValueError("max_options must be positive")
        self.max_options = max_options
        self.last_audit = None
        self.audit_event_kind = "SemanticProposalAudit"

    def generate(self, snapshot, contract, plan, evidence_state, capabilities):
        if ((snapshot.contract_ref, snapshot.contract_version) !=
                (contract.identity, contract.version) or
                (snapshot.plan_ref, snapshot.plan_version, snapshot.plan_identity_hash) !=
                plan.version_identity):
            raise ValueError("stale generation snapshot")
        obligations = {row.id: row for row in contract.requirements}
        unresolved = {row.obligation_ref for row in snapshot.obligation_progress
                      if row.mandatory and row.terminal and not row.progress_credit}
        ready_nodes = _ready_nodes(plan, snapshot)
        proposals = []
        for node in plan.nodes:
            if (node.lifecycle not in {Lifecycle.PENDING, Lifecycle.ACTIVE}
                    or node.node_id not in ready_nodes):
                continue
            targets = tuple(sorted(set(node.contract_obligation_refs) & unresolved))
            if not targets:
                continue
            bindings = node.candidate_bindings
            for binding in bindings:
                expected_evidence = tuple(sorted({kind for ref in targets
                    for kind in obligations[ref].evidence_kinds})) or ("scientific_result",)
                expected_artifacts = tuple(sorted({kind for ref in targets
                    for kind in obligations[ref].artifact_kinds}))
                role = obligations[targets[0]].effective_role
                intents = ([OptionIntent.FALSIFICATION, OptionIntent.DIRECT_PROGRESS]
                           if role is ObligationRole.QUESTION else [OptionIntent.DIRECT_PROGRESS])
                for intent in intents:
                    option_id = _option_id(snapshot, node.node_id, binding.binding_id, intent)
                    inputs = {}
                    for ref in targets:
                        inputs.update(obligations[ref].input_bindings)
                    from bioma.core.research import fingerprint
                    inputs["_acceptance_input_hash"] = fingerprint(obligations[targets[0]].input_bindings)
                    inputs["committed_evidence_refs"] = [item.id for item in evidence_state.evidence]
                    inputs["input_refs"] = [item.id for item in evidence_state.observations
                                            if item.kind == "work_result"] + inputs["committed_evidence_refs"]
                    proposals.append(ScientificOption(option_id=option_id,
                        contract_ref=contract.identity, contract_version=contract.version,
                        plan_ref=plan.plan_id, plan_version=plan.version,
                        plan_identity_hash=plan.version_identity[2],
                        target_plan_node_refs=(node.node_id,),
                        target_contract_obligation_refs=targets,
                        scientific_intent=("Attempt a discriminative falsification." if
                            intent is OptionIntent.FALSIFICATION else "Advance accepted contract progress."),
                        expected_result_classes=expected_evidence,
                        required_artifact_classes=expected_artifacts,
                        termination_condition="accepted result exists",
                        proposer_id=self.proposer_id,
                        provenance_refs=(snapshot.identity, plan.version_identity[2], binding.binding_id),
                        source_snapshot_identity=snapshot.identity, intent_class=intent,
                        rationale="Derived from one unresolved plan node and one preserved candidate binding.",
                        steps=(OptionStep(step_id=option_id + ":step", description=node.description,
                            capability=binding.capability, message_type=binding.message_type,
                            input_bindings_json=json.dumps(inputs, sort_keys=True, separators=(",", ":")),
                            expected_result_classes=expected_evidence,
                            expected_artifact_classes=expected_artifacts,
                            method_hint=binding.method_id),)))
                    if len(proposals) >= self.max_options:
                        return tuple(proposals)
        return tuple(proposals)


class EvidenceDrivenOptionGenerator:
    """Propose bounded alternatives from gaps, evidence and live contracts.

    Capability inventory supplies candidates only. Grounding remains the
    authority for availability, qualification, resources and implementation.
    """

    proposer_id = "evidence-driven-proposer-v1"

    def __init__(self, *, max_options: int = 32, include_plan_options: bool = True):
        if max_options < 1:
            raise ValueError("max_options must be positive")
        self.max_options = max_options
        self.include_plan_options = include_plan_options

    def generate(self, snapshot, contract, plan, evidence_state, capabilities):
        proposals = list(PlanDerivedOptionGenerator(max_options=self.max_options).generate(
            snapshot, contract, plan, evidence_state, capabilities)) if self.include_plan_options else []
        unresolved = {row.obligation_ref for row in snapshot.obligation_progress
            if row.mandatory and row.terminal and not row.progress_credit}
        ready_nodes = _ready_nodes(plan, snapshot)
        obligations = {row.id: row for row in contract.requirements}
        represented = {(step.capability, step.method_hint) for option in proposals for step in option.steps}
        unavailable = evidence_state.status_reason.get("control_failed_bindings", ())
        failed = {tuple(row) for row in unavailable}
        for node in plan.nodes:
            if (node.lifecycle not in {Lifecycle.PENDING, Lifecycle.ACTIVE}
                    or node.node_id not in ready_nodes):
                continue
            targets = tuple(sorted(set(node.contract_obligation_refs) & unresolved))
            if not targets:
                continue
            expected = tuple(sorted({kind for ref in targets for kind in obligations[ref].evidence_kinds}))
            artifacts = tuple(sorted({kind for ref in targets for kind in obligations[ref].artifact_kinds}))
            for capability in sorted(capabilities, key=lambda item: (item.family, item.agent_id, item.id)):
                if not capability.enabled:
                    continue
                for method in sorted(capability.methods, key=lambda item: item.id):
                    if not _method_permitted(contract, targets, capability, method):
                        continue
                    if (capability.agent_id, method.id) in failed:
                        continue
                    result_classes = tuple(sorted(set(expected) & set(method.result.evidence_kinds)))
                    if not result_classes or (capability.id, method.id) in represented:
                        continue
                    intent = {
                        "scientific.research": OptionIntent.EVIDENCE_ACQUISITION,
                        "scientific.compute": OptionIntent.EVIDENCE_TRANSFORMATION,
                        "scientific.study_design": OptionIntent.PREPARATION,
                        "scientific.execution": OptionIntent.DIRECT_PROGRESS,
                        "scientific.evidence_assessment": OptionIntent.FALSIFICATION,
                    }.get(capability.family, OptionIntent.DIRECT_PROGRESS)
                    option_id = _option_id(snapshot, node.node_id,
                                           capability.agent_id + ":" + method.id, intent)
                    inputs = dict(evidence_state.inputs)
                    from bioma.core.research import fingerprint
                    inputs["_acceptance_input_hash"] = fingerprint(obligations[targets[0]].input_bindings)
                    inputs["committed_evidence_refs"] = [item.id for item in evidence_state.evidence]
                    inputs["input_refs"] = [item.id for item in evidence_state.observations
                                            if item.kind == "work_result"] + inputs["committed_evidence_refs"]
                    proposals.append(ScientificOption(option_id=option_id,
                        contract_ref=contract.identity, contract_version=contract.version,
                        plan_ref=plan.plan_id, plan_version=plan.version,
                        plan_identity_hash=plan.version_identity[2],
                        target_plan_node_refs=(node.node_id,),
                        target_contract_obligation_refs=targets,
                        scientific_intent=f"Use {capability.family} to address unresolved evidence obligations.",
                        expected_result_classes=result_classes,
                        required_artifact_classes=artifacts,
                        termination_condition=("one declared WorkResult exists" if method.result.result_channel == "work_result"
                                               else "accepted result exists"),
                        proposer_id=self.proposer_id,
                        provenance_refs=(snapshot.identity, plan.version_identity[2],
                                         capability.agent_id, method.id),
                        source_snapshot_identity=snapshot.identity, intent_class=intent,
                        rationale=("Derived from an unresolved obligation, committed evidence "
                                   "references and a matching capability result contract."),
                        steps=(OptionStep(step_id=option_id + ":step", description=node.description,
                            capability=capability.id, message_type=method.message_type,
                            input_bindings_json=json.dumps(inputs, sort_keys=True, separators=(",", ":")),
                            expected_result_classes=result_classes,
                            expected_artifact_classes=artifacts, method_hint=method.id),)))
                    represented.add((capability.id, method.id))
                    if len(proposals) >= self.max_options:
                        return tuple(proposals)
        return tuple(proposals)


class _AnalysisWork(FrozenModel):
    operation: str = Field(min_length=1)
    variables: tuple[str, ...] = ()
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class _SemanticMethodChoice(FrozenModel):
    node_ref: Reference
    obligation_refs: tuple[Reference, ...] = Field(min_length=1)
    capability_id: Reference
    method_id: Reference
    scientific_question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    intent_class: OptionIntent
    search_query: str | None = Field(default=None, min_length=1, max_length=500)
    analysis_work: _AnalysisWork | None = None


class _SemanticMethodChoices(FrozenModel):
    choices: tuple[_SemanticMethodChoice, ...] = ()
    abstention_reason: str | None = None

    @model_validator(mode="after")
    def justified_defer(self):
        if not self.choices and not (self.abstention_reason or "").strip():
            raise ValueError("Empty proposal requires an explicit scientific defer reason")
        return self


class LLMEvidenceDrivenOptionGenerator:
    """Let a model propose semantics within a deterministic live Method catalog.

    Model output cannot replace source identities, contracts, result classes or
    authority. Declared analysis parameters distinguish work on the same sources;
    grounding and WP1 still decide whether a proposal may execute.
    """

    proposer_id = "llm-evidence-driven-proposer-v1"

    def __init__(self, provider, *, model_id: str, max_options: int = 8):
        if max_options < 2:
            raise ValueError("semantic option generation requires at least two alternatives")
        self.provider = provider
        self.model_id = model_id
        self.max_options = max_options
        self.last_audit = None
        self.audit_event_kind = "SemanticProposalAudit"

    def generate(self, snapshot, contract, plan, evidence_state, capabilities):
        self.last_audit = None
        if ((snapshot.contract_ref, snapshot.contract_version) !=
                (contract.identity, contract.version) or
                (snapshot.plan_ref, snapshot.plan_version, snapshot.plan_identity_hash) !=
                plan.version_identity):
            raise ValueError("stale generation snapshot")
        obligations = {row.id: row for row in contract.requirements}
        unresolved = {row.obligation_ref for row in snapshot.obligation_progress
            if row.mandatory and row.terminal and not row.progress_credit}
        ready_nodes = _ready_nodes(plan, snapshot)
        nodes = {node.node_id: node for node in plan.nodes
                 if node.lifecycle in {Lifecycle.PENDING, Lifecycle.ACTIVE}
                 and node.node_id in ready_nodes
                 and set(node.contract_obligation_refs) & unresolved}
        unavailable = evidence_state.status_reason.get("control_failed_bindings", ())
        failed = {tuple(row) for row in unavailable}
        catalog = {}
        unavailable_methods = []
        rows = []
        for node in nodes.values():
            targets = tuple(sorted(set(node.contract_obligation_refs) & unresolved))
            expected = {kind for ref in targets for kind in obligations[ref].evidence_kinds}
            for capability in sorted(capabilities, key=lambda item: (item.family, item.agent_id, item.id)):
                if not capability.enabled:
                    continue
                for method in sorted(capability.methods, key=lambda item: item.id):
                    if not _method_permitted(contract, targets, capability, method):
                        continue
                    result_classes = tuple(sorted(expected & set(method.result.evidence_kinds)))
                    if (not method.available or not result_classes or
                            (capability.agent_id, method.id) in failed):
                        continue
                    from bioma.core.method_inputs import method_input_reasons
                    missing_inputs = method_input_reasons(method, evidence_state,
                        [o.id for o in evidence_state.observations if o.kind == 'work_result'] +
                        [e.id for e in evidence_state.evidence], snapshot.available_resources)
                    if missing_inputs:
                        unavailable_methods.append({'node_ref': node.node_id, 'method_id': method.id,
                            'availability_reason': missing_inputs})
                        continue
                    bound_inputs = dict(evidence_state.inputs)
                    for ref in targets:
                        bound_inputs.update(obligations[ref].input_bindings)
                    # Retrieval may use a new search query; its exact parameters
                    # are checked after grounding, not excluded as a whole Method.
                    if capability.family not in {'scientific.research','scientific.compute'} and completed_action(
                            evidence_state, targets, capability.agent_id, method.id, bound_inputs):
                        unavailable_methods.append({'node_ref': node.node_id, 'method_id': method.id,
                            'availability_reason': ['Completed with identical inputs and no new scientific information']})
                        continue
                    key = (node.node_id, capability.id, method.id)
                    catalog[key] = (node, targets, capability, method, result_classes)
                    rows.append({"node_ref": node.node_id,
                        "obligation_refs": list(targets), "node_question": node.description,
                        "capability_id": capability.id, "capability_family": capability.family,
                        "method_id": method.id, "method_description": method.description,
                        "result_classes": list(result_classes),
                        "limitations": list(method.limitations),
                        "required_input_kinds": method.required_input_kinds,
                        "required_evidence_refs": method.required_evidence_refs,
                        "execution_environment": method.execution_environment,
                        "availability_reason": "State-bound prerequisites satisfied"})
        if not rows:
            self.last_audit={'catalog_size':0,'accepted_choices':[], 'rejected_choices':[],
                'unavailable_methods':unavailable_methods,'ready_nodes':sorted(ready_nodes),
                'blocked_nodes':sorted(n.node_id for n in plan.nodes if n.node_id not in ready_nodes),
                'termination_cause':'CATALOG_EMPTY'}
            return ()
        evidence = [{"id": item.id, "kind": item.kind, "claim": item.claim,
                     "source": item.source, "method": item.method}
                    for item in evidence_state.evidence[-24:]]
        messages = [{"role": "system", "content": (
            "Propose a bounded set of scientifically useful next actions for the unresolved "
            "research obligations. Select only exact node_ref/capability_id/method_id tuples "
            "from method_catalog. Formulate the concrete scientific subquestion each selected "
            "method should answer. Prefer multiple materially distinct valid alternatives when "
            "available, account for committed evidence, and do not claim that a result exists. "
            "Do not authorize execution, invent tools, change obligations, or predict scientific "
            "outcomes. If no further action is justified, return empty choices with an explicit abstention_reason. For research methods, also provide a concise database search_query "
            "containing only the key scientific concepts. Return exact JSON matching output_schema.")},
            {"role": "user", "content":
            json.dumps({"scientific_goal": contract.query,
                "query_spec": evidence_state.gateway.get("query_spec", {}),
                "unresolved_obligations": [obligations[ref].model_dump(mode="json")
                                            for ref in sorted(unresolved)],
                "committed_evidence": evidence,
                "scientific_state_version": evidence_state.version,
                "qualified_evidence_content": [item.model_dump(mode="json") for item in evidence_state.evidence],
                "observed_work_results": [item.model_dump(mode="json") for item in evidence_state.observations if item.kind == 'work_result'],
                "execution_feedback": evidence_state.status_reason,
                "method_catalog": rows,
                "unavailable_methods": unavailable_methods,
                "maximum_choices": self.max_options,
                "output_schema": _SemanticMethodChoices.model_json_schema()},
                ensure_ascii=False, default=str)}]
        from bioma.llm import parse_json_object
        response = _SemanticMethodChoices.model_validate(
            parse_json_object(self.provider.chat(messages, json_mode=True)))
        choices = response.choices
        proposals = []
        self.last_audit = {"rejected_choices": [], "accepted_choices": [],
            "abstention_reason": response.abstention_reason, "catalog_size": len(rows),
            "unavailable_methods": unavailable_methods,
            'termination_cause':'MODEL_ABSTAINED' if not choices else 'CANDIDATES_RETURNED'}
        seen = set()
        for choice in choices:
            key = (choice.node_ref, choice.capability_id, choice.method_id)
            if key not in catalog:
                self.last_audit["rejected_choices"].append({"choice": choice.model_dump(mode="json"), "reason": "outside the qualified catalog"})
                continue
            work = choice.analysis_work.model_dump(mode='json') if choice.analysis_work else None
            if work is not None:work['variables']=sorted(set(work['variables']))
            work_key = (key, json.dumps(work,sort_keys=True), choice.search_query)
            if work_key in seen:
                self.last_audit['rejected_choices'].append({'choice':choice.model_dump(mode='json'),'reason':'identical bound work in this proposal'})
                continue
            seen.add(work_key)
            node, targets, capability, method, result_classes = catalog[key]
            if set(choice.obligation_refs) != set(targets):
                self.last_audit["rejected_choices"].append({"choice": choice.model_dump(mode="json"), "reason": "changed obligation scope"})
                continue
            self.last_audit["accepted_choices"].append(choice.model_dump(mode="json"))
            artifacts = tuple(sorted({kind for ref in targets
                                      for kind in obligations[ref].artifact_kinds}))
            inputs = dict(evidence_state.inputs)
            for ref in targets:
                inputs.update(obligations[ref].input_bindings)
            from bioma.core.research import fingerprint
            inputs["_acceptance_input_hash"] = fingerprint(obligations[targets[0]].input_bindings)
            inputs["committed_evidence_refs"] = [item.id for item in evidence_state.evidence]
            inputs["input_refs"] = [item.id for item in evidence_state.observations
                                    if item.kind == "work_result"] + inputs["committed_evidence_refs"]
            if choice.analysis_work and capability.family == 'scientific.compute':
                work = choice.analysis_work.model_dump(mode='json')
                work['variables'] = sorted(set(work['variables']))
                inputs['analysis_work'] = work
            if choice.search_query and capability.family == "scientific.research":
                inputs["search_query"] = choice.search_query
            option_id = _option_id(snapshot, node.node_id,
                                   capability.agent_id + ":" + method.id + ':' + fingerprint(inputs), choice.intent_class)
            proposals.append(ScientificOption(option_id=option_id,
                contract_ref=contract.identity, contract_version=contract.version,
                plan_ref=plan.plan_id, plan_version=plan.version,
                plan_identity_hash=plan.version_identity[2],
                target_plan_node_refs=(node.node_id,),
                target_contract_obligation_refs=targets,
                scientific_intent=choice.scientific_question,
                expected_result_classes=result_classes,
                required_artifact_classes=artifacts,
                termination_condition=("one declared WorkResult exists" if method.result.result_channel == "work_result"
                                               else "accepted result exists"),
                proposer_id=self.proposer_id,
                provenance_refs=(snapshot.identity, plan.version_identity[2],
                                 capability.agent_id, method.id, self.model_id),
                source_snapshot_identity=snapshot.identity,
                intent_class=choice.intent_class, rationale=choice.rationale,
                steps=(OptionStep(step_id=option_id + ":step",
                    description=choice.scientific_question,
                    capability=capability.id, message_type=method.message_type,
                    input_bindings_json=json.dumps(inputs, sort_keys=True, separators=(",", ":")),
                    expected_result_classes=result_classes,
                    expected_artifact_classes=artifacts, method_hint=method.id),)))
            if len(proposals) >= self.max_options:
                break
        if choices and not proposals:
            raise ValueError("all semantic choices rejected: outside the qualified catalog or obligation scope")
        return tuple(proposals)


class CallableOptionGenerator:
    """Adapter for role agents or model/LLM backends; invocation is caller-controlled."""

    def __init__(self, proposer_id: str, propose: Callable[..., Any]):
        self.proposer_id, self.propose = proposer_id, propose

    def generate(self, snapshot, contract, plan, evidence_state, capabilities):
        raw = self.propose(snapshot=snapshot.model_dump(mode="json"),
            contract=contract.model_dump(mode="json"), plan=plan.model_dump(mode="json"),
            evidence_projection={"state_version": evidence_state.version,
                "evidence_refs": [item.id for item in evidence_state.evidence]},
            capabilities=[item.model_dump(mode="json") for item in capabilities])
        values = raw.get("options", raw) if isinstance(raw, dict) else raw
        return tuple(ScientificOption.model_validate(item) for item in values)


RoleOptionGenerator = CallableOptionGenerator
ModelOptionGenerator = CallableOptionGenerator


def _equivalence_key(option: ScientificOption):
    return json.dumps({"targets": option.target_plan_node_refs,
        "obligations": option.target_contract_obligation_refs,
        "intent": option.intent_class, "steps": [step.model_dump(mode="json") for step in option.steps],
        "termination": option.termination_condition}, sort_keys=True, separators=(",", ":"), default=str)


def validate_generated_options(options, *, snapshot: DecisionSnapshot,
                               contract: ResearchContract, plan: ResearchPlan,
                               active_options=()) -> tuple[ScientificOption, ...]:
    """Validate strategy semantics only; grounding remains executability authority."""
    nodes = {node.node_id for node in plan.nodes}
    obligations = {row.id for row in contract.requirements}
    restrictions = contract.method_restrictions
    accepted, seen = [], {_equivalence_key(item) for item in active_options}
    for option in sorted(options, key=lambda item: item.identity):
        if (option.source_snapshot_identity != snapshot.identity or
                (option.contract_ref, option.contract_version) !=
                (contract.identity, contract.version) or
                (option.plan_ref, option.plan_version, option.plan_identity_hash) !=
                plan.version_identity):
            raise ValueError("stale generated option")
        if (set(option.target_plan_node_refs) - nodes or
                set(option.target_contract_obligation_refs) - obligations):
            raise ValueError("generated option targets unknown plan or contract references")
        if not option.target_plan_node_refs or not option.target_contract_obligation_refs:
            raise ValueError("generated option has no scientific target")
        if option.intent_class is None or not option.rationale:
            raise ValueError("generated option lacks intent class or rationale")
        for step in option.steps:
            applicable = [row for row in restrictions if row.obligation_ref is None or
                          row.obligation_ref in option.target_contract_obligation_refs]
            if any((row.method_id and row.method_id != step.method_hint) or
                   (row.capability and row.capability != step.capability) for row in applicable):
                raise ValueError("generated option violates explicit method restriction")
        key = _equivalence_key(option)
        if key in seen and not option.duplicate_justification:
            continue
        seen.add(key)
        accepted.append(option)
    return tuple(accepted)
