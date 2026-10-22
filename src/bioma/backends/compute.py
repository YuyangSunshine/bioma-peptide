"""Shared ownership for calculation, modeling, candidate design and data analysis."""
from bioma.backends.base import MethodBinding, ScientificBackend
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import COMPUTE


def compute_backend(bindings, *, scoped_skill_resolver=None):
    return ScientificBackend(COMPUTE, 'scientific-compute', bindings,
                             scoped_skill_resolver=scoped_skill_resolver)


def build_compute_backend(*, config, base_dir, tool_registry, skill_registry,
                  provider_factory, observation_runner, observation_trust=None,
                  method_tools=None, additional_backends=(), compute_config=None):
    """Bind professional implementations, never discover methods through roles."""
    compute_config = compute_config or config.resolved_compute_services()
    additional_backends = tuple(additional_backends)
    from importlib.util import find_spec
    from bioma.backends import compute_methods
    from bioma.backends.investigation import investigation_backend
    from bioma.capabilities.scoped import ScopedSkillResolver
    from bioma.backends.tabular_analysis import generic_tabular_analysis
    methods = (
        (compute_methods.sequence_descriptors(available=find_spec('Bio') is not None),
         _sequence_descriptors(base_dir)),
        (compute_methods.candidate_substitution_design(), _candidate_design(
            provider_factory(compute_config.proposal_llm), base_dir)),
        (compute_methods.computational_hypothesis(), _hypothesis(
            provider_factory(compute_config.proposal_llm))),
        (compute_methods.committed_observation_analysis(), _observation_analysis(
            observation_runner, observation_trust)),
    )
    grants = method_tools or {}
    bindings = []
    for declaration, execute in methods:
        tools = tuple(grants.get(declaration.id, ()))
        if any(t.external_effect or t.result_kind == 'retrieved_fact' for t in tools):
            raise ValueError('Compute methods cannot expose external actions or retrieval')
        bindings.append(MethodBinding(declaration, execute, tools))
    if not config.continuous_investigation:
        raise ValueError('Legacy compute loops are retired; enable continuous_investigation')
    tools = tuple(grants.get('continuous_scientific_investigation',
        grants.get('configured_computational_tools', tuple(t for t in tool_registry.all()
            if not t.external_effect and t.result_kind in {'calculation', 'computed_prediction', 'retrieved_fact'}
            and t.result_validator is not None))))
    if any(t.external_effect for t in tools):
        raise ValueError('Investigation cannot expand external effect authority')
    declaration = compute_methods.continuous_scientific_investigation()
    bindings.append(MethodBinding(declaration, investigation_backend(
        compute_config.worker, base_dir,
        provider_factory=provider_factory, skill_registry=skill_registry,
        reference_models=compute_config.reference_models), tools))
    bindings.extend(additional_backends)
    bindings.append(MethodBinding(method(COMPUTE, 'verified_tabular_analysis', 'AnalysisResult').model_copy(update={
        'required_input_kinds':['work_result','user_supplied_input'], 'required_input_qualification':'independently_validated',
        'required_artifact_suffixes':['.csv'], 'min_input_count':1, 'max_input_count':1,
        'execution_environment':['python']}), generic_tabular_analysis(base_dir)))
    bindings.append(MethodBinding(method(COMPUTE, 'candidate_bundle_analysis', 'Calculation').model_copy(update={
        'required_input_kinds':['CandidateDesign'], 'execution_environment':['python_module:Bio']}),
        _candidate_bundle_analysis(base_dir)))
    from bioma.tools.registry import ToolRegistry
    registered = ToolRegistry([])
    for tool in tool_registry.all():
        registered.add(tool)
    for backend in additional_backends:
        for tool in backend.tools:
            registered.add(tool)
    return compute_backend(bindings, scoped_skill_resolver=ScopedSkillResolver(skill_registry, registered))


def _candidate_bundle_analysis(base_dir):
    """Calculate and plot properties for a verified candidate-design result."""
    def execute(state, message, tools):
        del tools
        import csv
        import hashlib
        import json
        from pathlib import Path
        from Bio import __version__ as biopython_version
        from Bio.SeqUtils.ProtParam import ProteinAnalysis
        from bioma.core.models import Artifact
        from bioma.core.scientific_capabilities import WorkResult

        refs = set(message.payload.get('input_refs', []))
        source = next((row for row in state.observations
                       if row.id in refs and WorkResult.model_validate(row.value).kind == 'CandidateDesign'), None)
        if source is None:
            raise ValueError('Candidate analysis requires a referenced CandidateDesign WorkResult')
        design = WorkResult.model_validate(source.value)
        candidates = design.payload.get('candidate_batch')
        # Read-only replay of previously committed packages; new backends return raw results.
        for candidate in design.payload.get('candidate_results', []):
            value = candidate.get('value') if isinstance(candidate, dict) else None
            if candidates is None and isinstance(value, dict) and value.get('candidates') and value.get('parent_sequence'):
                candidates = value
                break
        if candidates is None:
            raise ValueError('CandidateDesign result contains no validated candidate batch')
        rows = []
        for item in [{'id': 'parent', 'sequence': candidates['parent_sequence'], 'mutations': []},
                     *candidates['candidates']]:
            sequence = item['sequence']
            analysis = ProteinAnalysis(sequence)
            rows.append({'id': item['id'], 'mutations': item.get('mutations', []),
                'sequence_sha256': hashlib.sha256(sequence.encode()).hexdigest(),
                'length': len(sequence), 'molecular_weight_da': analysis.molecular_weight(),
                'theoretical_pI': analysis.isoelectric_point(), 'gravy': analysis.gravy(),
                'aromaticity': analysis.aromaticity()})
        from bioma.core.research import fingerprint
        input_version = fingerprint({'work': design.model_dump(mode='json'),
                                     'biopython': biopython_version, 'method': 'candidate_bundle_analysis-v1'})
        root = Path(base_dir).resolve() / 'artifacts' / 'derived' / state.task_id / 'candidate-analysis' / input_version
        root.mkdir(parents=True, exist_ok=True)
        json_path, csv_path, plot_path = root / 'analysis_results.json', root / 'comparison.csv', root / 'analysis_plot.png'
        payload = {'data_kind': 'calculation', 'input_work_result_ref': source.id,
            'runtime': {'biopython': biopython_version}, 'rows': rows}
        json_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        with csv_path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['id', 'mutations', 'sequence_sha256',
                'length', 'molecular_weight_da', 'theoretical_pI', 'gravy', 'aromaticity'])
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, 'mutations': ';'.join(row['mutations'])})
        import matplotlib.pyplot as plt
        figure, axis = plt.subplots(figsize=(6, 4))
        axis.bar([row['id'] for row in rows], [row['gravy'] for row in rows])
        axis.set_ylabel('GRAVY'); axis.set_title('Calculated sequence hydropathy')
        figure.tight_layout(); figure.savefig(plot_path, dpi=150); plt.close(figure)
        artifacts = [Artifact(kind=kind, uri=str(path), source='scientific-compute')
            for kind, path in (('candidate-calculations-json', json_path),
                               ('candidate-comparison-csv', csv_path),
                               ('candidate-comparison-plot', plot_path))]
        return WorkResult(family=COMPUTE, kind='Calculation', payload=payload,
            input_refs=[source.id], artifacts=[row.model_dump(mode='json') for row in artifacts],
            provenance={'method': 'candidate_bundle_analysis', 'input_work_result_ref': source.id,
                        'runtime': {'biopython': biopython_version}})
    return execute


def _sequence_descriptors(base_dir):
    """Direct numerical method; no PredAgent or sequence_backend dispatch."""
    def execute(state, message, tools):
        del tools
        from bioma.runtime.admission import current_execution_permit, check_execution
        from bioma.core.source_inputs import verify_source_identities
        from bioma.backends.sequence import calculate_sequence_descriptors
        from bioma.backends.base import upstream_work_result_identities
        current_execution_permit()
        check_execution()
        inputs = message.payload.get('inputs', state.inputs)
        sources = verify_source_identities(inputs.get('source_inputs', []))
        upstream = upstream_work_result_identities(state, message.payload.get('input_refs', []))
        local = state.model_copy(update={'inputs': inputs})
        result = calculate_sequence_descriptors(base_dir, local, message)
        if result is None:
            raise ValueError('Sequence descriptors require an explicit sequence calculation input')
        return result.model_copy(update={
            'input_refs': [*[row.id for row in sources], *[row['observation_id'] for row in upstream]],
            'provenance': {**result.provenance, 'backend': 'calculate_sequence_descriptors',
                          'source_message_id': message.id,
                          'source_inputs': [row.model_dump(mode='json') for row in sources],
                          'upstream_work_results': upstream}})

    return execute


def _candidate_design(provider, base_dir):
    def execute(state, message, tools):
        del tools
        from bioma.runtime.admission import current_execution_permit, check_execution
        from bioma.core.source_inputs import verify_source_identities
        from bioma.capabilities.deliveries import method_deliveries
        from bioma.backends.prediction.candidate_design import design_candidates
        from bioma.backends.base import upstream_work_result_identities
        current_execution_permit(); check_execution()
        inputs = message.payload.get('inputs', {})
        sources = verify_source_identities(inputs.get('source_inputs', []))
        upstream = upstream_work_result_identities(state, message.payload.get('input_refs', []))
        contracts = message.payload.get('delivery_contracts') or [c for c in method_deliveries(
            state.objective, inputs, (), method_id='candidate_substitution_design') if c.get('candidate_spec')]
        if not any(c.get('candidate_spec') for c in contracts):
            raise ValueError('Candidate design requires a validated sequence and candidate contract')
        request = message.model_copy(update={'payload':{**message.payload, 'delivery_contracts':contracts}})
        result = design_candidates(provider, base_dir, state, request, source='scientific-compute')
        return result.model_copy(update={
            'input_refs': [*[s.id for s in sources], *[r['observation_id'] for r in upstream]],
            'provenance': {**result.provenance, 'backend': 'design_candidates',
                'source_message_id': message.id,
                'source_inputs': [s.model_dump(mode='json') for s in sources],
                'upstream_work_results': upstream}})

    return execute


def _hypothesis(provider):
    def execute(state, message, tools):
        del tools
        import json
        from bioma.runtime.admission import current_execution_permit, check_execution
        from bioma.runtime.deadline import bounded_call
        from bioma.core.source_inputs import verify_source_identities
        from bioma.backends.base import upstream_work_result_identities
        from bioma.core.scientific_capabilities import WorkResult, HypothesisProposal
        current_execution_permit(); check_execution()
        inputs = message.payload.get('inputs', {})
        sources = verify_source_identities(inputs.get('source_inputs', []))
        upstream = upstream_work_result_identities(state, message.payload.get('input_refs', []))
        refs = [*[s.id for s in sources], *[r['observation_id'] for r in upstream]]
        messages = [{'role':'system', 'content':
            'Formulate an explicitly untested scientific hypothesis or modeling plan. '
            'No tools have executed in this method. Do not claim computed or experimental results.'},
            {'role':'user', 'content':json.dumps({'objective':message.payload.get('objective') or state.objective,
                'inputs':inputs, 'upstream_work_results':[r.model_dump(mode='json') for r in state.observations if r.id in refs]}, default=str)}]
        answer = bounded_call(lambda: provider.chat(messages))
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('Empty computational hypothesis')
        return WorkResult(family=COMPUTE, kind='ComputationalHypothesisProposal',
            payload={'statement':answer, 'executed_computation':False}, input_refs=refs,
            proposals=[HypothesisProposal(statement=answer, origin=COMPUTE, source_refs=refs,
                limitations=['Untested computational proposal; no prediction or measurement.'])],
            provenance={'backend':type(provider).__name__, 'method':'computational_hypothesis',
                        'source_inputs':[s.model_dump(mode='json') for s in sources], 'upstream_work_results':upstream})
    return execute


def _observation_analysis(runner, trust):
    def execute(state, message, tools):
        del tools
        from bioma.runtime.admission import current_execution_permit, check_execution
        from bioma.core.scientific_data import observation_data_kind
        from bioma.core.models import Artifact
        from bioma.backends.analysis.observations import ObservationAnalysisError
        from bioma.backends.base import BackendIncomplete
        from bioma.core.scientific_capabilities import WorkResult
        current_execution_permit(); check_execution()
        observation_id = message.payload.get('observation_id')
        observation = next((o for o in state.observations if o.id == observation_id), None)
        parent_id = message.payload.get('execution_evidence_id')
        parent = next((e for e in state.evidence if e.id == parent_id), None)
        if observation is None or observation.source not in {'operagent', 'experiment-execution'}:
            raise ValueError('Observation analysis requires a committed execution observation')
        if parent is None or not isinstance(parent.value, dict) or parent.value.get('observation_id') != observation.id:
            raise ValueError('A matching execution evidence record is required')
        kind = observation_data_kind(observation, artifacts=state.artifacts, trust=trust, run_id=state.task_id)
        if observation.kind == 'experimental_observation' and kind != 'experimental_observation':
            raise ValueError('Observation analysis requires authenticated measurement admission')
        try:
            result = runner.run(state.task_id, observation, state.success_criteria)
        except ObservationAnalysisError as exc:
            raise BackendIncomplete('blocked', {'code':'analysis_precondition_check',
                'message':str(exc), 'missing_groups':getattr(exc, 'groups', [])}) from exc
        artifact = Artifact(kind='analysis_json', uri=str(result.artifact_path), source='scientific-compute',
            metadata={'stages':list(result.stages), 'observation_id':observation.id})
        return WorkResult(family=COMPUTE, kind='AnalysisResult',
            payload={'analysis':result.analysis.model_dump(mode='json'), 'data_kind':kind,
                     'limitations':['Threshold assessment is scoped to this observation; simulation is not experimental confirmation.']},
            input_refs=[observation.id, parent.id], artifacts=[artifact.model_dump(mode='json')],
            provenance={'backend':type(runner).__name__, 'method':'committed_observation_analysis'})
    return execute
