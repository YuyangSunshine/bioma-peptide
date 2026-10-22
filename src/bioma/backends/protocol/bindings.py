"""Study design produces plans and specifications, never device effects."""
from bioma.backends.base import MethodBinding, ScientificBackend
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import STUDY


def study_backend(bindings, *, scoped_skill_resolver=None):
    return ScientificBackend(STUDY, 'study-design', bindings,
                             scoped_skill_resolver=scoped_skill_resolver)


def build_study_backend( service, *, base_dir):
    from bioma.capabilities.definitions import study_capability
    bindings = [MethodBinding(method(STUDY, original.id, 'StudyPlan', legacy=original),
        _study_method(service, base_dir)) for original in study_capability().methods]
    bindings.append(MethodBinding(method(STUDY, 'analysis_informed_study_design', 'StudyPlan',
        description='Draft a future mutation-score strata validation study from an independently recomputed AnalysisResult.',
        input_schema={'type':'object', 'properties':{
            'analysis_evidence_id':{'type':'string', 'description':'Selected committed AnalysisResult Evidence with independently_recomputed qualification.'}},
            'required':['analysis_evidence_id']},
        limitations=['Requires committed AnalysisResult Evidence with independently_recomputed qualification.']
        ).model_copy(update={'required_input_qualification':'independently_recomputed'}),
        _analysis_informed_study_method(base_dir)))
    return study_backend(bindings)


def _study_method(service, base_dir):
    """Use the protocol service as an implementation, without legacy role authority."""
    def execute(state, message, tools):
        del tools
        from bioma.core.source_inputs import verify_source_identities
        from bioma.core.scientific_capabilities import WorkResult
        inputs = message.payload.get('inputs') or {}
        sources = verify_source_identities(inputs.get('source_inputs', []))
        referenced = [row for row in state.observations
                      if row.id in message.payload.get('input_refs', [])]
        scientific_inputs = {'supplied_inputs': inputs,
            'upstream_work_results': [row.value for row in referenced]}
        previous_protocol = None
        revision_issues = ()
        revision_basis = {}
        if message.payload['method_id'] == 'revise_study':
            from bioma.core.research import fingerprint
            from pathlib import Path
            import hashlib
            import json
            feedback = next((row for row in state.evidence
                if row.id == message.payload.get('analysis_evidence_id')
                and row.kind in {'analysis', 'AnalysisResult'}), None)
            plans = [(row, WorkResult.model_validate(row.value)) for row in referenced
                     if row.kind == 'work_result' and isinstance(row.value, dict)
                     and row.value.get('kind') == 'StudyPlan'
                     and row.value.get('family') == STUDY]
            if feedback is None or not isinstance(feedback.value, dict) or len(plans) != 1:
                raise ValueError('Study revision requires analysis evidence and one explicitly referenced prior StudyPlan')
            observation, prior = plans[0]
            if feedback.value.get('protocol_id') != prior.payload.get('protocol_id'):
                raise ValueError('Study revision feedback does not identify the prior protocol')
            issues = feedback.value.get('revision_issues')
            if not isinstance(issues, list) or not issues or any(not isinstance(i, str) or not i.strip() for i in issues):
                raise ValueError('Study revision requires non-empty evidence-backed revision_issues')
            previous_protocol = prior.payload.get('protocol_text')
            if not isinstance(previous_protocol, str) or not previous_protocol.strip():
                raise ValueError('Study revision requires prior protocol text')
            artifact = next((a for a in prior.artifacts if a.get('kind') == 'study-protocol-json'), None)
            if artifact is None:
                raise ValueError('Study revision requires the prior protocol artifact')
            raw = Path(artifact['uri']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact.get('metadata', {}).get('sha256'):
                raise ValueError('Study revision prior artifact identity changed')
            saved = json.loads(raw)
            if any(saved.get(key) != prior.payload.get(key) for key in ('protocol_id', 'protocol_text', 'review')):
                raise ValueError('Study revision prior artifact differs from committed result')
            revision_issues = tuple(issues)
            revision_basis = {'prior_study_ref': observation.id,
                'prior_work_result_identity': fingerprint(prior.model_dump(mode='json')),
                'analysis_evidence_id': feedback.id,
                'analysis_evidence_identity': fingerprint(feedback.model_dump(mode='json')),
                'prior_protocol_id': prior.payload['protocol_id']}
        outcome = service.design_protocol(state.task_id,
            message.payload.get('objective') or state.objective, scientific_inputs,
            previous_protocol=previous_protocol, revision_issues=revision_issues,
            work_id=message.task_request_id or message.id)
        import json
        from pathlib import Path
        from bioma.core.models import Artifact
        root = Path(base_dir) / 'artifacts' / 'derived' / state.task_id / 'study-design' / (message.task_request_id or message.id)
        root.mkdir(parents=True, exist_ok=True)
        protocol_path = root / 'protocol.json'
        protocol_payload = {'protocol_id': outcome.protocol_id,
            'protocol_text': outcome.protocol_text,
            'review': {'passed': outcome.verification.passed,
                       'issues': list(outcome.verification.issues)}}
        import hashlib
        content = json.dumps(protocol_payload, ensure_ascii=False, sort_keys=True, indent=2)
        if protocol_path.exists() and protocol_path.read_text(encoding='utf-8') != content:
            raise ValueError('Study artifact conflict; reconciliation required')
        protocol_path.write_text(content, encoding='utf-8')
        protocol_artifact = Artifact(kind='study-protocol-json', uri=str(protocol_path),
            source='study-design', metadata={'sha256': hashlib.sha256(protocol_path.read_bytes()).hexdigest()})
        return WorkResult(family=STUDY, kind='StudyPlan', input_refs=[
            *[row.id for row in referenced], *[row.id for row in sources],
            *([revision_basis['analysis_evidence_id']] if revision_basis else [])],
            payload={**protocol_payload,
                'history': list(outcome.history)},
            artifacts=[protocol_artifact.model_dump(mode='json')],
            provenance={'backend': type(service).__name__, 'method': message.payload['method_id'],
                        'source_message_id': message.id,
                        'revision_basis': revision_basis,
                        'source_inputs': [row.model_dump(mode='json') for row in sources]})
    return execute


def _analysis_informed_study_method(base_dir):
    """Draft a bounded future study from qualified analysis; never an executable protocol."""
    def execute(state, message, tools):
        del tools
        import hashlib
        import json
        from pathlib import Path
        from bioma.core.models import Artifact, Evidence
        from bioma.core.scientific_capabilities import WorkResult
        inputs = message.payload.get('inputs') or {}
        refs = set(message.payload.get('input_refs', inputs.get('input_refs', [])))
        supplied = [Evidence.model_validate(row) for row in inputs.get('qualified_evidence_inputs', [])]
        analyses = [row for row in [*state.evidence, *supplied]
                    if row.id in refs and row.kind == 'AnalysisResult'
                    and isinstance(row.value, dict)
                    and row.value.get('qualification_status') == 'independently_recomputed']
        if len(analyses) != 1:
            raise ValueError('Analysis-informed StudyPlan requires exactly one selected qualified AnalysisResult')
        analysis = analyses[0]
        summary = analysis.value['summary']
        plan = {
            'schema_version': 'analysis-informed-study-plan-v1',
            'status': 'future_study_not_executed',
            'scientific_validation_objective': (
                'Experimentally evaluate whether prespecified strata of model mutation scores differ '
                'in measured protein stability and sample quality; retain raw-score labels until '
                'provider documentation establishes score direction.'),
            'comparison_objects': {
                'source_rows': summary['row_count'],
                'covered_positions': (summary.get('position_coverage') or {}).get('unique_positions'),
                'strategy': 'Pre-register strata across the observed raw-score distribution after excluding identity substitutions and failed constructs.',
            },
            'controls': [
                'Wild-type protein processed in every biological batch.',
                'No-protein/process blank for background and contamination checks.',
                'A literature-supported destabilizing or stabilizing reference only after its identity and applicability are independently sourced.',
            ],
            'measurement_endpoints': [
                'Primary: measured thermal midpoint (Tm) from a prespecified thermal unfolding assay.',
                'Orthogonal: soluble yield and monomer fraction by a prespecified purification/SEC workflow.',
                'Quality: construct identity, purity, aggregation flag, and complete raw curve retention.',
            ],
            'replication': {'biological_replicates_per_construct': 3,
                'technical_replicates_per_biological_replicate': 2,
                'randomization': 'Randomize construct positions within batch and balance score strata across plates/batches.',
                'blinding': 'Mask raw model score during primary endpoint calling.'},
            'quality_control': [
                'Sequence-verify every construct before measurement.',
                'Predefine purity, concentration, curve-fit, and replicate-dispersion acceptance thresholds.',
                'Retain failures and missingness; do not replace failed constructs after unblinding.',
                'Analyze batch effects and report exclusions with reasons.',
            ],
            'analysis_plan': (
                'Estimate endpoint distributions and uncertainty by prespecified score stratum; report '
                'effect sizes and confidence intervals. Do not translate model score into delta-Tm or '
                'beneficial/deleterious direction without an independently verified semantic contract.'),
            'model_prediction_limitations': [
                'The upstream values are model predictions, not experimental observations.',
                'The score direction is unverified.',
                'Descriptive table coverage does not establish predictive accuracy or causal effect.',
            ],
            'unresolved_scientific_questions': [
                'What is the provider-documented score definition and direction?',
                'Which exact protein construct, sequence, expression system, and assay conditions will be used?',
                'Which mutation strata and sample size meet a preregistered power or precision target?',
                'Which orthogonal endpoint is biologically appropriate for the target protein?',
            ],
            'upstream_analysis_ref': analysis.id,
            'upstream_summary_sha256': hashlib.sha256(json.dumps(summary, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest(),
            'experiment_executed': False,
        }
        if inputs.get('execution_mode') == 'simulation':
            # A virtual replay procedure is distinct from a proposed wet-lab study.
            # Preserve the qualified analysis identity and explicit fixture scope.
            from bioma.core.measurements import simulation_payload
            simulation = simulation_payload(inputs)
            plan = {'schema_version':'analysis-bound-simulation-plan-v1',
                'protocol_id':'simulation-' + plan['upstream_summary_sha256'][:24],
                'protocol_text': 'Virtual-device replay only. Source CSV has ' + str(summary['row_count']) +
                    ' rows. Preserve descriptive statistics and unknown biological interpretation. '
                    'Replay the explicitly supplied synthetic measurements; retain failures and receipt identities. '
                    'No physical experiment or biological validation is authorized.',
                'execution_scope':'simulation','experiment_executed':False,
                'upstream_analysis_ref':analysis.id,'upstream_summary_sha256':plan['upstream_summary_sha256'],
                'simulation_source':simulation['source'], 'simulation_payload_sha256':hashlib.sha256(
                    json.dumps(simulation,sort_keys=True,separators=(',', ':')).encode()).hexdigest()}
        root = Path(base_dir) / 'artifacts' / 'derived' / state.task_id / 'study-design'
        root.mkdir(parents=True, exist_ok=True)
        path = root / 'analysis-informed-study-plan.json'
        path.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
        artifact = Artifact(kind='study-protocol-json' if inputs.get('execution_mode') == 'simulation' else 'study-plan-json', uri=str(path), source='study-design',
            metadata={'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'upstream_analysis_ref': analysis.id})
        return WorkResult(family=STUDY, kind='StudyPlan', input_refs=[analysis.id],
            payload=plan, artifacts=[artifact.model_dump(mode='json')],
            provenance={'method': 'analysis_informed_study_design',
                'qualified_analysis_refs': [analysis.id],
                'acceptance_input_hash': inputs.get('_acceptance_input_hash'),
                'source_message_id': message.id})
    return execute
