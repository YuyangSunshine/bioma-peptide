"""Tool-poor epistemic qualification proposals. No search, computation or planning."""
from bioma.backends.base import MethodBinding, ScientificBackend
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import ASSESSMENT, EvidenceAssessment, EvidenceGap, WorkResult


def build_assessment_backend(synthesizer, *, assess_results=None):
    declared = method(ASSESSMENT, 'assess_evidence', 'EvidenceAssessment')
    declared.result.evidence_kinds.append('EpistemicQualification')
    def assess(state, message, tools):
        if message.payload.get('review_scope') is not None:
            from bioma.core.research import contract_for, fingerprint, review_snapshot
            from bioma.backends.analysis.evidence_audit import assess_contract
            contract = contract_for(state)
            if contract is None or message.payload['review_scope'] != contract.identity:
                raise ValueError('Research review requires the exact versioned contract scope')
            assessment = assess_contract(state, synthesizer=synthesizer)
            assessment.snapshot_hash = review_snapshot(state)
            return WorkResult(family=ASSESSMENT, kind='EpistemicQualification',
                payload={'assessment': assessment.model_dump(mode='json'),
                    'reviewed_state_version': state.version,
                    'reviewed_state_identity': fingerprint(state.model_dump(mode='json'))},
                input_refs=[e.id for e in state.evidence] + [a.id for a in state.artifacts],
                provenance={'method': 'full_contract_review', 'scientific_truth_asserted': False})
        inputs = tuple(WorkResult.model_validate(o.value) for o in state.observations if o.kind == 'work_result')
        if inputs and assess_results is not None:
            assessment = EvidenceAssessment.model_validate(assess_results(state, inputs))
            known = {o.id for o in state.observations} | {e.id for e in state.evidence}
            if not set(assessment.input_refs) <= known:
                raise ValueError('Assessment references ungranted material')
            return WorkResult(family=ASSESSMENT, kind='EvidenceAssessment',
                payload=assessment.model_dump(mode='json'), input_refs=assessment.input_refs,
                provenance={'method': 'configured_result_assessment'})
        # The existing synthesizer consumes a read-only state snapshot and has no tool registry.
        synthesis = synthesizer.synthesize_all(state)
        relation = {'supported': 'SUPPORTS', 'contradicted': 'OPPOSES', 'mixed': 'CONFLICTED',
                    'insufficient': 'INSUFFICIENT'}[synthesis.claim_status]
        refs = [item.id for item in state.evidence] + [o.id for o in state.observations if o.kind == 'work_result']
        gaps = [EvidenceGap(objective=state.objective, input_refs=refs, missing=[text])
                for text in synthesis.missing_evidence]
        assessment = EvidenceAssessment(claim=synthesis.primary_claim, relation=relation,
            input_refs=refs, rationale=synthesis.summary, gaps=gaps)
        if inputs and not state.evidence:
            assessment.relation = 'INSUFFICIENT'
            assessment.gaps.append(EvidenceGap(objective=state.objective, input_refs=refs,
                missing=['Unqualified results require independent scientific qualification.']))
        # Recommendations are deliberately not translated into scientific actions.
        return WorkResult(family=ASSESSMENT, kind='EvidenceAssessment',
            payload=assessment.model_dump(mode='json'), input_refs=refs,
            provenance={'method': synthesis.method})

    return ScientificBackend(ASSESSMENT, 'evidence-assessment', [MethodBinding(declared, assess)])
