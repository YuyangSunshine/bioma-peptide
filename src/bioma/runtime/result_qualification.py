"""Explicit runtime qualification of committed work; no default result promotion."""
from __future__ import annotations
from bioma.core.scientific_capabilities import has_computational_result

from bioma.core.models import AgentMessage, Evidence, StateDelta
from bioma.core.research import fingerprint
from bioma.core.scientific_capabilities import WorkResult
from bioma.runtime.task_lease import task_lease


class WorkResultQualificationService:
    """A domain-installed validator proposes Evidence; existing verifier commits.

    The callback receives only a copied WorkResult and returns a candidate
    Evidence or None. It has no state, controller, device or ledger authority.
    The model ID must identify the frozen scientific qualification rule. This
    service is never installed implicitly for bounded WORK or delivery.
    """
    def __init__(self, model_id, qualify_candidate):
        if not model_id:
            raise ValueError('Qualification requires a versioned domain model')
        self.model_id, self.qualify_candidate = model_id, qualify_candidate

    @classmethod
    def linear_regression(cls, *, input_manifest, source_uri, x_column, y_column,
                          result_name='result.json', report_name='report.md'):
        """Host-frozen numerical rule; never trust model-selected data or hashes.

        Candidate checks do not grant admission. qualify() still requires the
        result's own successful WP1 execution and Runtime verification.
        """
        from pathlib import Path
        from bioma.core.tabular import sha256_file
        from bioma.core.linear_regression import verify_linear_fit, checked_linear_fit
        manifest = {str(Path(p).resolve()): h for p, h in input_manifest.items()}
        source = str(Path(source_uri).resolve())
        if source not in manifest or len(manifest) != len(input_manifest):
            raise ValueError('Numerical source must be uniquely frozen in admitted inputs')
        checked_linear_fit(source, manifest[source], x_column, y_column)
        def qualify(work):
            if (work.family != 'scientific.compute' or not has_computational_result(work)
                    or work.provenance.get('method') != 'continuous_scientific_investigation'):
                return None
            for path, digest in manifest.items():
                if sha256_file(Path(path)) != digest:
                    raise ValueError('Frozen regression input changed')
            observed = {str(Path(p).resolve()): h
                        for p, h in work.payload.get('input_manifest', {}).items()}
            if observed != manifest:
                raise ValueError('Regression WorkResult input identity mismatch')
            from bioma.core.source_inputs import verify_source_identities
            sources = verify_source_identities(work.provenance.get('source_inputs', []))
            if ({str(Path(s.uri).resolve()):s.sha256 for s in sources} != manifest
                    or not {s.id for s in sources}.issubset(set(work.input_refs))):
                raise ValueError('Regression source references do not match frozen inputs')
            if not work.input_refs or not work.payload.get('execution_receipts'):
                raise ValueError('Regression requires input references and execution receipts')
            artifacts = work.artifacts
            hashes = work.payload.get('delivery_review', {}).get('sha256', {})
            for row in artifacts:
                path = Path(row['uri'])
                digest = row.get('metadata', {}).get('sha256')
                if not digest or sha256_file(path) != digest or hashes.get(str(path)) != digest:
                    raise ValueError('Regression artifact changed or lacks review binding')
            def named(name):
                matches = [a for a in artifacts if Path(a['uri']).name == name]
                if len(matches) != 1:
                    raise ValueError('Regression requires one unambiguous artifact: ' + name)
                return matches[0]
            result, report = named(result_name), named(report_name)
            if not Path(report['uri']).read_text().strip():
                raise ValueError('Missing regression report')
            checked = verify_linear_fit(source, manifest[source], result['uri'],
                result['metadata']['sha256'], x_column, y_column)
            return Evidence(kind='AnalysisResult', source='harness',
                claim='Unweighted linear fit and residuals independently recomputed from frozen inputs; not biological confirmation.',
                value={'qualification_status':'independently_recomputed',
                    'scope':'unweighted_ols_with_intercept_complete_finite_rows',
                    'source_sha256':manifest[source], 'x_column':x_column, 'y_column':y_column,
                    'fit':checked, 'input_manifest':manifest,
                    'result_sha256':result['metadata']['sha256'],
                    'limits':['In-sample numerical verification only; no causal, predictive or biological validation.',
                              'Report existence and hash checked; narrative scientific claims are not certified.']},
                artifact_ids=[a['id'] for a in artifacts])
        return cls('independent-linear-regression-v1', qualify)

    @classmethod
    def protocol_review(cls, model_id, independent_review, *, execution_scope):
        """Host-installed independent review; a plan's own review is not admission.

        Scope grants no WP1 permission. Physical execution still needs an
        authenticated device implementation and a separate resource grant.
        """
        if execution_scope not in {'compilation', 'simulation'}:
            raise ValueError('This qualifier does not authorize physical protocols')
        def qualify(work):
            import hashlib, json
            from pathlib import Path
            from bioma.core.protocols import VerificationReport
            if work.family != 'scientific.study_design' or work.kind != 'StudyPlan':
                return None
            payload = work.payload
            text = payload.get('protocol_text')
            if not isinstance(text,str) or not text.strip() or not payload.get('protocol_id'):
                return None
            artifact = next((a for a in work.artifacts if a['kind']=='study-protocol-json'), None)
            if not artifact:
                return None
            path = Path(artifact['uri'])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=artifact.get('metadata',{}).get('sha256'):
                raise ValueError('StudyPlan artifact identity changed')
            saved = json.loads(raw)
            if any(saved.get(k)!=payload.get(k) for k in ('protocol_id','protocol_text','review')):
                raise ValueError('StudyPlan text differs from its actual artifact')
            review = independent_review(work.model_copy(deep=True))
            if not isinstance(review,VerificationReport):
                raise TypeError('Independent protocol review must return VerificationReport')
            if not review.passed or review.issues:
                return None
            return Evidence(kind='reviewed_protocol', claim='Independently reviewed protocol, limited to '+execution_scope,
                source='harness', value={'qualification_status':'independently_reviewed',
                    'execution_scope':execution_scope,'protocol_id':payload['protocol_id'],
                    'protocol_text':text,'protocol_sha256':hashlib.sha256(text.encode()).hexdigest(),
                    'work_result_identity':fingerprint(work.model_dump(mode='json')),
                    'review':{'passed':True,'issues':[]},'experiment_executed':False},
                artifact_ids=[artifact['id']])
        return cls(model_id,qualify)

    @classmethod
    def research_review(cls, model_id, state, independent_review):
        """Qualify an executed full-contract proposal using an independent check.

        The host installs the reviewer; the producing Provider cannot install or
        bypass it. Capturing the state does not grant execution or commit rights.
        The verifier rechecks the current snapshot at the actual commit boundary.
        """
        from bioma.core.protocols import VerificationReport
        from bioma.core.research import validate_assessment, review_snapshot
        frozen = state.model_copy(deep=True)
        def qualify(work):
            if work.family != 'scientific.evidence_assessment' or work.kind != 'EpistemicQualification':
                return None
            payload = work.payload
            if payload.get('assessment', {}).get('snapshot_hash') != review_snapshot(frozen):
                raise ValueError('Full-contract proposal has a stale review snapshot')
            candidate = Evidence(kind='research_assessment', source='harness',
                claim='Independently checked full-contract review; not independent biological confirmation.',
                value={key:payload[key] for key in ('assessment', 'reviewed_state_identity', 'reviewed_state_version')},
                parent_ids=list(dict.fromkeys(ref for row in payload['assessment']['requirements']
                    for ref in row['evidence_ids'])))
            validate_assessment(frozen, candidate)
            report = independent_review(work.model_copy(deep=True))
            if not isinstance(report, VerificationReport):
                raise TypeError('Independent research review must return VerificationReport')
            if not report.passed or report.issues:
                return None
            candidate.value['independent_review'] = {'model_id':model_id,
                'passed':report.passed, 'issues':list(report.issues)}
            return candidate
        return cls(model_id, qualify)

    def qualify(self, runtime, task_id, result_refs):
        if runtime.ledger is None or not runtime.ledger.verify_chain(task_id):
            raise ValueError('Qualification requires verified committed work')
        refs = []
        with task_lease(runtime.ledger, task_id):
            state = runtime.ledger.replay_state(task_id)
            if state is None:
                raise ValueError('Qualification requires committed work')
            events = runtime.ledger.events(task_id)
            requested = set(result_refs)
            observations = {o.id: o for o in state.observations if o.kind == 'work_result'}
            if requested - observations.keys():
                raise ValueError('Unknown WorkResult reference')
            # Receipt presence somewhere in a run is not provenance for this result.
            # Replay above validates each proof and the journaled permit/result;
            # bind each requested observation to its successful owner output.
            committed = {}
            for event in events:
                if event.kind != 'TransitionCommitted':
                    continue
                proof = event.payload['delta'].get('transition') or {}
                result = proof.get('result', {})
                owner = proof.get('owner_delta', {})
                if (result.get('execution_status') != 'completed'
                        or result.get('work_outcome') != 'completed'
                        or owner.get('outcome') != 'completed'):
                    continue
                for raw in owner.get('updates', {}).get('observations', []):
                    if (raw.get('id') in requested
                            and raw == observations[raw['id']].model_dump(mode='json')):
                        committed[raw['id']] = result
            if requested - committed.keys():
                raise ValueError('WorkResult requires its own committed successful execution')
            for observation in state.observations:
                if observation.id not in requested or observation.kind != 'work_result':
                    continue
                work = WorkResult.model_validate(observation.value)
                key = fingerprint({'task': task_id, 'result': observation.id,
                                   'model': self.model_id, 'work': work.model_dump(mode='json')})
                evidence_id = 'qualified.' + key
                existing = next((e for e in state.evidence if e.id == evidence_id), None)
                if existing:
                    refs.append(existing.id)
                    continue
                from bioma.runtime.admission import ExecutionAuthorizationError
                from bioma.runtime.deadline import ExecutionTimeout
                try:
                    candidate = self.qualify_candidate(work.model_copy(deep=True))
                    if candidate is not None:
                        candidate = Evidence.model_validate(candidate)
                except (ExecutionAuthorizationError, ExecutionTimeout, PermissionError):
                    raise
                except (ValueError, TypeError, OSError) as exc:
                    # Only the independent checker boundary is recoverable here.
                    # Ledger, permission and commit verification failures below
                    # must still propagate; execution is never relabelled failed.
                    runtime.ledger.append(task_id, 'WorkResultQualificationError', 'harness',
                        {'work_result_ref': observation.id, 'qualification_model': self.model_id,
                         'execution_result_identity': fingerprint(committed[observation.id]),
                         'error_type': type(exc).__name__, 'reason': str(exc)})
                    continue
                if candidate is None:
                    runtime.ledger.append(task_id, 'WorkResultQualificationRejected', 'harness',
                        {'work_result_ref':observation.id, 'qualification_model':self.model_id,
                         'execution_result_identity':fingerprint(committed[observation.id]),
                         'reason':'Independent qualifier declined scientific credit'})
                    continue
                candidate = Evidence.model_validate(candidate)
                # Runtime provenance is separate from the provider's raw result.
                candidate = candidate.model_copy(update={'id': evidence_id, 'source': 'harness',
                    'method': self.model_id, 'value': {**(candidate.value or {}),
                        'work_result_ref': observation.id, 'qualification_model': self.model_id,
                        'execution_result_identity': fingerprint(committed[observation.id])}})
                message = AgentMessage(id='qualification.' + key, sender='harness', recipient='harness',
                    message_type='runtime.evidence_qualification')
                from bioma.core.acceptance import protocol_for, completion_status
                protocol = protocol_for(state)
                updates = {}
                outcome = 'completed' if state.status == 'completed' else 'continue'
                if protocol and protocol.mode == 'research':
                    prospective = state.model_copy(update={'evidence':[*state.evidence, candidate]})
                    acceptance = completion_status(prospective)
                    outcome = 'completed' if acceptance['complete'] else 'continue'
                    updates = {'metrics':{'acceptance':acceptance}}
                delta = StateDelta(agent_id='harness', source_message_id=message.id,
                    expected_version=state.version, evidence=[candidate], updates=updates, outcome=outcome)
                if protocol and protocol.mode == 'research' and candidate.kind != 'research_assessment':
                    # Reuse the production activation/router and durable outbox.
                    # Qualification itself never executes a reviewer or a model.
                    from bioma.runtime.acceptance import activate
                    delta = activate(state, delta, message, ())
                    delta = runtime._route_requests(delta, source_message=message,
                        active_agents=set(state.active_agents or runtime.agents),
                        event_sink=None, state=state)
                check, receipt = runtime.verifier.verify_with_receipt(state, delta,
                    expected_agent='harness', source_message=message)
                if not check.ok:
                    raise ValueError('Evidence qualification rejected: ' + '; '.join(check.reasons))
                state = runtime.ledger.commit_transition(state,
                    delta.model_copy(update={'status': 'verified'}), message, receipt)
                refs.append(candidate.id)
        if getattr(runtime, 'memory', None) is not None:
            runtime.memory.sync(runtime.ledger, task_id)
        return tuple(refs)


def production_scientific_result_qualifier(*, inputs=None, state=None):
    """Qualification rules for reproducible AnalysisResult and dependent StudyPlan."""
    import json
    from pathlib import Path
    from bioma.core.tabular import sha256_file, summarize_csv

    frozen = state.model_copy(deep=True) if state is not None else None

    def qualify(work):
        if (frozen is not None and (inputs or {}).get('execution_mode') == 'simulation'
                and work.kind in {'StudyPlan', 'ExecutionReceipt'}):
            from bioma.core.protocols import VerificationReport
            from bioma.core.scientific_capabilities import WorkResult
            if work.kind == 'StudyPlan':
                refs = work.provenance.get('qualified_analysis_refs', [])
                parents = [e for e in frozen.evidence if e.id in refs and e.kind == 'AnalysisResult']
                def review(candidate):
                    if len(parents) != 1:
                        return VerificationReport(False, ('Missing committed qualified analysis',))
                    parent = parents[0]
                    source = next((o for o in frozen.observations if o.id == parent.value.get('work_result_ref')), None)
                    expected = fingerprint(parent.value['summary'])
                    from bioma.core.measurements import simulation_payload
                    payload_hash = fingerprint(simulation_payload(inputs))
                    valid = (source is not None and qualify(WorkResult.model_validate(source.value)) is not None
                        and candidate.payload.get('execution_scope') == 'simulation'
                        and candidate.payload.get('experiment_executed') is False
                        and candidate.payload.get('upstream_analysis_ref') == parent.id
                        and candidate.payload.get('upstream_summary_sha256') == expected
                        and candidate.payload.get('simulation_payload_sha256') == payload_hash
                        and candidate.payload.get('protocol_id') == 'simulation-' + expected[:24]
                        and candidate.payload.get('protocol_text') == (
                            'Virtual-device replay only. Source CSV has ' + str(parent.value['summary']['row_count']) +
                            ' rows. Preserve descriptive statistics and unknown biological interpretation. '
                            'Replay the explicitly supplied synthetic measurements; retain failures and receipt identities. '
                            'No physical experiment or biological validation is authorized.'))
                    return VerificationReport(valid, () if valid else ('Analysis or simulation scope does not match',))
                candidate = WorkResultQualificationService.protocol_review(
                    'analysis-bound-virtual-protocol-v1', review, execution_scope='simulation').qualify_candidate(work)
                if candidate is not None:
                    candidate.parent_ids = refs
                return candidate
            from bioma.core.handoffs import ReviewedProtocol
            from bioma.core.measurements import verify_simulation
            from bioma.core.models import Observation
            if (len(work.input_refs) != 1 or work.payload.get('physical_execution') is not False
                    or work.payload.get('data_kind') != 'simulation'):
                return None
            protocol = ReviewedProtocol.from_state(frozen, work.input_refs[0])
            observation = Observation(kind='simulated_experiment', source='experiment-execution', value=work.payload['observation'])
            verify_simulation(observation, work.payload['machine_code'], frozen.task_id, protocol.text, work.provenance['execution_id'])
            for artifact in work.artifacts:
                path = Path(artifact['uri'])
                if not path.is_file() or sha256_file(path) != artifact.get('metadata', {}).get('sha256'):
                    return None
                expected_content = {'machine-code-json': work.payload['machine_code'],
                                    'simulation-observation-json': work.payload['observation']}.get(artifact['kind'])
                if expected_content is None or json.loads(path.read_text()) != expected_content:
                    return None
            return Evidence(kind='simulation_receipt_checked', source='harness',
                claim='Qualified protocol and virtual-device receipts agree; not a physical experiment.',
                value={'data_kind':'simulation','physical_execution':False},
                parent_ids=work.input_refs, artifact_ids=[a['id'] for a in work.artifacts])
        if work.kind == 'Calculation' and work.provenance.get('method') == 'sequence_descriptors':
            return _check_sequence_calculation(work, inputs or {})
        if has_computational_result(work):
            from bioma.core.prediction_checks import qualify_alphafold_prediction
            try:
                prediction = qualify_alphafold_prediction(work)
            except (KeyError, IndexError) as exc:
                raise ValueError('Malformed prediction receipt or confidence structure') from exc
            if prediction is not None:
                return prediction
            payload = work.payload
            source = Path(str(payload.get('source_csv_uri', '')))
            if (not source.is_file() or sha256_file(source) != payload.get('source_csv_sha256')
                    or payload.get('score_direction') != 'unverified'):
                return None
            recomputed = summarize_csv(source)
            if recomputed != payload.get('summary'):
                return None
            summary_artifact = next((row for row in work.artifacts
                if row.get('kind') == 'analysis-summary-json'), None)
            receipt_artifact = next((row for row in work.artifacts
                if row.get('kind') == 'analysis-execution-receipt'), None)
            code_artifact = next((row for row in work.artifacts
                if row.get('kind') == 'analysis-code-python'), None)
            if not all((summary_artifact, receipt_artifact, code_artifact)):
                return None
            for artifact in (summary_artifact, receipt_artifact, code_artifact):
                path = Path(str(artifact.get('uri', '')))
                if not path.is_file() or sha256_file(path) != artifact.get('metadata', {}).get('sha256'):
                    return None
            saved = json.loads(Path(summary_artifact['uri']).read_text(encoding='utf-8'))
            if saved != recomputed or sha256_file(Path(summary_artifact['uri'])) != payload.get('summary_sha256'):
                return None
            return Evidence(kind='AnalysisResult',
                claim='The referenced tabular summary was independently recomputed from the immutable upstream CSV.',
                value={'qualification_status': 'independently_recomputed',
                    'summary': recomputed, 'source_csv_sha256': payload['source_csv_sha256'],
                    'score_direction': 'unverified', 'qualified_outcome': 'positive',
                    'upstream_work_result_refs': [ref for ref in work.input_refs if not ref.startswith('source:sha256:')],
                    'source_input_refs': [ref for ref in work.input_refs if ref.startswith('source:sha256:')],
                    'evidence_package': {'input_hash': work.provenance.get('acceptance_input_hash'),
                        'method_id': work.provenance.get('method_id') or work.provenance.get('method'),
                        'responsibility': work.family}},
                source='harness', parent_ids=[],
                artifact_ids=[row['id'] for row in work.artifacts])
        if work.kind == 'StudyPlan':
            refs = work.provenance.get('qualified_analysis_refs', [])
            required = {'scientific_validation_objective', 'comparison_objects', 'controls',
                'measurement_endpoints', 'replication', 'quality_control', 'model_prediction_limitations',
                'unresolved_scientific_questions', 'experiment_executed'}
            if (not refs or not required <= set(work.payload) or work.payload.get('experiment_executed') is not False
                    or not all(work.payload.get(key) for key in required - {'experiment_executed'})):
                return None
            return Evidence(kind='StudyPlan',
                claim='A future study plan was generated from a referenced qualified AnalysisResult; no experiment was executed.',
                value={'qualification_status': 'plan_structurally_validated',
                    'qualified_analysis_refs': refs, 'experiment_executed': False,
                    'qualified_outcome': 'positive',
                    'upstream_input_refs': list(work.input_refs),
                    'evidence_package': {'input_hash': work.provenance.get('acceptance_input_hash'),
                        'method_id': work.provenance.get('method_id') or work.provenance.get('method'),
                        'responsibility': work.family}},
                source='harness', parent_ids=list(refs),
                artifact_ids=[row['id'] for row in work.artifacts])
        return None

    return WorkResultQualificationService(
        # A distinct checker revision cannot reuse an older mislabelled candidate.
        # Existing qualified records remain immutable and readable.
        'production-scientific-result-qualification-v4', qualify)


def _check_sequence_calculation(work, inputs):
    """Reproduce declared numerical definitions; not independent biological data.

    Uses a fresh Biopython invocation, plus counts computed from the input. Shared
    library defects remain a limitation. This function only proposes Evidence;
    it has no Runtime, ledger or submission authority.
    """
    import csv
    import hashlib
    import io
    import json
    from collections import Counter
    from pathlib import Path
    from bioma.core.sequence_inputs import canonical_sequence
    sequence = inputs.get('sequence')
    if (not sequence or canonical_sequence(sequence) != sequence
            or len(inputs.get('sequences', [])) > 1 or work.family != 'scientific.compute'):
        return None
    result = work.payload.get('calculation')
    if not isinstance(result, dict):
        # Read-only qualification/replay of historical result records.
        rows = work.payload.get('candidate_results', [])
        if len(rows) != 1:
            return None
        result = rows[0].get('value', {})
    from Bio import __version__
    from Bio.SeqUtils.ProtParam import ProteinAnalysis
    analysis = ProteinAnalysis(sequence)
    counts = {aa: Counter(sequence)[aa] for aa in 'ACDEFGHIKLMNPQRSTVWY'}
    digest = hashlib.sha256(sequence.encode()).hexdigest()
    expected = {
        'sequence': sequence, 'sequence_sha256': digest, 'length': len(sequence),
        'data_kind': 'calculation', 'method': 'Bio.SeqUtils.ProtParam.ProteinAnalysis',
        'biopython_version': __version__,
        'molecular_weight_da': round(analysis.molecular_weight(), 4),
        'theoretical_pI': round(analysis.isoelectric_point(), 4),
        'gravy': round(analysis.gravy(), 4), 'aromaticity': round(analysis.aromaticity(), 6),
        'instability_index': round(analysis.instability_index(), 4),
        'secondary_structure_fraction': dict(zip(('helix', 'turn', 'sheet'),
            [round(v, 6) for v in analysis.secondary_structure_fraction()])),
        'amino_acid_counts': counts,
        'amino_acid_percent': {aa: round(n * 100 / len(sequence), 4) for aa, n in counts.items()}}
    if any(result.get(key) != value for key, value in expected.items()):
        return None
    kinds = {'sequence-properties-json', 'amino-acid-composition-csv'}
    artifacts = [a for a in work.artifacts if a['kind'] in kinds]
    if len(artifacts) != 2 or {a['kind'] for a in artifacts} != kinds:
        return None
    try:
        for artifact in artifacts:
            data = Path(artifact['uri']).read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact.get('metadata', {}).get('sha256'):
                return None
            if artifact['kind'] == 'sequence-properties-json':
                if json.loads(data) != expected:
                    return None
            else:
                records = list(csv.DictReader(io.StringIO(data.decode())))
                if (len(records) != 20 or len({r['amino_acid'] for r in records}) != 20
                        or {r['amino_acid']: int(r['count']) for r in records} != counts
                        or {r['amino_acid']: float(r['percent']) for r in records} != expected['amino_acid_percent']):
                    return None
    except (OSError, ValueError, KeyError, UnicodeError):
        return None
    return Evidence(kind='checked_sequence_calculation', source='harness',
        claim='Source-bound descriptors and CSV/JSON reproduce the documented calculation; no biological confirmation.',
        value={'sequence_sha256': digest, 'scientific_object': digest, 'recomputed': True,
               'scope': 'Computational reproducibility using shared Biopython definitions, not experimental validation.'},
        artifact_ids=[a['id'] for a in artifacts])
