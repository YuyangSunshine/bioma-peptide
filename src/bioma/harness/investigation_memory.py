"""Bounded operational memory and trace-derived capability attribution, not Evidence."""
from collections import Counter
import hashlib
import json
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def bounded_text(text, limit=4000):
    if len(text) <= limit:
        return text
    return text[:limit//2] + '\n[preview middle omitted; read archived original if needed]\n' + text[-limit//2:]


def skill_reads(context):
    found = {}
    for row in context.state.get('harness_observations', []):
        meta = row.get('metrics', {}).get('metadata', {})
        if row.get('tool_name') == 'skill_view' and row.get('status') == 'ok' and meta.get('success'):
            key = meta.get('stable_skill_id') or meta.get('name')
            if key:
                found[key] = digest(meta)
    return found



def recovery_input_catalog(context):
    """Expose same-query, hash-matching recovery inputs without granting credit.

    Only admitted local files are opened. Paths and claimed source receipts in
    the manifest never authorize reading outside that set.
    """
    from pathlib import Path
    from bioma.harness.input_inspection import _file_sha256
    files={str(Path(p).resolve()):i for i,p in enumerate(context.state.get('raw_data_sources',[]))}
    query=context.state.get('original_user_request') or context.state.get('current_user_request','')
    for filename in files:
        path=Path(filename)
        if path.name!='recovery_provenance.json' or path.stat().st_size>1_000_000:continue
        try:
            data=json.loads(path.read_text())
            if (not str(data.get('run_kind','')).startswith('development_recovery_')
                    or data.get('query_sha256')!=hashlib.sha256(query.encode()).hexdigest()):continue
            groups={}
            for row in data.get('retained_inputs',[]):
                current=str(Path(row['path']).resolve())
                if current not in files or row.get('role') not in {
                        'prior_admitted_input', 'prior_computed_output', 'prior_partial_output',
                        'prior_executed_source', 'prior_status_observation',
                        'verified_structure_observation'}:continue
                if _file_sha256(Path(current))!=row['sha256']:continue
                receipt=row.get('source_receipt','unidentified')
                item={'input_index':files[current],'name':Path(row['source_path']).name,
                      'role':row['role'],'sha256':row['sha256'],
                      'recorded_process_exit_code':row.get('process_exit_code')}
                if Path(current).suffix=='.json' and 'summary' in Path(current).name and Path(current).stat().st_size<=16_000:
                    item['unqualified_saved_summary']=bounded_text(json.dumps(json.loads(Path(current).read_text()),ensure_ascii=False),2000)
                groups.setdefault(receipt,[]).append(item)
            if not groups:continue
            audit=data.get('independent_delivery_audit')
            if audit and (str(Path(audit['path']).resolve()) not in files or _file_sha256(Path(audit['path']))!=audit['sha256']):
                raise ValueError('Independent audit changed or was not admitted')
            if audit:
                audit={**audit,'review':json.loads(Path(audit['path']).read_text())}
            return {'independent_delivery_audit':audit, 'parent_status':data.get('parent_status'),'same_original_query':True,
                    'versions':[{'source_receipt_label':key,'inputs':value} for key,value in list(groups.items())[-4:]],
                    'authority':'Hash-matching admitted recovery files, not qualified Evidence or accepted delivery. Receipt paths here are labels, not new read permissions.'}
        except (ValueError,KeyError,TypeError,OSError):continue
    return None


def working_memory(context, recent_messages):
    """Operational phase is inferred from receipts, never a scientific conclusion."""
    state = context.state
    rows = state.get('harness_observations', [])
    last = rows[-1] if rows else {}
    from bioma.harness.execution_view import computation_packages
    completed = computation_packages(context)
    latest = next(reversed(completed.values()), {})
    recovery = recovery_input_catalog(context)
    phase = ('repair' if last.get('status') == 'error' else
             'review' if state.get('delivery_review_rejected') else
             'computation_available' if latest else 'recover_computation' if recovery else 'observe')
    present = set()
    recent_skills = set()
    for item in recent_messages:
        if item.get('role') != 'tool':
            continue
        try:
            value = json.loads(item.get('content', ''))
            present.update(t['function']['name'] for t in value.get('metrics', {}).get('tools', []))
            if value.get('tool_name')=='skill_view':
                recent_skills.add(value.get('metrics',{}).get('metadata',{}).get('stable_skill_id'))
        except (ValueError, TypeError, KeyError):
            pass
    metrics = latest.get('metrics', {})
    sources, failures = observed_sources_and_failures(context)
    from bioma.harness.policy import investigation_delivery_readiness
    readiness = investigation_delivery_readiness(context)
    memory = {'operational_phase': phase, 'phase_is_scientific_validation': False,
        'delivery_readiness': readiness,
        'original_request': state.get('original_user_request') or state.get('current_user_request'),
        'required_deliverables': state.get('desired_deliverables', []),
        'execution_requirements': state.get('execution_requirements', {}),
        'research_assignment': state.get('research_assignment', {}),
        'clarification_reply': state.get('clarification_reply'),
        'observed_source_catalog': sources, 'failed_actions': failures,
        'unqualified_partial_execution_inputs': state.get('admitted_execution_inputs', {}),
        'same_query_recovery_computations':recovery,
        'inputs': [{'index': i, 'path': p} for i,p in enumerate(state.get('raw_data_sources', []))],
        'completed_tool_counts': dict(Counter(r.get('tool_name') for r in rows if r.get('status') == 'ok')),
        'last_input_inspection': next(({'artifacts':r.get('artifacts', []),
            'preview':bounded_text(json.dumps(r.get('metrics', {}).get('input_previews', {}),ensure_ascii=False),3000),
            'r_runtime':r.get('metrics', {}).get('r_runtime'),
            'python_runtime':r.get('metrics', {}).get('python_runtime')}
            for r in reversed(rows) if r.get('tool_name')=='inspect_inputs' and r.get('status')=='ok'), None),
        'skills_discovered': [s for r in rows if r.get('tool_name') in {'skills_list','taxonomy_skills'}
            and r.get('status')=='ok' and r.get('metrics',{}).get('metadata',{}).get('success')
            for s in r['metrics']['metadata'].get('skills',[])][-32:],
        'skills_read': skill_reads(context),
        'read_skill_guidance_not_in_recent_feedback': [
            {'skill_id':key, 'metadata_sha256':digest(meta), 'content':bounded_text(meta.get('content',''),4000),
             'scope':'Previously read guidance, not evidence or execution authority; full content remains in the archive.'}
            for key,meta in list({r.get('metrics',{}).get('metadata',{}).get('stable_skill_id'):r['metrics']['metadata']
                for r in rows if r.get('tool_name')=='skill_view' and r.get('status')=='ok'
                and r.get('metrics',{}).get('metadata',{}).get('success')}.items())[-2:]
            if key and key not in recent_skills],
        'tool_schemas_not_in_recent_feedback': {k:v for k,v in state.get('discovered_tool_schemas', {}).items() if k not in present},
        'notes_unverified': bounded_text(json.dumps(state.get('investigation_notes', {}),ensure_ascii=False)),
        'last_error': bounded_text(last.get('summary',''),2000) if last.get('status')=='error' else None,
        'resume_from': {'receipt': metrics.get('receipt_path'), 'artifacts': latest.get('artifacts', []),
            'summary_status': metrics.get('summary_status'),
            'published_summary': metrics.get('summary'),
            'diagnostic_preview': metrics.get('diagnostic_preview'),
            'source_programs': latest.get('patch',{}).get('analysis_code_paths', []),
            'scope': 'Successful execution only; reuse actual saved artifacts, not unsaved process memory.'},
        'computation_catalog': [{'receipt':ref, 'artifacts':r.get('artifacts',[]),
            'summary_status':r.get('metrics',{}).get('summary_status')} for ref,r in completed.items()],
        'archive': str(context.artifact_dir()),
        'instruction': 'Receipt-bound failed requests and observed download links are represented in work_facts with their exact identities and observed outcomes. An access challenge is not missing tool permission. Use the latest successful inspection and its archived source as the next starting point. '
            'Failed execution files listed as unqualified_partial_execution_inputs are now INPUT_FILES; inspect and reuse them in a new successful computation rather than rerunning expensive completed stages. They cannot be submitted as completed results. Persist reusable intermediate data as declared output files. A new process cannot reuse unsaved variables. '
            'When same_query_recovery_computations is present, inspect its saved summaries and reuse version-consistent statistics and figures for downstream report repair. Do not repeat completed expensive calculations unless the files fail checks or a changed scientific design requires it; explain that reason. Earlier completion verdicts are not inherited. '
            'Do not reread diagnostics already visible here. Full history remains available through read_workspace.'}
    work_facts = work_fact_projection(context, readiness=readiness)
    if (work_facts['recorded_observations'] or work_facts['unmet']
            or work_facts['next_decision_information']['failed_requests']):
        memory['work_facts'] = work_facts
    return memory


def _trace_entries(context):
    trace = context.artifact_dir('tool_calls') / 'trace.jsonl'
    if not trace.exists():
        return []
    entries = []
    for line in trace.read_text().splitlines():
        try:
            entry = json.loads(line)
            if isinstance(entry, dict) and isinstance(entry.get('observation'), dict):
                entries.append(entry)
        except (TypeError, ValueError):
            continue
    return entries


def _receipt_ref(observation):
    return (observation.get('metrics') or {}).get('receipt_path')


def _actual_tool_and_arguments(entry):
    tool = entry.get('tool')
    arguments = entry.get('arguments') or {}
    if tool == 'invoke_scientific_tool':
        return arguments.get('tool_name'), arguments.get('arguments') or {}
    return tool, arguments


def _request_identity(tool, arguments):
    allowed = ('url', 'accession', 'max_bytes', 'query', 'organism', 'start_year', 'end_year')
    return {'tool': tool, **{key: arguments[key] for key in allowed if key in arguments}}


def work_fact_projection(context, *, readiness=None):
    """Project source-labelled observations without asserting receipt verification."""
    state = context.state
    entries = _trace_entries(context)
    official_catalogs = {}
    acquired_resources = {}
    diagnostic_gaps = {}
    for entry in entries:
        tool, arguments = _actual_tool_and_arguments(entry)
        observation = entry['observation']
        metrics = observation.get('metrics') or {}
        receipt = _receipt_ref(observation)
        result = metrics.get('result') or {}
        if receipt and observation.get('status') == 'ok' and tool == 'inspect_geo_dataset':
            dataset = result.get('dataset') or {}
            accession = dataset.get('accession') or arguments.get('accession')
            if not accession:
                continue
            official_catalogs[accession] = {
                'kind': 'official_dataset_file_catalog',
                'status': 'recorded_tool_observation',
                'dataset': {key: dataset.get(key) for key in (
                    'accession', 'title', 'organism', 'sample_count', 'publication_date',
                    'repository_url') if dataset.get(key) is not None},
                'candidate_resources': [{
                    'name': row.get('name'),
                    'role': row.get('role'),
                    'url': row.get('url'),
                    'size_bytes': row.get('size_bytes'),
                    'scientific_status': 'candidate_unverified_for_requested_use',
                } for row in result.get('resources') or [] if isinstance(row, dict)],
                'qualification': result.get('qualification'),
                'source_receipt_ref': receipt,
                'source_receipt_status': 'referenced_not_revalidated_by_projection',
                'evidence_status': 'not_evidence',
                'scope': 'Official repository metadata; file role and scientific applicability remain unverified unless separately checked.',
            }
        if receipt and observation.get('status') == 'ok' and tool == 'read_public_resource' and result.get('files'):
            key = result.get('requested_url') or digest(result.get('files'))
            acquired_resources[key] = {
                'kind': 'acquired_public_resource',
                'status': 'recorded_tool_observation',
                'requested_url': result.get('requested_url'),
                'resolved_url': result.get('resolved_url'),
                'bytes': result.get('bytes'),
                'files': result.get('files'),
                'source_receipt_ref': receipt,
                'source_receipt_status': 'referenced_not_revalidated_by_projection',
                'file_identity_status': 'recorded_tool_result_not_rehashed_by_projection',
                'evidence_status': 'not_evidence',
                'scientific_status': 'retrieved_bytes_unqualified',
                'scope': result.get('qualification') or 'Retrieved bytes, not scientific evidence.',
            }
        diagnostic = metrics.get('diagnostic_context') or {}
        for condition in diagnostic.get('unmet_conditions') or [] if receipt else []:
            key = digest({'condition': condition, 'receipt': receipt})
            diagnostic_gaps[key] = {
                'kind': 'diagnostic_unmet_condition',
                'condition': condition,
                'status': 'unmet',
                'source_receipt_ref': receipt,
                'source_receipt_status': 'referenced_not_revalidated_by_projection',
                'evidence_status': 'not_evidence',
            }

    if readiness is None:
        from bioma.harness.policy import investigation_delivery_readiness
        readiness = investigation_delivery_readiness(context)
    contract = {
        'kind': 'original_task_contract',
        'task_id': state.get('task_id'),
        'query_control_id': ((state.get('gateway') or {}).get('query_control_record') or {}).get('id'),
    }
    contract['source_ref'] = contract['task_id'] or contract['query_control_id']
    delivery_status = 'fulfilled' if state.get('delivery_complete') else (
        'pending_independent_review' if readiness.get('ready_for_review') else 'unmet'
    )
    obligations = [{
        'kind': 'contract_deliverable',
        'name': name,
        'status': delivery_status,
        'source': contract,
    } for name in readiness.get('expected_artifacts') or state.get('desired_deliverables') or []]
    budget = getattr(context, 'investigation_budget', None)
    remaining_budget = ({
        'model_calls': budget.remaining_llm_calls,
        'tool_calls': budget.remaining_tool_calls,
    } if budget is not None else {'model_calls': None, 'tool_calls': None})
    _, failures = observed_sources_and_failures(context)
    failures = [{**row,
        'source_receipt_status': 'referenced_not_revalidated_by_projection',
        'evidence_status': 'not_evidence'}
        for row in failures if row.get('source_receipt_refs')]
    return {
        'projection_schema': 'bioma.work_facts.v2',
        'recorded_observations': [*list(official_catalogs.values())[-6:],
                                  *list(acquired_resources.values())[-6:]],
        'verified_file_identities': {
            'status': 'not_assessed_by_projection', 'items': []},
        'qualified_scientific_support': {
            'status': 'not_assessed_by_projection', 'items': []},
        'unmet': [*obligations, *list(diagnostic_gaps.values())[-8:]],
        'next_decision_information': {
            'official_resource_facts': [{
                'accession': row['dataset'].get('accession'),
                'source_receipt_ref': row['source_receipt_ref'],
            } for row in list(official_catalogs.values())[-6:]],
            'failed_requests': failures,
            'remaining_budget': remaining_budget,
        },
        'decision_authority': {
            'agent_selects_next_action': True,
            'runtime_selects_alternative_resource': False,
            'new_permission_granted': False,
            'scientific_success_asserted': bool(state.get('delivery_complete')),
            'scope': 'Read-only projection of recorded Tool Observations and the original contract. Receipt references are source labels only: this projection does not traverse the Ledger, rehash receipts/files, qualify scientific support, grant authority, or create Evidence.',
        },
    }


def capability_events(entry, sequence):
    """Deterministic projections of actual trace events; no inferred causal credit."""
    name, args, obs = entry['tool'], entry['arguments'], entry['observation']
    meta = obs.get('metrics', {}).get('metadata', {})
    base = {'trace_sequence': sequence, 'trace_sha256': digest(entry),
            'contribution_claim': 'Observed use/explicit selection only; causal scientific benefit not established.'}
    events = []
    def add(kind, **fields):
        event = {**base, 'event': kind, **fields}
        event['event_id'] = digest(event)
        events.append(event)
    if name == 'discover_scientific_capabilities' and obs.get('status') == 'ok':
        for row in obs.get('metrics', {}).get('capabilities', []):
            add('CapabilityDiscovered', capability=row['capability'], method_id=row['method_id'],
                authority='discovery_only')
    if name == 'request_scientific_capability':
        result = obs.get('metrics', {})
        add('MethodSelected', method=args.get('method_id'), capability=args.get('capability'),
            explicit_skill_refs=args.get('skill_refs', []), arguments_sha256=digest(args))
        add('CapabilityRequestObserved', status=result.get('status', 'error'),
            ledger_event_ref=result.get('ledger_event_ref'), reused=result.get('reused', False))
        if result.get('status') == 'reviewed' and result.get('outcome') == 'completed':
            add('ResultReused' if result.get('reused') else 'ResultProduced',
                work_result_refs=[r['id'] for r in result.get('work_results', [])],
                ledger_event_ref=result.get('ledger_event_ref'), result_status='unqualified')
    if name in {'skills_list','taxonomy_skills'} and meta.get('success'):
        for skill in meta.get('skills', []):
            add('SkillDiscovered', skill_id=skill.get('stable_skill_id') or skill['name'], name=skill['name'])
    if name == 'skill_view':
        add('SkillRead' if obs.get('status')=='ok' and meta.get('success') else 'SkillReadFailed',
            skill_id=meta.get('stable_skill_id') or args.get('name'), metadata_sha256=digest(meta))
    partial = obs.get('metrics', {}).get('retained_partial_outputs')
    if partial:
        add('PartialOutputsRetained', files=partial['files'], manifest_path=partial['manifest_path'],
            result_status='failed_execution_unqualified')
    if name in {'invoke_scientific_tool','execute_analysis_code','revise_analysis_code'}:
        method = args.get('tool_name') or ('code:'+args.get('engine','retained_draft' if name.startswith('revise') else 'python'))
        libraries = re.findall(r'(?:library|require)\(\s*[\"\']?([A-Za-z][A-Za-z0-9_.]*)', args.get('code',''))
        add('MethodSelected', method=method, source_libraries=sorted(set(libraries)),
            source_libraries_scope='Static selected program, not proof each package or method executed.',
            selection_kind='explicit_tool_or_language_choice',
            explicit_skill_refs=args.get('skill_refs',[]), arguments_sha256=digest(args))
        add('ToolExecuted' if obs.get('status')=='ok' else 'ToolCallFailed', tool=method,
            receipt=obs.get('metrics',{}).get('receipt_path'), status=obs.get('status'))
        files = obs.get('metrics',{}).get('sha256',{})
        result = obs.get('metrics',{}).get('result',{})
        if isinstance(result,dict):
            files = files or result.get('files',{})
        if obs.get('status')=='ok' and files:
            add('ResultProduced', files=files, result_status='unqualified',
                receipt=obs.get('metrics',{}).get('receipt_path'))
    return events


def observed_sources_and_failures(context):
    """Retain retrieved link identities and failed actions, not guessed next URLs."""
    from pathlib import Path
    sources=[]
    for row in context.state.get('harness_observations', []):
        result=row.get('metrics',{}).get('result',{})
        if row.get('status')!='ok' or not isinstance(result,dict) or not result.get('requested_url'):
            continue
        text=result.get('preview') or ''
        for filename,expected in result.get('files',{}).items():
            path=Path(filename)
            if (path.is_file() and path.stat().st_size<=1_000_000 and
                    ('text' in result.get('content_type','') or path.suffix.lower() in {'.md','.rmd','.html','.txt','.json'})):
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()==expected:
                    text=raw.decode('utf-8',errors='replace')
        links=list(dict.fromkeys(u.rstrip(').,;]') for u in re.findall(r'https://[^\s<>"\']+',text)))
        from html.parser import HTMLParser
        from urllib.parse import urljoin, urlsplit
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag != 'a': return
                href = dict(attrs).get('href')
                if href:
                    try:
                        url = urljoin(result.get('resolved_url') or result['requested_url'], href)
                        if urlsplit(url).scheme == 'https': links.append(url)
                    except ValueError:
                        pass  # Malformed links are untrusted source text, not a loop failure.
        Links().feed(text)
        links = list(dict.fromkeys(links))
        links.sort(key=lambda u: -sum(token in u.lower() for token in ('download','dataset','suppl','.csv','.rda','.rds','.gz')))
        sources.append({'url':result['requested_url'],'files':list(result.get('files',{})),
            'observed_https_links':links[:16], 'links_omitted':max(0,len(links)-16),
            'receipt':row.get('metrics',{}).get('receipt_path'),
            'preview':bounded_text(result.get('preview') or '',600),
            'scope':'Retrieved source text/links, not measured data or scientific conclusions.'})
    failures={}
    for entry in _trace_entries(context):
        obs=entry['observation']
        if obs.get('status')!='error':continue
        tool,args=_actual_tool_and_arguments(entry)
        key=digest({'tool':tool,'args':args})
        previous=failures.pop(key,{})
        metrics=obs.get('metrics') or {}
        diagnostic=metrics.get('diagnostic_context') or {}
        error=(metrics.get('result') or {}).get('error') or {}
        receipt=_receipt_ref(obs)
        backend_retryable=diagnostic.get('retryable',error.get('retriable'))
        retry_scope=diagnostic.get('retry_assessment')
        if retry_scope is None:
            from bioma.harness.diagnostics import retry_assessment
            retry_scope=retry_assessment(bool(backend_retryable))
        failures[key]={'tool':tool,
            'url':args.get('url'), 'request_identity':_request_identity(tool,args),
            'arguments_sha256':digest(args),
            'attempts':previous.get('attempts',0)+1,
            'observed_outcome':{'status':'error',
                'error_type':error.get('error_type') or next((row.get('error_type')
                for row in diagnostic.get('observed_facts') or []
                if row.get('kind')=='tool_error'),None)},
            'automatic_replay_assessment':retry_scope,
            'failure_domain':diagnostic.get('failure_domain'),
            'failure_stage':diagnostic.get('failure_stage'),
            'unmet_conditions':diagnostic.get('unmet_conditions') or [],
            'source_receipt_refs':list(dict.fromkeys([
                *previous.get('source_receipt_refs',[]), *([receipt] if receipt else [])])),
            'feedback':bounded_text(obs.get('summary',''),900)}
    by_url = {source['url']:source for source in sources}
    return list(by_url.values())[-8:], list(failures.values())[-8:]
