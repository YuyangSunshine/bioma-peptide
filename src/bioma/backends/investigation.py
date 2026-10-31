"""Continuous, non-authoritative investigation on the existing analysis harness.

The work package is admitted once by WP1. Scripts, observations and working
notes are execution history, not ScientificState or accepted scientific claims.
"""
from dataclasses import asdict
from copy import deepcopy
from pathlib import Path
from typing import Literal
import hashlib
import json
import os

from pydantic import Field

from bioma.harness.service import DataAnalysisRunner
from bioma.harness.types import HarnessTool, HarnessToolInput, ToolObservation
from bioma.harness.code_analysis import (
    AnalysisCodeInput, AnalysisRevisionInput, execute_analysis_code, revise_analysis_code,
)
from bioma.core.models import Artifact
from bioma.core.scientific_capabilities import COMPUTE, WorkResult
from bioma.backends.base import BackendIncomplete


class InvestigationWorkPackage(HarnessToolInput):
    """Immutable execution inputs/limits; not a new scientific authority."""
    task_id: str
    workspace_scope: str
    objective: str
    original_request: str = ""
    execution_requirements: dict = Field(default_factory=dict)
    input_manifest: dict[str, str]
    deliverables: list[str]
    permitted_tools: list[str]
    analysis_work: dict | None = None
    query_input_questions: list[dict] = Field(default_factory=list)
    research_intake: dict = Field(default_factory=dict)
    max_model_requests: int
    max_tool_calls: int


class HistoryQuery(HarnessToolInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=30)
    query: str = ''


class ReadWorkspace(HarnessToolInput):
    path: str = Field(description='File inside this work package artifact workspace, not an input file. Read admitted inputs with inspect_input_data using input_index.')
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=100, ge=1, le=300)


class WorkingNotes(HarnessToolInput):
    findings: list[str] = Field(default_factory=list)
    method: str = ''
    unresolved: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class ClarificationRequest(HarnessToolInput):
    kind: Literal['semantic_choice', 'input_unavailable', 'tool_unavailable'] = 'semantic_choice'
    alternatives: list[str] = Field(default_factory=list, description='For a material scientific choice, at least two concrete alternatives supported by observed inputs/resources. A request for an example dataset authorizes choosing a suitable public example.')
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Advice(HarnessToolInput):
    question: str
    scientific_context: str


class Finish(HarnessToolInput):
    reason: str
    receipt_refs: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    answer_candidate_ref: str | None = None
    answer_text: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    unresolved_requirements: list[str] = Field(default_factory=list)


class ExecutionViewQuery(HarnessToolInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=50)


def inspect_execution_records(context, args):
    from bioma.harness.execution_view import execution_view
    view = execution_view(context)
    records = view.pop('records')
    return ToolObservation('inspect_execution_records', 'ok',
        'Native execution identities and check prerequisites; not scientific approval.',
        metrics={**view, 'total':len(records),
                 'records':records[args['offset']:args['offset']+args['limit']]})


def read_workspace(context, args):
    root = context.artifact_dir().resolve()
    path = (root / args['path']).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        # Do not echo the rejected path, which can contain private identifiers.
        raise ValueError(
            'WORKSPACE_SCOPE_VIOLATION: read_workspace reads only this work package artifact files. '
            'For admitted inputs use inspect_input_data with input_index from inspect_inputs; '
            'for earlier execution records use retrieve_history. No file was read.'
        ) from None
    if not path.is_file():
        return ToolObservation('read_workspace', 'error',
            'Expected an existing regular file; the supplied path is a directory or missing.',
            metrics={'path': str(path)})
    lines = path.read_text(encoding='utf-8').splitlines()
    start = args['start_line'] - 1
    return ToolObservation('read_workspace', 'ok', 'Read archived research material; not new evidence.',
        metrics={'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                 'line_count': len(lines), 'numbered_lines': '\n'.join(
                     f'{i+1}: {line}' for i, line in enumerate(lines[start:start+args['max_lines']], start))})


def retrieve_history(context, args):
    path = context.artifact_dir('tool_calls') / 'trace.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    rows = [row for row in rows if args['query'].casefold() in json.dumps(row).casefold()]
    return ToolObservation('retrieve_history', 'ok', 'Retrieved original execution records.',
        metrics={'total': len(rows), 'records': rows[args['offset']:args['offset']+args['limit']]})


def _skill_observation(metadata, args):
    result = metadata.func(**args)
    return ToolObservation(metadata.name, 'ok' if result.get('success') else 'error',
        'Skill metadata read; not evidence.' if result.get('success') else
        str(result.get('error')) + '; use the discovered skill name without an invented file_path.',
        metrics={'metadata': result})


def save_notes(context, args):
    return ToolObservation('save_working_notes', 'ok', 'Saved unverified working notes.',
                          patch={'investigation_notes': args})


def request_clarification(context, args):
    """Suspend only after scoped input observation, preserving completed work."""
    inspected = [row for row in context.state.get('harness_observations', [])
                 if row.get('status') == 'ok' and row.get('tool_name') in {
                     'inspect_inputs', 'inspect_tabular_schema', 'inspect_input_data'}]
    if not inspected:
        raise ValueError(
            'Clarification requires a successful scoped input inspection first. '
            'An unobserved Query hypothesis is not a verified ambiguity.'
        )
    if context.state.get('public_resource_discovery'):
        observations = context.state.get('harness_observations', [])
        resource_attempts = [row for row in observations if row.get('tool_name') == 'read_public_resource'
                             or row.get('metrics', {}).get('requested_tool') == 'read_public_resource']
        resource_observed = bool(resource_attempts)
        if not resource_observed:
            raise ValueError('Inspect the requested public resource or an example-data source first with '
                             'find_scientific_tools / invoke_scientific_tool. Not yet located is not missing. '
                             'For a material choice, record the available alternatives from source metadata.')
        if args.get('kind', 'semantic_choice') == 'semantic_choice':
            if len(set(args.get('alternatives', []))) < 2:
                raise ValueError('A material scientific ambiguity requires at least two observed alternatives. '
                                 'Locating inputs or selecting an example is routine authorized work; continue discovery.')
            observed_material = json.dumps([row.get('metrics', {}) for row in observations
                if row.get('status') == 'ok'], ensure_ascii=False).casefold()
            if not all(value.casefold() in observed_material for value in args.get('alternatives', [])):
                raise ValueError('Clarification alternatives must be concrete values actually present in observed '
                                 'input/resource metadata, not operational choices such as continue vs upload.')
        else:
            if args.get('kind') == 'tool_unavailable' and resource_attempts:
                failure = resource_attempts[-1]
                detail = failure.get('summary', '')
                if not any(marker in detail for marker in (
                    'Unknown configured scientific tool', 'No scoped ToolGrant authorizes',
                    'Tool was removed or replaced', 'Tool implementation differs from grant')):
                    raise ValueError('Resource access failure is not missing tool authorization. '
                        'The registered resource tool was callable; inspect its actual error and '
                        'follow documented alternative endpoints instead of asking for download permission.')
            if not resource_attempts or resource_attempts[-1].get('status') != 'error':
                raise ValueError('Input unavailability requires a concrete failed resource access. '
                                 'Successful metadata discovery is a reason to follow its data links, not ask for upload.')
    identities = []
    for row in inspected:
        metrics = row.get('metrics') or {}
        if metrics.get('sha256'):
            identities.append({'filename': metrics.get('filename'),
                               'sha256': metrics['sha256']})
        for item in (metrics.get('input_previews') or {}).get('files', []):
            if item.get('sha256'):
                identities.append({'filename': item.get('filename'),
                                   'sha256': item['sha256']})
    request = {**args, 'status': 'waiting_user',
               'inspection_count': len(inspected),
               'inspected_inputs': list({json.dumps(row, sort_keys=True): row
                                         for row in identities}.values())}
    return ToolObservation('request_user_clarification', 'ok',
        'Recorded a scoped clarification request after input inspection; '
        'completed computations and artifacts remain provisional.',
        metrics={'clarification_request': request},
        patch={'need_clarification': True, 'pending_question': args['question'],
               'clarification_request': request, 'next_action': 'ask_user'})


def compute_step(context, args, *, revise=False):
    # Intermediate work need not pretend to have covered the final contract.
    from bioma.harness.recovery import FailedAnalysisExecution, retain_partial_outputs, verify_partial_inputs
    recovery_provenance = verify_partial_inputs(context.state.get('admitted_execution_inputs', {}))
    # Existing code policy, input hashing, Python/R runners and receipt checks apply.
    keys = ('desired_deliverables', 'research_assignment', 'investigation_intermediate',
            'delivery_complete', 'raw_data_sources', 'artifact_reuse_sources')
    saved = {key: context.state.get(key) for key in keys}
    if not revise and args.get('output_files') is None:
        # An invocation owns its step declaration. Final obligations belong to
        # finish(), never to implicit defaults on every exploratory computation.
        args = {**args, 'output_files': []}
    from bioma.core.reporting import assignment_needs_pdf
    if assignment_needs_pdf(context.state):
        draft = (context.analysis_draft or {}) if revise else {}
        layouts = args.get('reports') if args.get('reports') is not None else draft.get('reports', {})
        names = list(args.get('output_files') if args.get('output_files') is not None else draft.get('output_files') or [])
        for name in layouts or {}:
            paired = str(Path(name).with_suffix('.pdf'))
            if paired not in names:
                names.append(paired)
        if layouts:
            args = {**args, 'output_files': names}
    catalog = list(context.state.get('raw_data_sources', []))
    indices = (context.analysis_draft or {}).get('input_indices') if revise else args.get('input_indices')
    reuse = ((context.analysis_draft or {}).get('reuse_inputs') or {}) if revise else (args.get('reuse_inputs') or {})
    if indices is not None:
        if any(type(i) is not int or i < 0 or i >= len(catalog) for i in indices):
            raise ValueError('Input selection must reference existing admitted input indices')
        if len(set(indices)) != len(indices):
            raise ValueError('Input selection must not contain duplicate indices')
        context.state['raw_data_sources'] = [catalog[i] for i in indices]
    if reuse:
        if indices is None:
            raise ValueError('input_indices is required with reuse_inputs to separate copy sources from program inputs')
    context.state['artifact_reuse_sources'] = catalog
    context.state.update(desired_deliverables=[], research_assignment={}, investigation_intermediate=True)
    context.state.pop('delivery_complete', None)
    from uuid import uuid4
    attempt = context.artifact_dir('script_versions') / (uuid4().hex + '.json')
    attempt.write_text(json.dumps({'arguments': args, 'previous_draft': context.analysis_draft}, ensure_ascii=False))
    try:
        observation = (revise_analysis_code if revise else execute_analysis_code)(context, args)
    except FailedAnalysisExecution as exc:
        retain_partial_outputs(context, exc)
        raise
    finally:
        for key, value in saved.items():
            if value is None:
                context.state.pop(key, None)
            else:
                context.state[key] = value
        # A failed attempt never becomes a result, but it must not erase an
        # immutable earlier computation in another version directory. finish()
        # rechecks its hashes and the independent review sees all failed attempts.
        # Delivery authority is always invalidated; computation history is not.
        context.state.pop('delivery_complete', None)
        context.state['raw_data_sources'] = list(dict.fromkeys([
            *context.state.get('raw_data_sources', []), *context.state.get('admitted_execution_inputs', {})]))
    from bioma.harness.investigation_memory import bounded_text
    receipt_data = json.loads(Path(observation.metrics['receipt_path']).read_text())
    stdout = receipt_data.get('process', receipt_data.get('execution', {})).get('stdout', '')
    observation.metrics['diagnostic_preview'] = bounded_text(stdout)
    observation.metrics['execution_sha256'] = {**recovery_provenance, **{path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in [observation.metrics['receipt_path'], *observation.patch.get('analysis_code_paths', [])]}}
    observation.metrics['script_version'] = str(attempt)
    observation.summary = ('Executed intermediate computation. Summary: '
                           + observation.metrics.get('summary_status', 'published')
                           + '. Scientific interpretation is unqualified.')
    from bioma.runtime.tool_actions import current_tool_action
    observation.metrics['dispatch'] = current_tool_action()
    observation.metrics['execution_arguments'] = {
        key:value for key,value in args.items() if key != 'code'}
    observation.execution_package = asdict(observation)
    observation.patch['delivery_complete'] = False
    return observation


def finish(context, args):
    from bioma.harness.recovery import verify_partial_inputs
    verify_partial_inputs(context.state.get('admitted_execution_inputs', {}))
    from bioma.harness.execution_view import delivery_support_packages
    catalog = delivery_support_packages(context)
    args = dict(args)
    from bioma.harness.result_proposal import ResultProposal, retain
    candidate_ref = args.get('answer_candidate_ref')
    if (candidate_ref
            and candidate_ref not in context.state.get('final_answer_candidates', {})):
        if (args.get('answer_text') is None and not args.get('assumptions')
                and not args.get('unresolved_requirements')):
            raise ValueError(
                'answer_candidate_ref must identify a retained ResultProposal, not a '
                'computation receipt; supply answer_text to create a proposal.'
            )
        args.pop('answer_candidate_ref')
    if not args.get('answer_candidate_ref') and (args.get('answer_text') is not None
            or args.get('assumptions') or args.get('unresolved_requirements')):
        args['answer_candidate_ref'] = retain(context, ResultProposal(
            answer_text=args.get('answer_text'), support_receipt_refs=args.get('receipt_refs') or [],
            artifact_refs=args.get('artifact_paths') or [], assumptions=args.get('assumptions') or [],
            unresolved_requirements=args.get('unresolved_requirements') or []),
            available=catalog, selection='model_explicit' if args.get('receipt_refs') else 'unselected')
    raw = next(reversed(catalog.values()), None)
    if not raw:
        raise ValueError('Explicit receipt selection required: no current successful support to submit; retain work first.')
    selected = args.get('receipt_refs')
    if not selected:
        from bioma.harness.policy import investigation_delivery_readiness
        candidates = investigation_delivery_readiness(context)['reviewable_support']
        if len(candidates) > 1:
            raise ValueError(
                'Explicit receipt selection required: Multiple computations satisfy the file contract; select receipt_refs explicitly. '
                'The runtime will not choose a scientific result by recency.'
            )
        if not candidates and len(catalog) != 1:
            raise ValueError('Explicit receipt selection required: no contract-ready support; retain work and inspect missing obligations.')
        # A sole record can be checked for precise missing obligations. This
        # never chooses among multiple versions by recency or marks it ready.
        selected = [candidates[0]['receipt_ref']] if candidates else list(catalog)
    if len(set(selected)) != len(selected) or any(ref not in catalog for ref in selected):
        raise ValueError('Select unique receipts from this admitted work package')
    observations = [ToolObservation(**deepcopy(catalog[ref])) for ref in selected]
    expected = set(context.state.get('desired_deliverables') or [])
    requirements = context.state.get('execution_requirements') or {}
    summary_required = bool(requirements.get('summary_required')) or not expected
    if summary_required and not any(isinstance(item.metrics.get('summary'), dict)
                                    and item.metrics['summary'] for item in observations):
        raise ValueError('Selected computation has no published summary required by this work package. '
                         'Publish actual findings; execution alone is not a scientific conclusion.')
    admitted = {str(Path(p).resolve()): hashlib.sha256(Path(p).read_bytes()).hexdigest()
                for p in context.state.get('raw_data_sources', [])}
    for item in observations:
        if item.metrics.get('support_kind') == 'professional_method':
            from bioma.runtime.capability_requests import delivery_result
            delivery_result(item.metrics['receipt_path'],
                item.metrics['native_receipt_hashes'][item.metrics['receipt_path']])
            continue  # Native child scope is checked by Runtime, not parent Shell scope.
        if ((not item.metrics['input_sha256'] and item.metrics.get('support_kind') != 'retrieval') or any(admitted.get(p) != h
                for p, h in item.metrics['input_sha256'].items())):
            raise ValueError('Selected computation input scope/version changed')
        for filename, digest in {**item.metrics['sha256'], **item.metrics['execution_sha256']}.items():
            Path(filename).resolve().relative_to(context.artifact_dir().resolve())
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest:
                raise ValueError('Executed artifact changed before submission: ' + filename)
    retrieval_only = all(item.metrics.get('support_kind') == 'retrieval' for item in observations)
    if retrieval_only and (requirements.get('engine') or
                           set(requirements.get('operations') or []) - {'retrieval'}):
        raise ValueError('Retrieval alone does not satisfy the required execution method/engine/operations')
    if retrieval_only:
        from bioma.harness.result_proposal import load
        proposed = (load(context, args['answer_candidate_ref']).get('text')
                    if args.get('answer_candidate_ref') else args.get('answer_text'))
        if not isinstance(proposed, str) or not proposed.strip():
            raise ValueError('Source-only delivery requires an answer proposal; a receipt alone is not a user-facing answer')
    selected_inputs = {p: h for item in observations for p, h in item.metrics['input_sha256'].items()}
    observation = observations[0]
    requested = args.get('artifact_paths') or [p for item in observations for p in item.artifacts]
    known = {p for item in observations for p in item.artifacts}
    if len(set(requested)) != len(requested) or set(requested)-known:
        raise ValueError('Selected artifacts must be unique outputs of selected receipts')
    from bioma.harness.checkpoint import selected_review_receipt
    checkpoint = getattr(context, 'investigation_checkpoint', None)
    reused_package = False
    if selected_review_receipt() and checkpoint is not None and checkpoint.path.is_file():
        saved = json.loads(checkpoint.path.read_text())['observation']
        metrics = saved['metrics']
        if (metrics['receipt_path'] == selected_review_receipt()
                and metrics.get('selected_support_receipt_refs') == selected
                and metrics.get('answer_candidate_ref') == args.get('answer_candidate_ref')):
            observation = ToolObservation(**saved)
            reused_package = True
    native_hashes = {ref:identity for item in observations
                     for ref,identity in item.metrics.get('native_receipt_hashes', {}).items()}
    if not reused_package and (len(observations)>1 or args.get('artifact_paths') or native_hashes):
        # This is a bound delivery manifest, never a fabricated execution receipt.
        import shutil
        from uuid import uuid4
        folder=context.artifact_dir('delivery_packages',uuid4().hex)
        output=folder/'outputs';output.mkdir()
        names={}
        for item in observations:
            for filename in item.artifacts:
                if filename not in requested:continue
                if item.metrics.get('support_kind') == 'professional_method':
                    name = Path(item.metrics['artifact_names'][filename])
                    if name.is_absolute() or '..' in name.parts:
                        raise ValueError('Invalid native artifact relative name')
                else:
                    name=Path(filename).relative_to(Path(item.metrics['receipt_path']).parent/'outputs')
                if name.as_posix() in names:raise ValueError('Conflicting selected artifact names')
                names[name.as_posix()]=filename
        paths=[]
        for name,filename in names.items():
            dest=output/name;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(filename,dest);paths.append(str(dest))
        provenance={p:h for item in observations for p,h in
                    {**item.metrics['execution_sha256'], **{p:item.metrics['sha256'][p] for p in item.artifacts if p in requested}}.items()}
        manifest=folder/'delivery_manifest.json'
        manifest.write_text(json.dumps({'kind':'selected_execution_package','receipts':selected,
            'selected_outputs':names,'source_sha256':provenance,
            'native_receipt_hashes':native_hashes},sort_keys=True))
        provenance[str(manifest)]=hashlib.sha256(manifest.read_bytes()).hexdigest()
        codes=[p for item in observations for p in item.patch.get('analysis_code_paths',[])]
        observation=ToolObservation('finish_investigation','ok',args['reason'],artifacts=paths,
            metrics={'receipt_path':str(manifest),'sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
                     'input_sha256':selected_inputs,'runtime':observations[0].metrics['runtime'],
                     'execution_sha256':provenance,'selected_execution_sha256':provenance},
            patch={'analysis_code_paths':codes,'execution_receipts':selected,
                   'analysis_results':{'analysis_outputs':{'code_analysis':paths}}})
    # Runtime-derived selected origins. These are not Evidence or model labels.
    observation.patch['result_basis'] = [{
        'receipt_ref': ref,
        'receipt_sha256': (item.metrics.get('native_receipt_hashes') or item.metrics['execution_sha256'])[ref],
        'acquisition': item.metrics.get('support_kind', 'computation'),
        'scientific_support': 'not_assessed',
        **({'native_work_results':item.metrics['native_work_results']}
           if item.metrics.get('support_kind') == 'professional_method' else {}),
    } for ref, item in zip(selected, observations)]
    observation.metrics['selected_support_kinds'] = [item.metrics.get('support_kind', 'computation') for item in observations]
    observation.metrics['native_receipt_hashes'] = native_hashes
    observation.metrics['retrieval_only'] = retrieval_only
    # Supplied input files remain mandatory review material even for retrieval.
    if retrieval_only:
        observation.metrics['input_sha256'] = admitted
    observation.patch['selected_input_manifest'] = {p: h for item in observations
        for p, h in item.metrics['input_sha256'].items()}
    observation.patch['selected_delivery_paths']=list(dict.fromkeys([
        *observation.artifacts,*observation.patch.get('analysis_code_paths',[]),
        *[ref for ref in selected if ref not in native_hashes],observation.metrics['receipt_path']]))
    expected = set(context.state.get('desired_deliverables') or [])
    output_dir = Path(observation.metrics['receipt_path']).parent / 'outputs'
    output_names = {path: Path(path).relative_to(output_dir).as_posix() for path in observation.artifacts}
    actual = set(output_names.values())
    observation.patch['delivery_output_names'] = output_names
    if not expected <= actual:
        raise ValueError('Missing final deliverables: ' + ', '.join(sorted(expected - actual)))
    from bioma.core.reporting import assignment_needs_report, assignment_needs_pdf
    if assignment_needs_pdf(context.state) and not any(Path(path).suffix.lower()=='.pdf' for path in observation.artifacts):
        raise ValueError('The original task explicitly requires a PDF; Markdown alone is incomplete.')
    if assignment_needs_report(context.state) and not any(Path(path).suffix.lower() in {'.md', '.pdf'} for path in observation.artifacts):
        raise ValueError('The original task requires a report; intermediate computation is incomplete.')
    # Do not allow a changed artifact to inherit the execution provenance.
    for path, digest in {**observation.metrics['sha256'], **observation.metrics.get('execution_sha256', {})}.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError('Executed artifact changed before submission: ' + path)
    from bioma.harness.reproduction import verify_requested_reproduction
    folder = Path(observation.metrics['receipt_path']).parent
    output_dir = folder / 'outputs'
    observation.metrics['final_reproduction_checks'] = verify_requested_reproduction(
        context.state.get('current_user_request', ''), context.state.get('raw_data_sources', []),
        [Path(path).relative_to(output_dir) for path in observation.artifacts], output_dir, folder)
    observation.tool_name = 'finish_investigation'
    from bioma.harness.result_proposal import bind
    candidate_ref = bind(context, args.get('answer_candidate_ref'), selected, requested,
        answer=args.get('answer_text'), assumptions=args.get('assumptions', []),
        unresolved=args.get('unresolved_requirements', []), available=catalog,
        selection='model_explicit' if args.get('receipt_refs') else 'unique_eligible_support')
    digest = context.state['final_answer_candidates'][candidate_ref]
    observation.metrics['answer_candidate_ref'] = candidate_ref
    observation.metrics['selected_support_receipt_refs'] = selected
    observation.metrics['execution_sha256'][candidate_ref] = digest
    # The selected declaration derives from immutable drafts. Their byte identity
    # remains part of the review snapshot and must survive interrupted review.
    from bioma.harness.result_proposal import load
    parent = load(context, candidate_ref).get('parent_proposal_ref')
    visited = {candidate_ref}
    while parent:
        if parent in visited:
            raise ValueError('Cyclic result proposal lineage')
        visited.add(parent)
        previous = load(context, parent)
        observation.metrics['execution_sha256'][parent] = context.state['final_answer_candidates'][parent]
        parent = previous.get('parent_proposal_ref')
    observation.patch['selected_delivery_paths'].append(candidate_ref)
    observation.summary = args['reason']
    return observation


class ScientificInvestigationWorker(DataAnalysisRunner):
    """Reuse one coordinator, workspace, budget and code engine across methods."""

    def __init__(self, *args, reference_models=(), skill_view=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.reference_models = tuple(reference_models)
        self.skill_view = skill_view

    def consult(self, context, args):
        from bioma.llm import ModelBudgetUnavailable
        budget, checkpoint = context.investigation_budget, context.investigation_checkpoint
        if budget.remaining_llm_calls < 2:
            raise ModelBudgetUnavailable('Advice must leave a coordinator call available')
        budget.record_llm_call()
        checkpoint.save_work(budget)
        advisor = self.loop.provider_factory(self.reference_models[0].llm)
        response = advisor.chat_with_tools([{'role': 'user', 'content':
            'Provide methodological advice only; you cannot produce scientific evidence or execute tools.\n' +
            json.dumps(args)}], [])
        budget.telemetry.prompt_tokens += getattr(advisor, 'last_prompt_tokens', 0)
        budget.telemetry.completion_tokens += getattr(advisor, 'last_completion_tokens', 0)
        return ToolObservation('request_reference_advice', 'ok', 'Non-evidentiary reference advice.',
            metrics={'advice': response.content, 'is_evidence': False})

    def tools_for_state(self, state):
        from bioma.runtime.capability_requests import (
            CapabilityDiscovery, CapabilityRequest, discover, discover_inputs, request)
        from bioma.runtime.support_use import admitted_request_schema, check_support_use
        def support_use(context, args):
            return ToolObservation('assess_support_use', 'ok',
                'Use-scoped applicability; historical support and execution authority are unchanged.',
                metrics=check_support_use(args))

        def capability_discovery(context, args):
            rows = discover(capability=args.get('capability'), method_id=args.get('method_id'))
            offset, limit = args.get('offset', 0), args.get('limit', 6)
            inputs, input_offset = discover_inputs(), args.get('input_offset', 0)
            return ToolObservation('discover_scientific_capabilities', 'ok',
                'Registered Methods; discovery confers no execution authority.',
                metrics={'capabilities': rows[offset:offset+limit], 'total':len(rows),
                         'next_offset':offset+limit if offset+limit < len(rows) else None,
                         'admitted_inputs':inputs[input_offset:input_offset+limit],
                         'next_input_offset':input_offset+limit if input_offset+limit < len(inputs) else None})

        def capability_request(context, args):
            result = request(args, budget=context.investigation_budget,
                persist_budget=lambda: context.investigation_checkpoint.save_work(context.investigation_budget))
            return ToolObservation('request_scientific_capability', 'ok',
                'Runtime capability request outcome; not scientific Evidence.',
                artifacts=[a['uri'] for a in result.get('artifacts', [])],
                metrics=result)

        tools = [tool for tool in self.tools if tool.name in {
            'inspect_inputs', 'inspect_tabular_schema', 'inspect_input_data',
            'find_scientific_tools', 'invoke_scientific_tool'}]
        tools.extend([
            HarnessTool('assess_support_use',
                'Before treating historical scientific support as confirmed for a new use, request an applicability check. Provide claim_id, support_link_id and intended use only. Runtime resolves authorized records, source versions and checker. Returns applicable, reassessment_required or unknown; ordinary history remains context_only. Grants no execution permission.',
                handler=support_use, args_schema=admitted_request_schema()),
            HarnessTool('discover_scientific_capabilities',
                'Discover professional Methods across Backends. Every returned Method executes only through request_scientific_capability, never invoke_scientific_tool. Filter by capability; specify method_id for its full input schema, result contract and request Action schema. Discovery confers no permission.',
                handler=capability_discovery, args_schema=CapabilityDiscovery),
            HarnessTool('request_scientific_capability',
                'The only execution Action for a Method returned by discover_scientific_capabilities. Request one discovered Method for the stated purpose using parent-admitted inputs; do not send Method IDs to invoke_scientific_tool. A stable request_id identifies one attempt. Runtime independently authorizes and reviews execution. Returns feedback, not Evidence or contract completion; unresolved attempts cannot be replayed.',
                handler=capability_request, args_schema=CapabilityRequest),
            HarnessTool('inspect_execution_records', 'Inspect unified native execution identities, immutable output versions, actual-access gaps and applicable-check readiness. Does not execute or qualify results.',
                        handler=inspect_execution_records, args_schema=ExecutionViewQuery),
            HarnessTool('execute_analysis_code', 'Run an intermediate Python/R computation. Continue investigating or explicitly finish after reviewing results.',
                        handler=compute_step, args_schema=AnalysisCodeInput),
            HarnessTool('revise_analysis_code', 'Edit and rerun the latest script, including after a failed execution.',
                        handler=lambda context, args: compute_step(context, args, revise=True), args_schema=AnalysisRevisionInput),
            HarnessTool('read_workspace', 'Read an existing file inside this work package artifact workspace with bounded line ranges. Admitted input files are separate: use inspect_input_data(input_index), not this tool.', handler=read_workspace, args_schema=ReadWorkspace),
            HarnessTool('retrieve_history', 'Retrieve complete earlier tool arguments, observations and failures on demand.', handler=retrieve_history, args_schema=HistoryQuery),
            HarnessTool('save_working_notes', 'Record provisional findings, method, limitations and references; never scientific truth.', handler=save_notes, args_schema=WorkingNotes),
            HarnessTool('request_user_clarification', 'After inspecting admitted inputs, suspend only the analysis that depends on a genuinely unresolved user choice. Records the exact field, question, reason and inspected input identities; it does not discard completed work or assert that a file is missing.', handler=request_clarification, args_schema=ClarificationRequest),
            HarnessTool('finish_investigation', 'Submit selected receipt_refs and optional artifact_paths from this work package for independent review. Omit selection only when one unambiguous eligible support record exists. answer_text, assumptions and unresolved_requirements form an unverified ResultProposal. Does not commit scientific truth.', handler=finish, args_schema=Finish),
        ])
        if self.reference_models and self.config.harness_mode == 'current':
            tools.append(HarnessTool('request_reference_advice',
                'Optional consultation for a specific methodological disagreement; consumes the SAME request budget, never evidence.',
                handler=self.consult, args_schema=Advice))
        if self.skill_view is not None and self.config.harness_mode in {"current", "skills", "governed"}:
            from bioma.capabilities.scoped import build_scoped_skill_tools
            # These are local metadata reads; executable tools still use scoped grants.
            for metadata in build_scoped_skill_tools(self.skill_view):
                tools.append(HarnessTool(metadata.name, metadata.description,
                    handler=lambda context, args, metadata=metadata: _skill_observation(metadata, args), args_schema=metadata.args_schema))
        tools.extend(tool for tool in self.external_tools
                     if (self.config.harness_mode == 'current' or tool.name in self.config.direct_tool_names)
                     and tool.name not in {t.name for t in tools})
        return tools

    def run(self, state):
        return super().run({**state, 'continuous_investigation': True,
            'harness_mode': self.config.harness_mode,
            'public_resource_discovery': self.config.harness_mode != 'current' and
                any(t.name == 'read_public_resource' for t in self.external_tools)})


def investigation_backend(config, base_dir, *, provider_factory=None, skill_registry=None, reference_models=()):
    """Direct Provider backend, never invokes PredAgent/ComAgent.handle()."""
    def execute(state, message, tools):
        from bioma.core.source_inputs import verify_source_identities
        from bioma.backends.base import upstream_work_result_identities
        from bioma.core.research import fingerprint
        from bioma.tools.registry import ToolRegistry
        from bioma.runtime.admission import current_execution_permit, check_execution
        current_execution_permit()  # Work-package execution always requires live WP1.
        check_execution()
        inputs = message.payload.get('inputs') or {}
        try:
            sources = verify_source_identities(inputs.get('source_inputs', []))
        except (OSError, ValueError) as exc:
            names = [str(row.get('name') or Path(row.get('uri', '')).name)
                     for row in inputs.get('source_inputs', [])]
            raise BackendIncomplete('blocked', {
                'code': 'input_unavailable', 'stage': 'resource_access',
                'resources': names, 'detail': str(exc),
                'model_dispatched': False, 'computation_started': False,
            }) from exc
        upstream = upstream_work_result_identities(state, message.payload.get('input_refs', []))
        files = inputs.get('files') or message.payload.get('files') or state.inputs.get('files') or []
        files = [str((Path(base_dir) / path).resolve()) for path in files]
        unavailable = [{'name': Path(path).name,
                        'reason': ('missing' if not Path(path).exists()
                                   else 'not_regular_file' if not Path(path).is_file()
                                   else 'not_readable')}
                       for path in files
                       if not Path(path).is_file() or not os.access(path, os.R_OK)]
        if unavailable:
            raise BackendIncomplete('blocked', {
                'code': 'input_unavailable', 'stage': 'resource_access',
                'resources': unavailable, 'model_dispatched': False,
                'computation_started': False,
            })
        artifact_ids = message.payload.get('input_artifact_ids', [])
        dependency_refs = set(message.payload.get('dependency_evidence_ids', []))
        if dependency_refs - {e.id for e in state.evidence}:
            raise ValueError('Committed input evidence grant changed')
        granted_files = []
        for artifact_id in artifact_ids:
            artifact = next((a for a in state.artifacts if a.id == artifact_id), None)
            refs = dependency_refs
            if (artifact is None or not refs.intersection(artifact.evidence_ids)
                    or hashlib.sha256(Path(artifact.uri).read_bytes()).hexdigest() != artifact.metadata.get('sha256')):
                raise ValueError('Committed input artifact or grant changed')
            granted_files.append(str(Path(artifact.uri).resolve()))
        if artifact_ids or dependency_refs:
            if set(files) - set(granted_files):
                raise ValueError('Downstream files exceed declared committed artifact grants')
            files = granted_files
        scope = fingerprint([state.task_id, message.task_request_id or message.id])[:32]
        root = Path(base_dir) / 'artifacts' / 'investigation_inputs' / scope
        if not files:
            # Structured inputs remain request data, never claimed measurements.
            root.mkdir(parents=True, exist_ok=True)
            request = root / 'request.json'
            content = json.dumps(inputs, sort_keys=True, ensure_ascii=False)
            if request.exists() and request.read_text() != content:
                raise ValueError('Work-package structured input changed')
            request.write_text(content)
            files = [str(request.resolve())]
        registry = ToolRegistry([])
        for tool in tools:
            registry.add(tool)
        skill_view = None
        if skill_registry is not None:
            from bioma.capabilities.scoped import ScopedSkillResolver
            from bioma.backends.catalog import method
            skill_view = ScopedSkillResolver(skill_registry, registry).view(
                provider_id='scientific-compute', capability_family=COMPUTE,
                method=method(COMPUTE, 'continuous_scientific_investigation', 'InvestigationResult'),
                method_tools=tools, question=state.objective,
                result_kinds={tool.result_kind for tool in tools},
                knowledge_discovery=config.harness_mode in {'skills', 'governed'})
        runner = ScientificInvestigationWorker(config, base_dir=Path(base_dir),
            tool_registry=registry, provider_factory=provider_factory,
            skill_view=skill_view, reference_models=reference_models)
        assignment = message.payload.get('research_assignment') or {}
        outputs = message.payload.get('desired_deliverables') or [name
            for row in assignment.get('acceptance', []) for name in row.get('artifact_names', [])] or state.gateway.get('desired_deliverables', [])
        from bioma.core.query_contract import query_input_questions
        questions = query_input_questions(message.payload.get('query_spec') or state.gateway.get('query_spec') or {})
        intake = message.payload.get('query_spec') or state.gateway.get('query_spec') or {}
        package = InvestigationWorkPackage(task_id=state.task_id, workspace_scope=scope,
            objective=message.payload.get('assignment_objective') or state.objective,
            original_request=state.objective,
            execution_requirements=message.payload.get('execution_requirements') or {},
            input_manifest={path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in files},
            query_input_questions=questions,
            research_intake=intake,
            analysis_work=inputs.get('analysis_work'), deliverables=outputs, permitted_tools=[tool.name for tool in tools],
            max_model_requests=config.max_llm_calls, max_tool_calls=config.max_tool_calls)
        workspace = Path(base_dir) / 'artifacts' / scope / 'harness'
        workspace.mkdir(parents=True, exist_ok=True)
        package_path = workspace / 'work_package.json'
        encoded_package = package.model_dump_json(indent=2)
        if package_path.exists() and package_path.read_text() != encoded_package:
            previous_package = InvestigationWorkPackage.model_validate_json(package_path.read_text())
            previous_identity = previous_package.model_dump(exclude={'max_model_requests', 'max_tool_calls'})
            current_identity = package.model_dump(exclude={'max_model_requests', 'max_tool_calls'})
            clarification_continuation = bool(message.payload.get('clarification_reply'))
            if (not clarification_continuation or previous_identity != current_identity
                    or package.max_model_requests < previous_package.max_model_requests
                    or package.max_tool_calls < previous_package.max_tool_calls):
                raise ValueError('Investigation work-package inputs or limits changed; require a new assignment')
            version = hashlib.sha256(encoded_package.encode()).hexdigest()
            continuation_package = workspace / f'work_package.continuation-{version[:12]}.json'
            if continuation_package.exists() and continuation_package.read_text() != encoded_package:
                raise ValueError('Investigation continuation package identity collision')
            continuation_package.write_text(encoded_package)
        else:
            package_path.write_text(encoded_package)
        clarification = message.payload.get('clarification_reply')
        if clarification:
            record = {
                **clarification,
                'work_package_sha256': hashlib.sha256(encoded_package.encode()).hexdigest(),
                'source_message_id': message.causation_id,
            }
            clarification_path = workspace / 'clarification.json'
            encoded_clarification = json.dumps(record, sort_keys=True, indent=2)
            if clarification_path.exists():
                existing = json.loads(clarification_path.read_text())
                # A later review-only continuation receives a new transport
                # message but does not create a new scientific definition.
                # Preserve the first source message while requiring identical
                # clarification content and work-package identity.
                comparable = lambda value: {
                    key: item for key, item in value.items()
                    if key != 'source_message_id'
                }
                if comparable(existing) != comparable(record):
                    raise ValueError('Clarification identity changed for an existing work package')
            else:
                clarification_path.write_text(encoded_clarification)
        from bioma.llm import ComputedPendingReview, ModelBudgetUnavailable
        try:
            result = runner.run({'task_id': state.task_id, 'artifact_scope_id': scope,
                'current_user_request': message.payload.get('assignment_objective') or state.objective,
                'original_user_request': state.objective,
                'research_assignment': assignment, 'desired_deliverables': outputs,
                'analysis_work':inputs.get('analysis_work'), 'query_input_questions':questions,
                'research_intake':intake,
                **({'clarification_reply': clarification} if clarification else {}),
                'raw_data_sources': list(dict.fromkeys(files)),
                'execution_requirements': message.payload.get('execution_requirements') or {}})
        except ComputedPendingReview as exc:
            raise BackendIncomplete('pending', {
                'code': 'computed_pending_review', 'workspace_scope': scope,
                'checkpoint': exc.checkpoint, 'progress_retained': True,
                'detail': str(exc), 'computation_must_not_be_repeated': True}) from exc
        except ModelBudgetUnavailable as exc:
            raise BackendIncomplete('pending', {
                'code': 'model_budget_unavailable', 'workspace_scope': scope,
                'progress_retained': True, 'detail': str(exc),
                'computation_must_not_be_repeated': True}) from exc
        if result.get('status') == 'paused':
            if result.get('budget_pause'):
                raise BackendIncomplete('pending', {'code':'action_budget_paused',
                    'workspace_scope':scope, **result['budget_pause'],
                    'detail':'Completed work retained. Continuation requires fresh admission and available budget; no allowance was added.'})
            raise BackendIncomplete('pending', {'code':'action_session_paused',
                'workspace_scope':scope, 'progress_retained':True,
                'detail':'Resume the same session under freshly admitted WP1 authority.'})
        if result.get('status') == 'waiting_user':
            request = result.get('clarification_request') or {}
            raise BackendIncomplete('pending', {
                'code': 'clarification_required', 'stage': 'dependent_analysis',
                'field': request.get('field'),
                'question': result.get('pending_question'),
                'reason': request.get('reason'),
                'inspected_inputs': request.get('inspected_inputs', []),
                'workspace_scope': scope, 'progress_retained': True,
            })
        if result.get('status') != 'completed':
            raise BackendIncomplete('failed', {'code': result.get('error_code') or 'investigation_incomplete',
                'reason': result.get('harness_exit_reason'), 'detail': result.get('error_detail'),
                'workspace_scope': scope,
                'telemetry': result.get('harness_telemetry'), 'progress_retained': True})
        paths = result.get('selected_delivery_paths') or []
        if not paths:raise ValueError('No explicitly bound delivery artifacts')
        reviewed_hashes = (result.get('delivery_review') or {}).get('sha256', {})
        if any(path not in reviewed_hashes or hashlib.sha256(Path(path).read_bytes()).hexdigest() != reviewed_hashes[path]
               for path in paths):
            raise ValueError('Investigation artifact changed or was not covered by final review')
        artifacts = [Artifact(kind='investigation-output', uri=path, source='scientific-compute',
            metadata={'sha256': reviewed_hashes[path],
                      **({'relative_path': result['delivery_output_names'][path]}
                         if path in result.get('delivery_output_names', {}) else {})}).model_dump(mode='json') for path in paths]
        from bioma.harness.result_proposal import reviewed_result_basis
        basis = reviewed_result_basis(result)
        selected = {row['receipt_ref'] for row in basis}
        return WorkResult(family=COMPUTE, kind='InvestigationResult',
            payload={'result_basis': basis,
                **({'analysis_results': result.get('analysis_results')}
                   if any(row['acquisition'] == 'computation' for row in basis) else {}),
                **({'final_answer':result['final_answer'],
                    'accepted_answer_candidate':result['accepted_answer_candidate']}
                   if result.get('accepted_answer_candidate') else {}),
                'delivery_review': result.get('delivery_review'),
                'tool_receipts': [row for row in result.get('tool_receipts', []) if row.get('path') in selected],
                'execution_receipts': [row['receipt_ref'] for row in basis],
                'input_manifest': result.get('selected_input_manifest', {}),
                'retrieved_input_bindings': result.get('admitted_tool_inputs', {}),
                'recovered_execution_input_bindings': result.get('admitted_execution_inputs', {}),
                'limitations': ['Result origins describe selected execution records, not scientific qualification.',
                    'Retrieval is not an experiment; computation is not a measurement.']},
            input_refs=list(dict.fromkeys([s.id for s in sources] + [row['observation_id'] for row in upstream] + sorted(dependency_refs))),
            artifacts=artifacts, provenance={'method': 'continuous_scientific_investigation',
                'work_package_sha256': hashlib.sha256(encoded_package.encode()).hexdigest(),
                'workspace_scope': scope, 'workspace_uri': str(workspace),
                'telemetry': result.get('harness_telemetry'),
                'source_inputs': [s.model_dump(mode='json') for s in sources],
                'upstream_work_results': upstream, 'reference_advice_calls': sum(row.get('tool_name') == 'request_reference_advice'
                    for row in result.get('harness_observations', []))})
    return execute
