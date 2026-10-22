"""Versioned research obligations, independent of delivery and claim truth.

Contracts live in the immutable gateway envelope so historical state/receipt
schemas remain replayable. Agents may assess obligations, never rewrite them.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from bioma.core.community import ExecutionRequirements, merge_execution_requirements


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, default=str).encode()).hexdigest()


class EvidencePredicate(ResearchModel):
    field: str = Field(min_length=1, description="Dot path: object keys, numeric array indices (results.0.p), or array.length for cardinality. Optional leading $. denotes the JSON root; $ alone is the whole value. No wildcard, recursive descent, bracket expressions or executable code.")
    operator: Literal["exists", "eq", "gte", "lte", "contains"] = "exists"
    value: Any = None


class ArtifactPredicate(EvidencePredicate):
    artifact_name: str = Field(min_length=1, description='Exact requested JSON output basename, such as stats.json.')


DataKind = Literal['hypothesis_proposal', 'computed_prediction', 'calculation', 'analysis_result',
                   'experimental_observation', 'simulation', 'retrieved_fact', 'study_design']


class EvidenceNeed(ResearchModel):
    """Semantic Gateway proposal: no internal contract IDs or dispatch controls."""
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    mandatory: bool = True
    evidence_kind: Literal['prediction', 'analysis', 'protocol', 'execution', 'structure_resolution', 'evidence_graph_synthesis']
    depends_on: list[str] = Field(default_factory=list,
        description='IDs of necessary result nodes whose committed evidence this result consumes. This is an evidence dependency, not an Agent workflow. A report about computed candidates must depend on their candidate evidence.')
    data_kind: DataKind | None = Field(default=None, description='Use analysis_result for data/statistical analysis, calculation for deterministic sequence descriptors, computed_prediction for executed predictors, hypothesis_proposal for untested candidate proposals. Leave null if the user does not restrict the evidence class.')
    artifact_names: list[str] = Field(default_factory=list, description='Exact user-requested output basenames, not internal delivery IDs. Empty unless explicitly requested.')
    execution_requirements: ExecutionRequirements = Field(default_factory=ExecutionRequirements,
        description='Execution constraints for THIS result only. A downstream plot requirement must not constrain an upstream prediction. Preserve explicit engines such as R on the analysis that uses them.')
    artifact_predicates: list[ArtifactPredicate] = Field(default_factory=list,
        description='Only explicit checks on JSON file contents. File existence is enforced by artifact_names; never invent a file_exists field.')

    @model_validator(mode='after')
    def evidence_is_not_execution_wording(self):
        if self.evidence_kind == 'analysis' and self.data_kind in {'simulation', 'experimental_observation', 'study_design'}:
            raise ValueError('Analysis produces analysis_result or calculation. Simulation/experiment describes its INPUT provenance, not its result class; preserve that provenance in description and constraints.')
        if self.evidence_kind == 'execution' and self.data_kind in {
                'analysis_result', 'calculation', 'computed_prediction', 'hypothesis_proposal', 'study_design'}:
            raise ValueError('Execution evidence means a job/observation record; select prediction, analysis or protocol for that requested result')
        return self

    def bind(self, execution_requirements):
        return EvidenceRequirement(id=self.id, description=self.description, claim_ids=self.claim_ids,
            mandatory=self.mandatory, evidence_kinds=[self.evidence_kind],
            depends_on=self.depends_on,
            data_kinds=[self.data_kind] if self.data_kind else [], artifact_names=self.artifact_names,
            artifact_predicates=self.artifact_predicates, execution_requirements=merge_execution_requirements(
                execution_requirements, self.execution_requirements))


class EvidenceRequirement(ResearchModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    mandatory: bool = True
    mode: Literal["evidence", "claim_assessment", "delivery"] = "evidence"
    evidence_kinds: list[str] = Field(default_factory=list)
    data_kinds: list[DataKind] = Field(default_factory=list)
    artifact_kinds: list[str] = Field(default_factory=list)
    artifact_names: list[str] = Field(default_factory=list)
    artifact_predicates: list[ArtifactPredicate] = Field(default_factory=list)
    predicates: list[EvidencePredicate] = Field(default_factory=list)
    min_count: int = Field(default=1, ge=1)
    input_bindings: dict[str, Any] = Field(default_factory=dict)
    input_hash: str = ""
    depends_on: list[str] = Field(default_factory=list)
    delivery_id: str | None = None
    capability: str = ""
    message_type: str = "task.submitted"
    method_id: str | None = None
    stage: Literal['work', 'review', 'delivery'] = 'work'
    execution_requirements: dict[str, Any] = Field(default_factory=dict)
    repair_enabled: bool = True
    allow_insufficient: bool = False
    semantic_review: bool = True
    source: str = "user_request"

    @model_validator(mode="after")
    def executable_acceptance(self):
        if self.mode != 'claim_assessment' and not self.evidence_kinds:
            raise ValueError('An evidence obligation requires explicit evidence_kinds')
        return self


def validate_requirement_graph(requirements, claim_records):
    ids = [r.id for r in requirements]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate research requirement id")
    claims = [str(c["id"]) for c in claim_records]
    if len(claims) != len(set(claims)):
        raise ValueError("Duplicate research claim id")
    by_id = {r.id: r for r in requirements}
    def visit(key, path):
        if key in path:
            raise ValueError("Research requirement dependency cycle")
        for parent in by_id[key].depends_on:
            if parent not in by_id:
                raise ValueError("Unknown research requirement dependency: " + parent)
            visit(parent, path | {key})
    for r in requirements:
        if set(r.claim_ids) - set(claims):
            raise ValueError("Requirement references unknown claim")
        if r.mode == "claim_assessment" and not r.claim_ids:
            raise ValueError("Claim assessment requires claim_ids")
        visit(r.id, set())


class ResearchContractV1(ResearchModel):
    schema_version: Literal["research-contract-v1"] = "research-contract-v1"
    version: int = Field(default=1, ge=1)
    query: str
    input_hash: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    requirements: list[EvidenceRequirement]
    max_repairs_per_requirement: int = Field(default=2, ge=0, le=10)
    max_total_repairs: int = Field(default=8, ge=0, le=50)

    @model_validator(mode="after")
    def valid_graph(self):
        validate_requirement_graph(self.requirements, self.claims)
        return self

    @property
    def identity(self):
        return fingerprint(self.model_dump(mode="json"))


class RequirementAssessment(ResearchModel):
    requirement_id: str
    status: Literal["satisfied", "missing", "insufficient", "invalid", "pending", "unavailable"]
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    claim_status: str | None = None
    gap_id: str | None = None
    review_method: str = 'structural_acceptance'
    limitations: list[str] = Field(default_factory=list)


class ResearchAssessment(ResearchModel):
    schema_version: Literal["research-assessment-v1"] = "research-assessment-v1"
    contract_id: str
    contract_version: int
    snapshot_hash: str
    requirements: list[RequirementAssessment]
    complete: bool
    claim_synthesis: dict[str, Any] | None = None


AUDIT_KINDS = frozenset({"research_assessment", "evidence_gap"})


def contract_for(state):
    raw = state.gateway.get("research_contract")
    if not raw:
        return None
    if raw.get('schema_version', 'research-contract-v1') == 'research-contract-v1':
        return ResearchContractV1.model_validate(raw)
    return ResearchContract.model_validate(raw)


def evidence_snapshot(state):
    """Content-bound; excludes the review itself, never its scientific inputs."""
    from bioma.core.acceptance import protocol_for, delivery_basis_valid
    delivered = {e.id for e in state.evidence if isinstance(e.value, dict) and e.value.get('delivery_basis')
                 and delivery_basis_valid(state, e)} if protocol_for(state) else set()
    return fingerprint({
        "evidence": [e.model_dump(mode="json") for e in state.evidence if e.id not in delivered and not (e.kind in AUDIT_KINDS and e.source == 'comagent')],
        "artifacts": [a.model_dump(mode="json") for a in state.artifacts if not (a.metadata.get('delivery_basis') and delivered.intersection(a.evidence_ids)) and not (a.source == 'comagent'
            and a.kind in {'research-assessment-json', 'research-evidence-graph-json'} and a.metadata.get('research_review'))],
        "constraints": [c.model_dump(mode="json") for c in state.constraints],
    })


def review_snapshot(state, *, check_files=True):
    """New-review content identity; historical evidence_snapshot is unchanged.

    Only an explicit different object may be excluded. Missing object identity,
    dependencies, contrary observations and constraints are retained. No
    filtering by support direction or acceptance predicates is permitted.
    """
    from pathlib import Path
    contract = contract_for(state)
    if contract is None or contract.schema_version != 'research-contract-v2':
        raise ValueError('Production review requires the current research contract')
    subject = json.loads(contract.scope_json).get('scientific_object')
    facts = [e for e in state.evidence if e.kind not in AUDIT_KINDS
             and not (isinstance(e.value, dict) and e.value.get('delivery_basis'))]
    included = {e.id for e in facts if not subject or not isinstance(e.value, dict)
                or e.value.get('scientific_object') in (None, subject)}
    # An otherwise excluded record cannot hide a dependency on reviewed work.
    while True:
        linked = {e.id for e in state.evidence if set(e.parent_ids) & included}
        parents = {p for e in state.evidence if e.id in included for p in e.parent_ids}
        expanded = included | linked | parents
        if expanded == included:
            break
        included = expanded
    facts = [e for e in facts if e.id in included]
    excluded_artifacts = {a for e in state.evidence if e.id not in included for a in e.artifact_ids}
    included_artifacts = {a for e in facts for a in e.artifact_ids}
    artifacts = [a for a in state.artifacts if not a.metadata.get('delivery_basis')
                 and (a.id not in excluded_artifacts or a.id in included_artifacts)]
    if check_files:
        for artifact in artifacts:
            path = Path(artifact.uri)
            digest = artifact.metadata.get('sha256')
            if not digest or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Reviewed artifact is missing or changed: ' + artifact.id)
    return fingerprint({'contract': contract.identity,
        'inputs': state.inputs,
        'evidence': [e.model_dump(mode='json') for e in facts],
        'artifacts': [a.model_dump(mode='json') for a in artifacts],
        'constraints': [c.model_dump(mode='json') for c in state.constraints]})


def scientific_input_identity(task):
    """Contract identity including governed cross-run inputs without changing legacy hashes."""
    if not task.external_work_results:
        return task.inputs
    return {"inputs": task.inputs, "external_work_results": [
        row.model_dump(mode="json") for row in task.external_work_results]}

def prepare_contract(task):
    """Bind explicit obligations plus established delivery/entry contracts once.

    Explicit worker tasks without a gateway have no research contract.
    Interpreted queries bind obligations and work/research acceptance once.
    """
    if not task.gateway:
        return task
    query_spec = task.gateway.get("query_spec")
    if query_spec:
        from bioma.gateway import verify_query_spec
        verified = verify_query_spec(task.objective, query_spec)
        # Intake metadata cannot create Claims, Evidence obligations or a plan.
        # Explicit formal contracts supplied by callers retain their own gates.
        if task.gateway.get('intake_version') == 'research-contract-intake-v1':
            from bioma.runtime.query_compiler import QueryContractCompiler
            QueryContractCompiler.validate_intake(task, verified)
            return task
    else:
        # Historical persisted tasks retain their original admission rule.
        from bioma.core.query_coverage import verify_coverage_certificate
        verify_coverage_certificate(task.objective, task.gateway)
    if task.gateway.get("research_contract"):
        contract = contract_for(task)
        if contract.query != task.objective or contract.input_hash != fingerprint(scientific_input_identity(task)):
            raise ValueError("Research contract does not match the original query/inputs")
        from bioma.core.acceptance import protocol_for
        protocol_for(task)
        if contract.schema_version == 'research-contract-v2':
            from bioma.core.research_planning import plan_for
            plan_for(task)
        return task
    from bioma.core.deliverables import task_contracts
    from bioma.core.methods import entry_result_contract
    requirements = [EvidenceRequirement.model_validate(r) for r in task.gateway.get("evidence_requirements", [])]
    provider_selected = task.gateway.get('method_selection') == 'provider'
    if provider_selected:
        requirements = [r.model_copy(update={'method_id': None}) for r in requirements]
    known_producers = {'prediction': ('scientific.computational_research', 'task.submitted'),
        'analysis': ('scientific.data_analysis', 'task.submitted'),
        'protocol': ('research.study_design', 'task.submitted'),
        'structure_resolution': ('scientific.computational_research', 'structure.requested')}
    for requirement in requirements:
        if not requirement.capability and len(requirement.evidence_kinds) == 1:
            producer = known_producers.get(requirement.evidence_kinds[0])
            if producer:
                requirement.capability, requirement.message_type = producer
        if (not provider_selected and not requirement.method_id and requirement.capability == task.gateway.get('selected_entry_capability')
                and requirement.message_type == task.gateway.get('selected_message_type', 'task.submitted')):
            requirement.method_id = task.gateway.get('selected_method_id')
    claims = task.gateway.get("claims") or []
    deliveries = task_contracts(task)
    scientific_requirements = list(requirements)
    for row in deliveries:
        scientific_parents = {parent for r in scientific_requirements
            if row.get('stage') == 'delivery' and row['evidence'] in r.evidence_kinds
            for parent in r.depends_on}
        if (row.get('stage') == 'delivery' and row.get('input_scope') == 'task'
                and row['evidence'] in {'artifact_analysis', 'evidence_graph_synthesis'}):
            # A task-wide final package must include all mandatory scientific
            # work, even if the semantic report omitted a support edge. Local
            # visualizations keep their narrower producer dependencies.
            scientific_parents.update(r.id for r in scientific_requirements
                if r.mandatory and r.stage == 'work' and r.mode != 'claim_assessment'
                and not set(r.evidence_kinds) & {'artifact_analysis', 'evidence_graph_synthesis'})
        requirements.append(EvidenceRequirement(
            id="delivery:" + row["id"], description=row["label"], mode="delivery",
            evidence_kinds=[row["evidence"]], artifact_kinds=row["artifacts"], delivery_id=row["id"],
            capability=row["capability"], message_type=row["message_type"], input_bindings=row.get("input_bindings", {}),
            method_id=row.get('method_id'), stage=row.get('stage', 'work'),
            input_hash=row["input_hash"], depends_on=sorted(scientific_parents |
                {"delivery:" + d for d in row.get("depends_on", [])}),
            source="delivery_contract", semantic_review=row.get('stage') != 'delivery'))
    selected = task.gateway.get("selected_entry_capability")
    result = entry_result_contract(task.gateway)
    expected = result.get('evidence_kinds', [])
    if expected and not requirements:
        requirements.append(EvidenceRequirement(id="entry:requested-result",
            description="Complete the requested work: " + task.objective,
            capability=selected, message_type=task.gateway.get("selected_message_type") or "task.submitted",
            evidence_kinds=expected, data_kinds=result.get('data_kinds', []), input_bindings=task.inputs,
            method_id=None if provider_selected else task.gateway.get('selected_method_id'), stage=result.get('stage', 'work'),
            execution_requirements=task.gateway.get("execution_requirements") or {}, source="entry_contract"))
    # A claim must be assessed, but an honest negative/insufficient assessment
    # is legal unless a separate necessary evidence obligation says otherwise.
    from bioma.core.acceptance import acceptance_mode, bind_protocol
    for claim in claims if acceptance_mode(task) == 'research' else []:
        strategy_result = task.gateway.get("selected_strategy_result_contract") or {}
        qualified_kinds = list(strategy_result.get("evidence_kinds") or [])
        claim_requirement_id = "claim:" + claim["id"]
        requirements.append(EvidenceRequirement(id=claim_requirement_id,
            description=claim["text"], claim_ids=[claim["id"]],
            mode="evidence" if qualified_kinds else "claim_assessment",
            evidence_kinds=qualified_kinds,
            allow_insufficient=not qualified_kinds, repair_enabled=False,
            source="query_claim"))
        if qualified_kinds:
            roles = dict(task.gateway.get("obligation_roles") or {})
            roles[claim_requirement_id] = "QUESTION"
            task.gateway["obligation_roles"] = roles
    from bioma.core.research_planning import split_interpretation
    contract, plan = split_interpretation(task, requirements, claims)
    return bind_protocol(task.model_copy(update={"gateway": {**task.gateway,
        "research_contract": contract.model_dump(mode="json"),
        "research_plan": plan.model_dump(mode="json"),
        "research_plan_ref": list(plan.version_identity)}}))


def review_request(state, source_message, *, requester):
    from bioma.core.models import TaskRequest
    contract = contract_for(state)
    production = contract.schema_version == 'research-contract-v2'
    snapshot = review_snapshot(state) if production else evidence_snapshot(state)
    return TaskRequest(id=str(uuid5(NAMESPACE_URL, state.task_id + ":review:" + contract.identity + ":" + snapshot + ':' + str(state.version))),
        requester=requester, required_capability='scientific.evidence_assessment' if production else 'scientific.evidence_review',
        objective=state.objective, message_type='task.submitted' if production else 'research.review.requested',
        payload={"review_scope": contract.identity, "snapshot_requested": snapshot,
                 **({'method_id':'assess_evidence'} if production else {})},
        correlation_id=source_message.correlation_id or source_message.id, causation_id=source_message.id)


def qualified_review_assessment(state, evidence, *, check_files=True):
    """Accept only a runtime-qualified proposal with the exact committed inputs.

    The runtime qualifier proves the WP1 journal binding before this record is
    committed. This check additionally prevents a role name, a verdict flag, or
    an edited proposal from supplying completion authority.
    """
    from bioma.core.scientific_capabilities import WorkResult, ASSESSMENT
    value = evidence.value if isinstance(evidence.value, dict) else {}
    observation = next((o for o in state.observations
        if o.id == value.get('work_result_ref') and o.kind == 'work_result'), None)
    if (evidence.kind != 'research_assessment' or evidence.source != 'harness'
            or observation is None or observation.source != 'evidence-assessment'):
        raise ValueError('Review lacks a qualified professional WorkResult')
    work = WorkResult.model_validate(observation.value)
    if (work.family != ASSESSMENT or work.kind != 'EpistemicQualification'
            or work.provenance.get('provider_id') != observation.source
            or work.provenance.get('method_id') != observation.method
            or work.payload.get('assessment') != value.get('assessment')):
        raise ValueError('Review differs from its executed assessment')
    key = fingerprint({'task':state.task_id, 'result':observation.id,
        'model':value.get('qualification_model'), 'work':work.model_dump(mode='json')})
    if (evidence.id != 'qualified.' + key or evidence.method != value.get('qualification_model')
            or not value.get('execution_result_identity')
            or value.get('reviewed_state_identity') != work.payload.get('reviewed_state_identity')
            or value.get('reviewed_state_version') != work.payload.get('reviewed_state_version')):
        raise ValueError('Review qualification identity is not bound to executed work')
    independent = value.get('independent_review') or {}
    if (independent.get('model_id') != evidence.method
            or independent.get('passed') is not True or independent.get('issues') != []):
        raise ValueError('Review lacks an independent qualification record')
    assessment = validate_assessment(state, evidence, check_files=check_files)
    if not {i for row in assessment.requirements for i in row.evidence_ids} <= set(evidence.parent_ids):
        raise ValueError('Review omitted its supporting evidence dependencies')
    return assessment


def completion_assessment(state, *, check_files=True):
    contract = contract_for(state)
    if contract is None:
        return None
    for evidence in reversed(state.evidence):
        if evidence.kind != 'research_assessment':
            continue
        if contract.schema_version == 'research-contract-v2':
            try:
                return qualified_review_assessment(state, evidence, check_files=check_files)
            except (ValueError, TypeError):
                continue  # preserved historical review, no current completion credit
        elif evidence.source == 'comagent':
            assessment = ResearchAssessment.model_validate(evidence.value)
            if assessment.contract_id == contract.identity and assessment.snapshot_hash == evidence_snapshot(state):
                return validate_assessment(state, evidence, check_files=check_files)
    return None


def validate_assessment(state, evidence, *, check_files=True):
    contract = contract_for(state)
    value = evidence.value if isinstance(evidence.value, dict) else {}
    qualified = 'assessment' in value
    assessment = ResearchAssessment.model_validate(value['assessment'] if qualified else value)
    if contract is None or assessment.contract_id != contract.identity or assessment.contract_version != contract.version:
        raise ValueError("Research assessment contract/version mismatch")
    snapshot = review_snapshot(state, check_files=check_files) if qualified else evidence_snapshot(state)
    if assessment.snapshot_hash != snapshot:
        raise ValueError("Stale research assessment")
    rows = {r.requirement_id: r for r in assessment.requirements}
    from bioma.core.acceptance import review_requirements, protocol_for
    if protocol_for(state) and protocol_for(state).mode != 'research':
        raise ValueError('Work acceptance does not authorize a Com research assessment')
    requirements = review_requirements(state)
    if len(rows) != len(assessment.requirements) or set(rows) != {r.id for r in requirements}:
        raise ValueError("Research assessment must cover every requirement exactly once")
    if qualified:
        expected_claims = {c for r in requirements if r.mode == 'claim_assessment' for c in r.claim_ids}
        judgments = (assessment.claim_synthesis or {}).get('claim_assessments', [])
        identifiers = [r.get('claim_id') for r in judgments]
        if (not expected_claims <= set(identifiers) or len(identifiers) != len(set(identifiers))
                or set(identifiers) - {c['id'] for c in contract.claims}):
            raise ValueError('Full review must assess every contracted claim explicitly')
    known = {e.id for e in state.evidence}
    artifacts = {a.id for a in state.artifacts}
    for row in rows.values():
        if set(row.evidence_ids) - known or set(row.artifact_ids) - artifacts:
            raise ValueError("Research assessment references unknown evidence/artifacts")
    from bioma.core.evidence_acceptance import structural_assessment
    for requirement in requirements:
        row = rows[requirement.id]
        if row.status != 'satisfied':
            continue
        if any(rows[d].status != 'satisfied' for d in requirement.depends_on):
            raise ValueError('Satisfied requirement has unmet dependencies')
        if requirement.mode != 'claim_assessment' and check_files:
            actual = structural_assessment(state, requirement)
            if actual.status != 'satisfied' or len(row.evidence_ids) < requirement.min_count or not set(row.evidence_ids) <= set(actual.evidence_ids):
                raise ValueError('Research assessment cannot bypass structural evidence acceptance')
        elif requirement.mode == 'claim_assessment' and row.claim_status not in {'supported', 'contradicted', 'mixed', 'insufficient'}:
            raise ValueError('Satisfied claim obligation requires a scientific judgement')
        elif row.claim_status == 'insufficient' and not requirement.allow_insufficient:
            raise ValueError('This claim obligation requires a resolved judgement')
        if requirement.mode == 'claim_assessment' and row.claim_status != 'insufficient':
            from bioma.core.prediction_checks import validate_prediction_claim_scope
            claims = [c for c in contract.claims if c['id'] in requirement.claim_ids]
            for cited in state.evidence:
                if cited.id in row.evidence_ids:
                    validate_prediction_claim_scope(claims, cited)
        if (requirement.mode == 'claim_assessment' and row.claim_status != 'insufficient'
                and not row.evidence_ids):
            raise ValueError('Resolved scientific judgement requires supporting evidence')
    complete = all(rows[r.id].status == "satisfied" for r in requirements if r.mandatory)
    if assessment.complete != complete:
        raise ValueError("Research assessment completion disagrees with mandatory nodes")
    return assessment


# Public new-run model; old records use ResearchContractV1 through contract_for.
from bioma.core.research_contract import ResearchContract
