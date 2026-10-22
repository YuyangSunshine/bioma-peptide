"""Read-side execution identity derived from existing observations and receipts.

No execution, grant, result registration or new on-disk catalog lives here.
Native receipts remain authoritative for what they actually record. In particular,
input manifests are NOT an observation of file reads, and exit zero is NOT science.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def digest(path):
    from bioma.runtime.deadline import check
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024 * 1024), b''):
            check()
            value.update(block)
    return value.hexdigest()


def computation_packages(context):
    """Compatibility-shaped packages from observed execution events, not producers.

    Legacy checkpoint fields are read-only fallback material. New writers never
    populate them. This also lets old archived records be inspected without edits.
    """
    result = deepcopy(context.state.get('investigation_completed', {}))
    latest = context.state.get('investigation_latest')
    if latest:
        result.setdefault(latest['metrics']['receipt_path'], deepcopy(latest))
    for event in context.observations or context.state.get('harness_observations', []):
        package = event.get('execution_package')
        if package and package.get('status') == 'ok':
            ref = package['metrics']['receipt_path']
            result[ref] = deepcopy(package)
    return result


def _identities(files, cache=None):
    rows = []
    for filename, expected in files.items():
        try:
            if cache is not None and filename in cache:
                actual = cache[filename]
            else:
                actual = digest(filename)
                if cache is not None:
                    cache[filename] = actual
            status = 'matched' if actual == expected else 'changed'
        except OSError:
            actual, status = None, 'missing'
        rows.append({'path':filename, 'sha256':expected, 'current_sha256':actual,
                     'integrity':status})
    return rows


def _read_native(ref):
    try:
        value = json.loads(Path(ref).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def execution_view(context):
    """One reconstructible view for readiness, recovery, agents and review.

    `checks` describes entry prerequisites for individual checks; no single
    `valid_science` flag is manufactured. Unknown provenance remains explicit.
    """
    checkpoint = getattr(context, 'investigation_checkpoint', None)
    scope = {'task_id':context.task_id,
             'workspace_scope':str(context.state.get('artifact_scope_id') or context.task_id),
             'checkpoint_id':getattr(getattr(checkpoint, 'path', None), 'stem', None)}
    rows, represented = [], set()
    measured = {}
    def identities(files):
        return _identities(files, measured)

    def row(ref, native, metrics, package=None, kind='registered_tool', status=None):
        dispatch = metrics.get('dispatch') or native.get('dispatch') or {}
        source_files = metrics.get('execution_sha256', {})
        native_hash = source_files.get(ref)
        gaps = ['actual_input_reads_not_observed']
        local = Path(ref).resolve().is_relative_to(context.artifact_dir().resolve())
        if not local:
            gaps.append('native_receipt_outside_work_package')
        if not native_hash:
            native_hash = next((r['sha256'] for r in context.state.get('tool_receipts', [])
                                if r['path'] == ref), None)
        refs = identities({ref:native_hash}) if native_hash else []
        if not native_hash:
            gaps.append('native_receipt_hash_binding_missing')
        program_paths = (package or {}).get('patch', {}).get('analysis_code_paths', [])
        program_hashes = {p:source_files.get(p) for p in program_paths}
        environment = metrics.get('runtime') or native.get('runtime') or native.get('provenance', {}).get('execution_environment')
        if not environment:
            gaps.append('execution_environment_not_recorded')
        if metrics.get('support_kind') == 'retrieval':
            gaps.append('source_content_and_contract_applicability_require_review')
        if kind == 'registered_tool':
            gaps.append('tool_internal_execution_environment_not_observed')
        parameters = metrics.get('execution_arguments', native.get('execution_arguments', native.get('arguments')))
        if parameters is None:
            gaps.append('execution_parameters_not_recorded')
        inputs = metrics.get('input_sha256', {})
        outputs = identities(metrics.get('sha256', {}))
        source_rows = identities(source_files)
        input_rows = identities(inputs)
        if any(r['integrity'] != 'matched' for r in refs + source_rows + input_rows + outputs):
            gaps.append('recorded_bytes_missing_or_changed')
        status = status or native.get('execution_status') or native.get('status') or 'unknown'
        success = status in {'ok', 'succeeded', 'success'}
        if not success:
            gaps.append('execution_not_successful')
        if not dispatch:
            gaps.append('runtime_dispatch_identity_not_recorded')
        integrity_ready = local and bool(refs) and not any(r['integrity'] != 'matched'
            for r in refs + source_rows + input_rows + outputs)
        rows.append({'execution_id':ref, 'kind':kind, 'work_package':scope,
            'support_kind':metrics.get('support_kind', 'computation' if package else None),
            'native_result_kind':native.get('result_kind'),
            'action':{'action_id':dispatch.get('action_instance_id') or native.get('action_id') or dispatch.get('attempt_id'),
                      'native_action_id':native.get('action_id'), 'runtime_dispatch':dispatch},
            'native_receipts':refs or [{'path':ref,'sha256':None,'integrity':'unbound'}],
            'programs':identities(program_hashes),
            'parameters':parameters,
            'tool_implementation':{'identity':native.get('tool_implementation_identity'),
                                   'version':native.get('version')} if kind == 'registered_tool' else None,
            'inputs':{'authorized_or_declared':input_rows, 'observed_access':None,
                      'access_observation_status':'not_instrumented',
                      'scope':'declared scientific input files, not a complete process filesystem access log'},
            'environment':environment,
            'environment_scope':'registered-tool wrapper; internal environment not established'
                                if kind == 'registered_tool' else 'native analysis runner measurement',
            'outputs':outputs,
            'outcome':{'status':status, 'native_status':native.get('status'), 'exit_code':native.get('execution', native.get('process', {})).get('exit_code'),
                       'exception':native.get('error'),
                       'effects':'observed_process_exit' if native.get('execution', native.get('process', {})).get('exit_code') is not None
                                 else 'observed_return' if success else 'not_established'},
            'checks':{'receipt_integrity':integrity_ready,
                      'recorded_execution_succeeded':success,
                      'actual_input_access':False,
                      'contract_review_candidate':bool(package) and success and integrity_ready},
            'gaps':gaps, 'scientific_support':'not_assessed'})
        represented.add(ref)
        if kind == 'registered_tool':
            aliases = {p:h for p,h in source_files.items() if p != ref
                       and native.get('action_id')
                       and _read_native(p).get('action_id') == native['action_id']}
            rows[-1]['native_receipts'].extend(identities(aliases))
            represented.update(aliases)

    for ref, package in delivery_support_packages(context).items():
        if package['metrics'].get('support_kind') == 'professional_method':
            continue  # Render native Ledger facts below, not as a file receipt.
        native = _read_native(ref)
        kind = 'registered_tool' if native.get('tool') else 'scientific_code'
        row(ref, native, package.get('metrics', {}), package, kind,
            status=native.get('execution_status') or native.get('status') or 'unknown')

    # Registered failures and validated/non-computational tool results retain
    # their original identity too. They do not become scientific packages.
    for binding in context.state.get('tool_receipts', []):
        ref = binding['path']
        if ref not in represented:
            native = _read_native(ref)
            result = native.get('result') or {}
            row(ref, native, {'execution_sha256':{ref:binding['sha256']},
                'sha256':result.get('files', {}) if isinstance(result, dict) else {}},
                status=binding.get('status') or native.get('status', 'unknown'))

    events = context.observations or context.state.get('harness_observations', [])
    for event in events:
        failure = event.get('metrics', {}).get('failed_execution_receipt')
        if failure and failure['path'] not in represented:
            native = _read_native(failure['path'])
            process = native.get('process', {})
            source = Path(failure['path']).parent / ('analysis.R' if process.get('engine') == 'R' else 'analysis.py')
            partial = event.get('metrics', {}).get('retained_partial_outputs') or {}
            row(failure['path'], native, {
                'execution_sha256':{failure['path']:failure['sha256'],
                                    str(source):process.get('script_sha256')},
                'input_sha256':process.get('input_sha256', {}),
                'sha256':partial.get('files', {}),
            }, kind='scientific_code', status='failed')
            rows[-1]['outcome']['exception'] = event.get('summary')
        if event.get('tool_name') != 'mini_swe_shell':
            continue
        m = event.get('metrics', {})
        if not m.get('action_id'):
            continue
        versions = {m['source_archive']:m['executed_source_sha256'],
                    m['output_archive']:m['output_sha256']}
        bindings = identities(versions)
        valid = all(r['integrity'] == 'matched' for r in bindings)
        inputs = identities(m.get('input_sha256', {}))
        valid = valid and all(r['integrity'] == 'matched' for r in inputs)
        outputs = identities(m.get('output_versions', {}))
        valid = valid and all(r['integrity'] == 'matched' for r in outputs)
        gaps = ['actual_input_reads_not_observed','transitive_program_execution_not_observed',
                'scientific_applicability_not_checked']
        if not m.get('output_versions'):
            gaps.append('versioned_output_files_not_recorded')
        if m.get('output_errors'):
            gaps.append('declared_output_capture_incomplete')
        if not valid:
            gaps.append('recorded_bytes_missing_or_changed')
        rows.append({'execution_id':m['action_id'], 'kind':'shell', 'work_package':scope,
            'action':{'action_id':m['action_id'],'runtime_dispatch':m.get('dispatch')},
            'native_receipts':bindings,
            'programs':bindings[:1], 'parameters':m.get('parameters'),
            'inputs':{'authorized_or_declared':inputs,'observed_access':None,
                      'access_observation_status':'not_instrumented'},
            'environment':m.get('runtime'), 'outputs':outputs,
            'outcome':{'status':m['status'],'exit_code':m['returncode'],
                       'exception':None,'effects':'observed_process_exit'},
            'checks':{'receipt_integrity':valid, 'recorded_execution_succeeded':m['returncode']==0,
                      'actual_input_access':False,'contract_review_candidate':False},
            'gaps':gaps,'scientific_support':'not_assessed'})

    # An interrupted action must be visible even when no ToolObservation exists.
    journal = context.artifact_dir('action_records') / 'actions.jsonl'
    pending = {}
    completed_errors = []
    if journal.exists():
        for event in map(json.loads, journal.read_text().splitlines()):
            if event['phase'] == 'started':pending[event['action_id']] = event
            elif event['phase'] == 'completed':
                start = pending.pop(event['action_id'], None)
                result = event.get('result', {})
                metrics = result.get('observation', {}).get('metrics', {})
                ref = metrics.get('receipt_path') or metrics.get('failed_execution_receipt', {}).get('path')
                matches = [r for r in rows if ref and (r['execution_id'] == ref or
                    any(p['path'] == ref for p in r['native_receipts']))]
                for matched in matches:
                    matched['action']['action_id'] = event['action_id']
                if (start and event.get('status') == 'error' and not matches
                        and not any(r['action'].get('action_id') == event['action_id'] for r in rows)):
                    completed_errors.append((start, event))
    for start, event in completed_errors:
        rows.append({'execution_id':event['action_id'], 'kind':start['request']['kind'],
            'record_type':'action_attempt', 'work_package':scope,
            'action':{'action_id':event['action_id']}, 'parameters':start['request'],
            'native_receipts':[], 'programs':[], 'outputs':[], 'environment':None,
            'inputs':{'authorized_or_declared':[], 'observed_access':None},
            'outcome':{'status':'action_error', 'exit_code':None,
                'exception':event['result'].get('exception_info') or event['result'].get('output'),
                'effects':'no_process_effect_assertion'},
            'checks':{'contract_review_candidate':False},
            'gaps':['no_native_process_receipt'], 'scientific_support':'not_assessed'})
    for action_id, event in pending.items():
        rows.append({'execution_id':action_id, 'kind':event['request']['kind'],
            'work_package':scope, 'action':{'action_id':action_id},
            'parameters':event['request'], 'native_receipts':[], 'programs':[],
            'inputs':{'authorized_or_declared':[], 'observed_access':None,
                      'access_observation_status':'unknown'}, 'environment':None,'outputs':[],
            'outcome':{'status':'in_flight_or_interrupted','exit_code':None,
                       'exception':None,'effects':'unknown'},
            'checks':{'contract_review_candidate':False},
            'gaps':['no_completed_action_observation','unknown_effects_no_automatic_replay'],
            'scientific_support':'not_assessed'})
    if context.state.get('unresolved_tool_action') and not pending:
        rows.append({'execution_id':None,'kind':'tool','work_package':scope,
            'action':context.state['unresolved_tool_action'], 'native_receipts':[],
            'outcome':{'status':'unresolved','effects':'unknown','exit_code':None},
            'checks':{'contract_review_candidate':False},
            'gaps':['no_completed_tool_observation','unknown_effects_no_automatic_replay'],
            'scientific_support':'not_assessed'})
    # Professional Methods retain native WP1 proofs in the same Runtime ledger.
    # A tool-history entry is only a locator, never a new result authority.
    for observation in context.observations or context.state.get('harness_observations', []):
        if observation.get('tool_name') != 'request_scientific_capability':
            continue
        ref = observation.get('metrics', {}).get('ledger_event_ref')
        if not ref or ref in represented:
            continue
        represented.add(ref)
        from bioma.runtime.capability_requests import read_result
        try:
            event = read_result(ref)
        except ValueError:
            rows.append({'execution_id':ref, 'kind':'professional_method',
                'work_package':scope, 'checks':{'receipt_integrity':False,
                    'contract_review_candidate':False},
                'gaps':['native_runtime_record_not_accessible_under_current_authority'],
                'scientific_support':'not_assessed'})
            continue
        payload = event.payload
        proof = payload['delta'].get('transition') or {}
        native = payload['observation']
        outputs = identities({a['uri']:a['metadata']['sha256'] for a in native['artifacts']})
        inputs = identities(payload['input_manifest'])
        intact = all(r['integrity'] == 'matched' for r in inputs + outputs)
        gaps = ['actual_input_reads_not_observed', 'method_result_applicability_requires_review',
                'method_internal_environment_and_program_not_observed_by_dispatch']
        if not intact:
            gaps.append('recorded_bytes_missing_or_changed')
        rows.append({'execution_id':ref, 'kind':'professional_method',
            'work_package':{**scope, 'child_message_id':native['child_message_id'],
                            'request_id':native['request_id']},
            'action':{'runtime_dispatch':proof.get('result')},
            'native_receipts':[{'ledger_event_ref':ref, 'event_hash':event.event_hash,
                                'integrity':'matched'}],
            'programs':[], 'parameters':payload['message']['payload'],
            'inputs':{'authorized_or_declared':inputs, 'observed_access':None,
                      'access_observation_status':'not_instrumented'},
            'environment':None, 'outputs':outputs,
            'outcome':{'status':native['outcome'], 'effects':'recorded_method_return'},
            'checks':{'receipt_integrity':intact,
                      'recorded_execution_succeeded':native['outcome']=='completed',
                      'execution_review_passed':native['status']=='reviewed',
                      'contract_review_candidate':intact and native['outcome']=='completed'
                          and native['status']=='reviewed' and bool(native['work_results'])},
            'gaps':gaps, 'scientific_support':'not_assessed'})
    return {'schema':'bioma-execution-view-v1','work_package':scope,'records':rows,
            'authority':'Derived from native observations; grants no permission or scientific status.'}


def delivery_support_packages(context):
    """Read native records for contract review, without inventing computations.

    Retrieval material is only a candidate for a source-based obligation. The
    independent review must explicitly establish applicability and provenance.
    No files, executions or result catalogs are created here.
    """
    from dataclasses import asdict
    from .types import ToolObservation
    packages = computation_packages(context)
    represented = {p for v in packages.values() for p in v['metrics'].get('execution_sha256', {})}
    for record in context.state.get('tool_receipts', []):
        ref = record['path']
        if ref in represented or ref in packages:
            continue
        if (record.get('status') != 'ok' or not record.get('result_validated')
                or record.get('result_kind') not in {'retrieved_fact', 'source', 'source_set'}):
            continue
        path = Path(ref)
        if not path.resolve().is_relative_to(context.artifact_dir().resolve()):
            continue
        if not path.is_file() or digest(path) != record.get('sha256'):
            continue
        native = _read_native(ref)
        if (native.get('status') != 'ok' or not native.get('result_validated')
                or native.get('result_kind') != record['result_kind']):
            continue
        result = native.get('result')
        if not result:
            continue
        # Exact captured tool return, never a synthesized answer or a claim that
        # the tool's validator established scientific relevance/provenance.
        acquired = {name:binding['sha256'] for name,binding in
                    context.state.get('admitted_tool_inputs', {}).items()
                    if binding.get('receipt_path') == ref
                    and binding.get('receipt_sha256') == record['sha256']}
        if any(not isinstance(result, dict) or result.get('files', {}).get(name) != expected
               for name, expected in acquired.items()):
            continue
        packages[ref] = asdict(ToolObservation(native['tool'], 'ok',
            'Recorded retrieval material awaiting source and contract applicability checks.',
            metrics={'receipt_path':ref, 'support_kind':'retrieval',
                'summary':{'retrieved_result':result}, 'sha256':{},
                'input_sha256':acquired, 'execution_sha256':{ref:record['sha256']},
                'runtime':native.get('runtime', {}), 'dispatch':native.get('dispatch'),
                'execution_arguments':native.get('arguments')},
            patch={'analysis_code_paths':[], 'execution_receipts':[ref]}))
    # History supplies locators only. Eligibility is rederived under live WP1.
    from bioma.runtime.capability_requests import delivery_result
    for observation in context.observations or context.state.get('harness_observations', []):
        if observation.get('tool_name') != 'request_scientific_capability':
            continue
        ref = observation.get('metrics', {}).get('ledger_event_ref')
        if not ref or ref in packages:
            continue
        try:
            event = delivery_result(ref)
        except ValueError:
            continue
        payload = event.payload
        native = payload['observation']
        outputs = {a['uri']:a['metadata']['sha256'] for a in native['artifacts']}
        from bioma.core.research import fingerprint
        origins = [{'id':row['id'], 'family':row['value']['family'],
                    'kind':row['value']['kind'], 'identity':fingerprint(row['value'])}
                   for row in native['work_results'] if row['kind']=='work_result']
        packages[ref] = asdict(ToolObservation('request_scientific_capability', 'ok',
            'Native Method results awaiting parent contract review; not scientific Evidence.',
            artifacts=list(outputs),
            metrics={'receipt_path':ref, 'support_kind':'professional_method',
                'native_receipt_hashes':{ref:event.event_hash},
                'native_work_results':origins,
                'summary':{'work_results':native['work_results']}, 'sha256':outputs,
                'input_sha256':payload['input_manifest'], 'execution_sha256':{},
                'runtime':{'scope':'native Method; inspect its recorded provenance'},
                'artifact_names':{a['uri']:a['metadata'].get('relative_path', Path(a['uri']).name)
                                  for a in native['artifacts']}},
            patch={'analysis_code_paths':[], 'execution_receipts':[ref]}))
    return packages
