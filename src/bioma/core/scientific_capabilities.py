"""Non-authoritative provider outputs. Admission and epistemic commit stay in runtime."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from bioma.core.community import ExecutionRequirements, merge_execution_requirements
from bioma.core.models import StrictModel

RESEARCH = 'scientific.research'
COMPUTE = 'scientific.compute'
STUDY = 'scientific.study_design'
EXECUTION = 'scientific.execution'
ASSESSMENT = 'scientific.evidence_assessment'
FAMILIES = (RESEARCH, COMPUTE, STUDY, EXECUTION, ASSESSMENT)
RESULT_OWNERS = {
    # Ownership of the execution envelope, not a scientific classification.
    'InvestigationResult': COMPUTE,
    **dict.fromkeys(('ResearchBundle', 'RetrievedFact', 'SourceSet', 'LiteratureGroundedProposal'), RESEARCH),
    **dict.fromkeys(('ComputeResult', 'AnalysisResult', 'Calculation', 'Prediction', 'ComputedPrediction', 'CandidateDesign', 'Visualization', 'ComputationalHypothesisProposal',
                     'StructurePrediction', 'StabilityPrediction', 'SolubilityPrediction',
                     'MutationEffectPrediction', 'SequenceDesignPrediction',
                     'BindingPrediction'), COMPUTE),
    **dict.fromkeys(('StudyPlan', 'ExperimentalDesign', 'ControlPlan', 'ProtocolSpec', 'MeasurementPlan', 'EndpointDefinition', 'AcceptanceCriteria'), STUDY),
    **dict.fromkeys(('ExecutionReceipt', 'RawObservation', 'ExperimentalObservation', 'ExecutionFailure', 'EffectUncertain'), EXECUTION),
    **dict.fromkeys(('EvidenceAssessment', 'EvidenceGap', 'ClaimAssessment', 'EpistemicQualification'), ASSESSMENT),
}
LEGACY_CAPABILITY_ALIASES = {
    'scientific.computational_research': COMPUTE,
    'scientific.data_analysis': COMPUTE,
    'research.study_design': STUDY,
    'execution.protocol_execution': EXECUTION,
    'scientific.evidence_review': ASSESSMENT,
}
LEGACY_AGENT_ALIASES = {
    'queryagent': 'runtime.QueryContractCompiler',
    'predagent': (RESEARCH, COMPUTE),
    'comagent': (COMPUTE, ASSESSMENT, 'runtime.ReportRenderer'),
    'proagent': (STUDY,), 'operagent': (EXECUTION,),
}


def canonical_family(family: str, method: str | None = None) -> str:
    # Result semantics, not the protein domain, determine this exception.
    if method == 'exact_sequence_structure_lookup':
        return RESEARCH
    return LEGACY_CAPABILITY_ALIASES.get(family, family)


class CapabilityRequirement(StrictModel):
    capability_family: Literal['scientific.research', 'scientific.compute', 'scientific.study_design', 'scientific.execution', 'scientific.evidence_assessment']
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    # Compatibility spelling for compiler-produced method/message restrictions.
    constraints: dict[str, Any] = Field(default_factory=dict)
    execution_requirements: ExecutionRequirements = Field(default_factory=ExecutionRequirements)
    result_requirements: dict[str, list[str]] = Field(default_factory=dict)
    required_resources: list[str] = Field(default_factory=list)
    budget: float | None = Field(default=None, ge=0)
    deadline: datetime | None = None
    risk_level: Literal['low', 'medium', 'high'] = 'low'

    @model_validator(mode='after')
    def provider_agnostic(self):
        allowed = {'evidence_kinds', 'data_kinds', 'artifact_kinds', 'artifact_names'}
        if set(self.result_requirements) - allowed:
            raise ValueError('Unsupported result requirement')
        if any(RESULT_OWNERS.get(kind) != self.capability_family
               for kind in self.result_requirements.get('evidence_kinds', [])):
            raise ValueError('Required result kind belongs to another family')
        self.effective_execution_requirements()
        return self

    def effective_execution_requirements(self) -> ExecutionRequirements:
        constraints = dict(self.constraints)
        message_type = constraints.pop('message_type', 'task.submitted')
        if not isinstance(message_type, str) or not message_type:
            raise ValueError('message_type must be a nonempty string')
        # Strict validation rejects recipient/provider selectors and conflicting
        # restrictions. A family requirement never grants implementation authority.
        return ExecutionRequirements.model_validate(merge_execution_requirements(
            constraints, self.execution_requirements.model_dump(mode='json'),
            {'responsibility': self.capability_family}))


class EvidenceGap(StrictModel):
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    missing: list[str] = Field(min_length=1)
    authority: Literal['proposal'] = 'proposal'


class HypothesisProposal(StrictModel):
    statement: str = Field(min_length=1)
    origin: Literal['scientific.research', 'scientific.compute', 'scientific_controller']
    source_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    authority: Literal['proposal'] = 'proposal'


class Source(StrictModel):
    id: str = Field(min_length=1)
    identifier: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    spans: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(min_length=1)


class RetrievedClaim(StrictModel):
    statement: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    span_ids: list[str] = Field(min_length=1)


class ResearchBundle(StrictModel):
    sources: list[Source] = Field(default_factory=list)
    retrieved_claims: list[RetrievedClaim] = Field(default_factory=list)
    retrieval_trace: list[dict[str, Any]] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    hypotheses: list[HypothesisProposal] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(min_length=1)
    authority: Literal['unqualified'] = 'unqualified'

    @model_validator(mode='after')
    def grounded_statements(self):
        sources = {source.id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError('Duplicate source identifier')
        for claim in self.retrieved_claims:
            source = sources.get(claim.source_id)
            if source is None or not set(claim.span_ids) <= set(source.spans):
                raise ValueError('Every retrieved statement requires resolvable source spans')
        for hypothesis in self.hypotheses:
            if hypothesis.origin != RESEARCH or not set(hypothesis.source_refs) <= set(sources):
                raise ValueError('Research hypothesis provenance does not resolve')
        return self


class EvidenceAssessment(StrictModel):
    claim: str = Field(min_length=1)
    relation: Literal['SUPPORTS', 'OPPOSES', 'CONTEXT', 'INSUFFICIENT', 'CONFLICTED']
    input_refs: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    gaps: list[EvidenceGap] = Field(default_factory=list)
    authority: Literal['proposal'] = 'proposal'


class WorkResult(StrictModel):
    family: str
    kind: str
    payload: dict[str, Any]
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[HypothesisProposal] = Field(default_factory=list)
    requirements: list[CapabilityRequirement] = Field(default_factory=list)
    input_refs: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(min_length=1)
    authority: Literal['unqualified'] = 'unqualified'

    @model_validator(mode='after')
    def unique_owner(self):
        if RESULT_OWNERS.get(self.kind) != self.family:
            raise ValueError('Result kind has a different scientific owner')
        if self.kind == 'InvestigationResult':
            basis = self.payload.get('result_basis')
            if (not isinstance(basis, list) or not basis
                    or any(not isinstance(row, dict) or row.get('acquisition') not in {'computation', 'retrieval', 'professional_method'}
                           or not row.get('receipt_ref') or not row.get('receipt_sha256')
                           or row.get('scientific_support') != 'not_assessed' for row in basis)):
                raise ValueError('InvestigationResult requires selected, unqualified native origins')
        if any(proposal.origin != self.family for proposal in self.proposals):
            raise ValueError('Hypothesis proposal must retain its originating family')
        return self


def has_computational_result(work):
    """Eligibility only; domain checkers must still verify all native records."""
    return work.kind == 'AnalysisResult' or (
        work.kind == 'InvestigationResult' and any(
            row.get('acquisition') == 'computation' for row in work.payload.get('result_basis', [])))
