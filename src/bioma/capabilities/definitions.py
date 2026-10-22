"""Responsibility contracts and concrete methods exposed by the five roles.

This is the public method catalog, not a tool-name routing table. Scientific
domains belong to inputs and installed methods; role ownership belongs to the
result being requested. Deployment code supplies actual availability.
"""
from __future__ import annotations

from bioma.core.models import CapabilitySpec

PROBLEM = "research.problem_formulation"
COMPUTATION = "scientific.computational_research"
STUDY = "research.study_design"
EXECUTION = "execution.protocol_execution"
ANALYSIS = "scientific.data_analysis"
REVIEW = "scientific.evidence_review"


def _profile(name, inputs, outputs, operations=(), *, engine=None, **kwargs):
    return dict(id=name, modalities=list(inputs), output_modalities=list(outputs),
                operations=list(operations), engine=engine, **kwargs)


def _method(name, description, message, responsibility, evidence, *, profiles=(),
            inputs=None, tools=(), artifacts=(), optional_artifacts=(), named_extensions=None, data_kinds=(), stage="work",
            verify=("input_identity", "transition_integrity", "artifact_integrity"), **kwargs):
    writes = {PROBLEM: [], COMPUTATION: ['cycle', 'predictions', 'metrics', 'agent_status'],
              STUDY: ['protocol', 'metrics'], EXECUTION: ['protocol', 'machine_code', 'observations', 'metrics'],
              ANALYSIS: ['metrics'], REVIEW: ['metrics']}[responsibility]
    guards = []
    for field in (inputs or {}).get('required', []):
        if field.endswith('_evidence_id'):
            guards.append(dict(id='committed_' + field, source='message', path='payload.' + field,
                               operator='evidence_reference'))
    effects = [dict(id='query_control_recorded' if responsibility == PROBLEM else 'supporting_evidence_returned',
                    source='result', path='control_records' if responsibility == PROBLEM else 'evidence',
                    operator='nonempty')]
    if responsibility == STUDY:
        effects.append(dict(id='design_review_passed', source='after', path='protocol.review.passed', operator='eq', value=True))
    if responsibility == EXECUTION:
        guards.append(dict(id='reviewed_protocol', source='state', path='protocol.review.passed', operator='eq', value=True))
    invariants = [dict(id='preserve_' + field, source='after', path=field, operator='same_as',
                       value={'source':'state', 'path':field}) for field in ('task_id', 'objective')]
    action_contract = dict(preconditions=guards, invariants=invariants, writable_fields=writes, expected_effects=effects)
    return dict(id=name, description=description, message_type=message, action_contract=action_contract,
                entrypoint=message in {"task.submitted", "structure.requested", "evidence.synthesis.requested"},
                input_schema=inputs or {"type": "object"}, profiles=list(profiles),
                required_tools=list(tools), result=dict(responsibility=responsibility,
                    evidence_kinds=list(evidence), artifact_kinds=list(artifacts),
                    optional_artifact_kinds=list(optional_artifacts), named_output_extensions=named_extensions,
                    data_kinds=list(data_kinds), stage=stage,
                    verification_obligations=list(verify)), **kwargs)


def _capability(owner, responsibility, description, methods, **kwargs):
    return CapabilitySpec(id=responsibility, family=responsibility, agent_id=owner,
        description=description, methods=methods,
        accepts_message_types=list(dict.fromkeys(m["message_type"] for m in methods)), **kwargs)


def input_fields(*names):
    return {"type": "object", "required": ["inputs"], "properties": {
        "inputs": {"type": "object", "anyOf": [{"required": [name]} for name in names],
            "properties": {name: ({"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
                          if name == 'files' else {"type": "string", "minLength": 1}) for name in names}}}}


def reference_fields(*names):
    return {'type':'object', 'required':list(names),
            'properties':{name:{'type':'string','minLength':1} for name in names}}


def _catalog_projection(declaration, responsibility, evidence):
    """Legacy message/catalog view; never a second implementation declaration.

    Old node clients still consume Evidence-shaped contracts. Production
    MethodBindings consume the original WorkResult declaration directly.
    """
    row = declaration.model_dump(mode='json')
    row['result'].update(responsibility=responsibility, result_channel='evidence', evidence_kinds=evidence)
    row['action_contract'] = _method(declaration.id, declaration.description,
        declaration.message_type, responsibility, evidence, inputs=declaration.input_schema)['action_contract']
    return row


def sequence_capability(*, owner='scientific-worker', sequence_available=True):
    from bioma.backends.compute_methods import sequence_descriptors, exact_sequence_structure_lookup
    methods = [_catalog_projection(sequence_descriptors(available=sequence_available), COMPUTATION, ['prediction']),
               _catalog_projection(exact_sequence_structure_lookup(), COMPUTATION, ['structure_resolution'])]
    return _capability(owner, COMPUTATION, 'Explicit amino-acid calculations and exact structure retrieval.',
                       methods, action_modes=['tool', 'verification'], priority=80)


def problem_capability():
    return _capability("queryagent", PROBLEM,
        "Maintain the original question, explicit inputs, constraints and acceptance obligations; publish result-oriented work requests without prescribing every peer's scientific method.",
        [_method("interpret_question", "Commit the validated question and evidence requirements.",
                 "query.submitted", PROBLEM, ["query_interpretation"])],
        tags=["coordination"], priority=100, action_modes=["llm", "verification"])


def study_capability():
    profile = _profile("reviewed-study-design", ["text", "json", "sequence", "table", "structure", "omics", "smiles", "variants"],
                       ["text", "json"], ["study_design", "protocol_design", "review"])
    return _capability("proagent", STUDY,
        "Own reproducible study and validation design across domains: comparisons, controls, measurements, uncertainty and acceptance criteria. A reviewed plan does not establish execution or scientific truth.",
        [_method(name, description, message, STUDY, ["protocol"], profiles=[profile], data_kinds=['study_design'],
                 inputs=(reference_fields('prediction_evidence_id') if message == 'prediction.ready' else
                         reference_fields('analysis_evidence_id') if message == 'experiment_design.requested' else None),
                 verify=["design_review", "controls_and_measurements", "protocol_content_identity"])
         for name, description, message in [
             ("draft_study", "Design and review a study from a research question.", "task.submitted"),
             ("design_from_prediction", "Design a validation study from committed computational results.", "prediction.ready"),
             ("revise_study", "Repair the study from committed missing-evidence feedback.", "experiment_design.requested")]],
        priority=80, action_modes=["llm", "verification"])


def execution_capability(*, allow_simulation):
    return _capability("operagent", EXECUTION,
        "Own configured execution resources, compiled procedures and authenticated job/observation records. Compilation, simulation and physical execution are distinct outcomes.", [
        _method("compile_reviewed_protocol", "Compile and validate a committed reviewed protocol; do not claim an experiment ran.",
                "protocol.compile.requested", EXECUTION, ["protocol_compilation"],
                inputs=reference_fields('protocol_evidence_id'),
                profiles=[_profile("protocol-compilation", ["json", "text"], ["json", "text"], ["compilation"])],
                verify=["reviewed_protocol_identity", "machine_code_validation", "no_execution_claim"]),
        _method("simulate_reviewed_protocol", "Execute a reviewed protocol graph on journaled virtual devices and replay explicitly supplied data; return node receipts and simulation artifacts.",
            "protocol.ready", EXECUTION, ["execution"],
                data_kinds=['simulation'],
                artifacts=['machine-code-json', 'simulation-observation-json'],
                inputs=reference_fields('protocol_evidence_id'),
                profiles=[_profile("explicit-simulation", ["json", "text"], ["json"], ["simulation"])],
                available=allow_simulation,
                unavailable_reason="" if allow_simulation else "No authenticated instrument adapter is configured; simulation is disabled.",
                verify=["reviewed_protocol_identity", "execution_receipt", "simulation_provenance"],
                limitations=["Explicit data replay validates orchestration, not biological or instrument physics. Simulation observations cannot establish experimental confirmation."]),
        ], priority=80, action_modes=["llm", "tool", "code", "verification"])
