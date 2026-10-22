"""Concrete computation and retrieval contracts, bound directly to MethodBinding.

These declarations grant no scientific admission or task completion authority.
"""
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import COMPUTE, RESEARCH

def _inputs(*names):
    return {"type": "object", "required": ["inputs"], "properties": {
        "inputs": {"type": "object", "anyOf": [{"required": [name]} for name in names],
                   "properties": {name: {"type": "string", "minLength": 1} for name in names}}}}


def _references(*names):
    return {"type": "object", "required": list(names),
            "properties": {name: {"type": "string", "minLength": 1} for name in names}}


def _profile(name, inputs, outputs, operations):
    return dict(id=name, modalities=inputs, output_modalities=outputs, operations=operations)


def sequence_descriptors(*, available=True):
    return method(COMPUTE,
        'sequence_descriptors',
        'Calculation',
        description='Compute sequence physicochemical descriptors with Biopython; this method supports amino-acid sequences only.',
        message_type='task.submitted',
        input_schema=_inputs('sequence', 'protein_sequence'),
        profiles=[_profile('amino-acid-descriptors', ['sequence', 'text', 'json'], ['table', 'json', 'text'], ['calculation'])],
        verification=['full_input_identity', 'reproducible_descriptor_calculation', 'artifact_integrity'],
        artifacts=['sequence-properties-json', 'amino-acid-composition-csv'],
        data_kinds=['calculation'],
        available=available,
        reason='Biopython is not installed')


def candidate_substitution_design():
    return method(COMPUTE,
        'candidate_substitution_design',
        'CandidateDesign',
        description='Produce explicit protein candidate substitutions under the supplied count, mutation and alphabet contract; untested candidates remain proposals.',
        message_type='task.submitted',
        input_schema=_inputs('sequence', 'protein_sequence'),
        profiles=[_profile('candidate-proposals', ['sequence', 'json', 'text'], ['sequence', 'table', 'json', 'text'], ['design', 'optimization'])],
        verification=['parent_sequence_identity', 'candidate_specification', 'proposal_not_prediction'],
        limitations=['Tabular output is the candidate/mutation record collection in JSON; this does not promise arbitrary CSV files or plots.'],
        artifacts=['candidate-design-json', 'candidate-sequences-fasta'],
        data_kinds=['hypothesis_proposal'])


def computational_hypothesis():
    return method(COMPUTE,
        'computational_hypothesis',
        'ComputationalHypothesisProposal',
        description='Formulate computational hypotheses or a modeling plan; outputs are explicitly untested proposals, not executed model results.',
        message_type='task.submitted',
        profiles=[_profile('hypothesis-formulation', ['sequence', 'smiles', 'variants', 'omics', 'table', 'json', 'text', 'structure'], ['text', 'json'], ['hypothesis', 'design'])],
        verification=['input_identity', 'proposal_not_prediction', 'declared_limitations'],
        data_kinds=['hypothesis_proposal'])


def committed_observation_analysis():
    return method(COMPUTE,
        'committed_observation_analysis',
        'AnalysisResult',
        description='Check authenticated observation provenance, compare groups and evaluate the requested criteria.',
        message_type='observation.ready',
        input_schema=_references('execution_evidence_id', 'observation_id'),
        profiles=[_profile('observation-comparison', ['json', 'table'], ['json', 'text'], ['analysis', 'statistics'])],
        verification=['observation_authentication', 'group_requirements', 'criterion_evaluation'],
        data_kinds=['analysis_result'])


def exact_sequence_structure_lookup():
    return method(RESEARCH,
        'exact_sequence_structure_lookup',
        'ResearchBundle',
        description='Resolve a complete amino-acid sequence to an exact UniProt match, then retrieve and verify structure coordinates; not a general structure predictor.',
        message_type='structure.requested',
        input_schema=_inputs('sequence', 'protein_sequence'),
        profiles=[_profile('exact-sequence-retrieval', ['sequence', 'text', 'json'], ['structure', 'json', 'text'], ['retrieval'])],
        verification=['full_sequence_exact_match', 'structure_provenance', 'coordinate_integrity'],
        limitations=['UniProt and structure service access must succeed; a predicted structure is not experimental evidence.'],
        artifacts=['protein-structure-pdb', 'structure-index-json'],
        data_kinds=['retrieved_fact'])


def continuous_scientific_investigation():
    declaration = method(COMPUTE, 'continuous_scientific_investigation', 'InvestigationResult',
        description='Execute a bounded scientific investigation work package using granted local tools; retain code, failures, receipts and explicitly selected artifacts. Execution does not establish scientific support.',
        profiles=[_profile('bounded-scientific-computation',
            ['sequence', 'smiles', 'variants', 'omics', 'table', 'json', 'text', 'structure'],
            ['table', 'json', 'text', 'image', 'sequence', 'structure'],
            ['analysis', 'statistics', 'calculation', 'modeling', 'visualization', 'retrieval', 'source_inspection'])],
        verification=['actual_execution', 'artifact_integrity', 'work_package_review'],
        limitations=['Only installed dependencies and granted tools are available.',
                     'A completed work package is not a supported scientific claim.'])
    declaration.result.evidence_kinds = ['InvestigationResult']
    return declaration
