"""Output recipes attached to concrete methods, separate from generic DAG rules."""
import hashlib

from bioma.capabilities.definitions import COMPUTATION, ANALYSIS, REVIEW
from bioma.core.deliverables import digest, sequences
from bioma.core.reporting import needs_report


def method_deliveries(objective, inputs, desired, *, method_id=None, result_contract=None):
    rows = []
    def add(name, responsibility, method, message, evidence, artifacts, bound, *, parents=(), identity=None, **extra):
        key = identity or digest(bound)
        row = dict(id=name + ':' + digest([key, extra]), schema_version='delivery-v2',
                   input_hash=key, label=name.replace('_', ' '), capability=responsibility,
                   method_id=method, message_type=message, evidence=evidence, artifacts=artifacts,
                   input_bindings=bound, input_scope='task', depends_on=list(parents),
                   stage='work', **extra)
        rows.append(row)
        return row

    candidate = inputs.get('candidate_design')
    methods = set()
    if candidate:
        methods.add('candidate_substitution_design')
    elif method_id == 'sequence_descriptors' or 'sequence_descriptors' in desired:
        methods.add('sequence_descriptors')
    if method_id == 'exact_sequence_structure_lookup' or 'protein_structure' in desired:
        methods.add('exact_sequence_structure_lookup')
    for sequence in sequences(inputs) if methods else []:
        key = hashlib.sha256(sequence.encode()).hexdigest()
        for method in sorted(methods):
            extra = {'input_identity': 'sequence_sha256'}
            if method == 'candidate_substitution_design':
                from bioma.core.candidates import CandidateDesignSpec
                extra['candidate_spec'] = CandidateDesignSpec.model_validate(candidate).model_dump()
                artifacts = ['candidate-design-json', 'candidate-sequences-fasta']
                evidence, message = 'prediction', 'task.submitted'
            elif method == 'exact_sequence_structure_lookup':
                artifacts = ['protein-structure-pdb', 'structure-index-json']
                evidence, message = 'structure_resolution', 'structure.requested'
            else:
                artifacts = ['sequence-properties-json', 'amino-acid-composition-csv']
                if 'fasta' in objective.casefold():
                    artifacts.append('sequence-fasta')
                evidence, message = 'prediction', 'task.submitted'
            add(method, COMPUTATION, method, message, evidence, artifacts,
                {'sequence': sequence}, identity=key, **extra)

    report = needs_report(objective, desired)
    graph = 'evidence_graph' in desired or 'evidence graph' in objective.casefold() or '证据图' in objective
    # Data-analysis methods own their own requested files/reports. Additional
    # package delivery is useful only when consuming independently committed work.
    if not rows and result_contract and (report or graph) and result_contract.get('stage', 'work') == 'work':
        evidence = result_contract.get('evidence_kinds', [])
        if evidence:
            add('committed_work', result_contract['responsibility'], method_id, 'task.submitted',
                evidence[0], [], inputs)
    producers = [r['id'] for r in rows]
    package_key = digest([r['input_hash'] for r in rows])
    if any(r['method_id'] == 'sequence_descriptors' for r in rows) and (
            'composition_figure' in desired or 'chart' in objective.casefold() or '图' in objective):
        row = add('composition_chart', ANALYSIS, 'committed_artifact_analysis',
            'artifact.analysis.requested', 'scientific_visualization', ['sequence-composition-chart'],
            {}, parents=producers, identity=package_key)
        row['stage'] = 'delivery'
    if report or graph:
        standalone = not rows
        method = 'scientific_claim_synthesis' if standalone and not report else 'committed_artifact_analysis'
        responsibility = REVIEW if method == 'scientific_claim_synthesis' else ANALYSIS
        artifacts = (['evidence-graph-json'] if graph or not report else []) + (['evidence-report-markdown'] if report else [])
        row = add('evidence_report' if report else 'evidence_graph', responsibility, method,
            'evidence.synthesis.requested' if standalone and not report else 'artifact.analysis.requested',
            'artifact_analysis' if report and not graph else 'evidence_graph_synthesis', artifacts, {}, parents=[r['id'] for r in rows], identity=package_key)
        row['stage'] = 'delivery' if not standalone else 'work'
    return rows
