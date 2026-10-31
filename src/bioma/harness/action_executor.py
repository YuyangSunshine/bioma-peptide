"""Shared action dispatch extracted from the original loop; no agent reasoning.

Runtime admission, HarnessTool grants, receipts and delivery review remain the
existing authorities. Both loops call this service; it owns no ToolRegistry.
"""
import hashlib
import json
from bioma.harness.policy import validate_tool_call, recommend_next_tools
from bioma.llm import ModelTransportError, ModelOutputBudgetError
from bioma.runtime.deadline import ExecutionTimeout
from bioma.runtime.telemetry import emit


def parse_tool_args(raw):
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise ValueError('Tool arguments must be a JSON object.')
    return parsed


def call_harness_tool(name, raw_args, tools, context, budget, llm, checkpoint):
    from bioma.llm import ModelBudgetUnavailable
    check_deadline()
    check_execution()
    if context.state.get('unresolved_tool_action'):
        from bioma.harness.checkpoint import ReviewContinuationUnavailable
        raise ReviewContinuationUnavailable('UNRESOLVED_TOOL_ACTION: reconcile before another dispatch')
    if budget.remaining_tool_calls <= 0:
        raise ModelBudgetUnavailable("Action tool budget exhausted")
    budget.record_tool_call()
    args = {}
    from bioma.harness.types import ToolObservation

    tool = tools.get(name)
    if tool is None:
        observation = ToolObservation(
            tool_name=name, status="error", summary="Unknown tool.",
            metrics={"available": sorted(tools)}, warnings=["UNKNOWN_TOOL"],
        )
    else:
        observation = validate_tool_call(name, context)
        if observation is None:
            try:
                args = parse_tool_args(raw_args)
                if args.get('skill_refs'):
                    from bioma.harness.investigation_memory import skill_reads
                    if set(args['skill_refs']) - set(skill_reads(context)):
                        raise ValueError('Skill references must identify successfully read skills before method selection')
                from contextlib import nullcontext
                from bioma.llm import ModelBudgetUnavailable, ComputedPendingReview
                needs_review = (name == 'finish_investigation' if context.state.get('continuous_investigation')
                    else name in {'execute_analysis_code', 'revise_analysis_code'})
                if needs_review and budget.remaining_llm_calls < 1:
                    raise ModelBudgetUnavailable('Reserve one post-computation review call before computing')
                reservation = (llm.reserve_review(context, args)
                    if needs_review and hasattr(llm, 'reserve_review') else nullcontext())
                with reservation:
                    # A durable intent is cleared only after an observed outcome.
                    # This protects the legacy path too: no blind replay after
                    # timeout, lost transport or process/checkpoint interruption.
                    context.state['unresolved_tool_action'] = {'tool':name,
                        'arguments_sha256':hashlib.sha256(raw_args.encode()).hexdigest()}
                    if checkpoint is not None:
                        checkpoint.save_work(budget)
                    observation = tool.call(context, args)
                    if needs_review and observation.status == 'ok':
                        observation = checkpoint.review(observation, llm, budget)
            except ExecutionTimeout:
                raise
            except Exception as exc:
                from bioma.runtime.admission import ExecutionAuthorizationError
                from bioma.llm import ModelOutputBudgetError, ModelBudgetUnavailable
                from bioma.runtime.workers import ProviderUnavailable
                if isinstance(exc, (ExecutionAuthorizationError, ProviderUnavailable, ModelOutputBudgetError, ModelBudgetUnavailable)):
                    raise
                if isinstance(exc, (ModelTransportError, OSError)):
                    raise
                context.state.pop('unresolved_tool_action', None)
                from bioma.harness.code_analysis import retain_report_repair_draft, DeclaredOutputMissing
                retained = (name == 'execute_analysis_code' and retain_report_repair_draft(context, args, exc))
                detail = type(exc).__name__ + ': ' + str(exc)
                if len(detail) > 6000:
                    from uuid import uuid4
                    diagnostics = context.artifact_dir('tool_errors') / (uuid4().hex + '.txt')
                    diagnostics.write_text(detail, encoding='utf-8')
                    detail = (detail[:2000] + '\n... middle omitted; full diagnostic retained ...\n'
                              + detail[-4000:] + '\nFull diagnostic: ' + str(diagnostics))
                if (
                    name in {'execute_analysis_code', 'revise_analysis_code'}
                    and 'Source validation failed' in str(exc)
                    and any(
                        f'Undefined name `{tool_name}`' in str(exc)
                        for tool_name in (
                            'inspect_inputs', 'inspect_tabular_schema', 'inspect_input_data',
                            'recommend_next_step', 'execute_analysis_code',
                            'revise_analysis_code',
                        )
                    )
                ):
                    detail += (
                        '\nHarness tools cannot be called from generated Python. '
                        'Use their already-returned observations and INPUT_FILES instead.'
                    )
                if getattr(exc, 'retained_outputs', None):
                    detail += ('\nDeclared files survived and are retained as unqualified INPUT_FILES. '
                               'Inspect/reuse their actual contents with execute_analysis_code and input_indices '
                               'to repair downstream work. revise_analysis_code reruns the entire program. '
                               'These partial files cannot be submitted as completed results.')
                from bioma.harness.delivery_review import DeliveryReviewRejected
                review_rejection = isinstance(exc, DeliveryReviewRejected)
                from bioma.harness.diagnostics import diagnostic_context
                observation = ToolObservation(
                    tool_name=name, status="error", summary=detail + (
                        '\nThe typed program is retained as an UNEXECUTED draft. Use revise_analysis_code with edits=[] and a valid reports OBJECT, not a JSON-encoded string. Repair only the layout; do not resend the full program. Normal execution checks and delivery review still apply.' if retained else ''),
                    warnings=["TOOL_EXCEPTION"],
                    metrics={"error_type": type(exc).__name__, "model_transport_error": isinstance(exc, ModelTransportError),
                             "requested_tool": args.get("tool_name") if name == "invoke_scientific_tool" else name,
                             "diagnostic_context": diagnostic_context(
                                 name, exc,
                                 requested_tool=args.get("tool_name") if name == "invoke_scientific_tool" else None),
                             "unexecuted_draft_retained": bool(retained),
                             **({"failed_execution_receipt":{
                                 "path":str(exc.receipt_path),
                                 "sha256":hashlib.sha256(Path(exc.receipt_path).read_bytes()).hexdigest()}}
                                if name in {'execute_analysis_code', 'revise_analysis_code'}
                                and getattr(exc, 'receipt_path', None) else {}),
                             **({"retained_partial_outputs": exc.retained_outputs} if getattr(exc, "retained_outputs", None) else {}),
                             **({"execution_validation": exc.validation} if isinstance(exc, DeclaredOutputMissing) else {}),
                             **({"computed_delivery_sha256": exc.computed_delivery_sha256,
                                 "review_issues": exc.issues,
                                 "dismissed_non_issues": exc.dismissed_non_issues}
                                if review_rejection else {})},
                )
    context.record_observation(observation)
    context.state.pop('unresolved_tool_action', None)
    if checkpoint is not None:
        checkpoint.save_work(budget)
    payload = observation.to_agent_dict()
    if observation.status == 'error' and context.analysis_draft is not None:
        source = context.analysis_draft['code']
        draft = {
            'sha256': hashlib.sha256(source.encode()).hexdigest(),
            'line_count': len(source.splitlines()),
            'output_files': context.analysis_draft['output_files'],
            'reports': context.analysis_draft.get('reports', {}),
            'input_indices': context.analysis_draft.get('input_indices'),
        }
        from bioma.harness.code_analysis import resolve_analysis_inputs
        try:
            catalog, selected = resolve_analysis_inputs(context, context.analysis_draft)
            indices = context.analysis_draft.get('input_indices')
            if indices is None:
                indices = list(range(len(catalog)))
            draft['execution_input_map'] = [
                {'python_index': local, 'r_index': local + 1,
                 'catalog_index': index, 'path': selected[local]}
                for local, index in enumerate(indices)]
            draft['input_index_rule'] = 'INPUT_FILES uses the local selected order. For input_indices=[1], read INPUT_FILES[0] in Python (INPUT_FILES[[1]] in R). File contents retain their original schema; tool result fields need not be file fields.'
        except ValueError as exc:
            draft['input_selection_error'] = str(exc)
        # Small drafts remain convenient for line-edit repair. Large drafts
        # already exist in the immediately preceding native tool arguments
        # and in the durable checkpoint/trace; echoing them again doubles
        # model context without adding authority or scientific information.
        if len(source.encode()) <= 16_384:
            draft['numbered_source'] = '\n'.join(
                f'{i}: {line}' for i, line in enumerate(source.splitlines(), 1)
            )
        else:
            draft['storage'] = 'durable_checkpoint_and_tool_trace'
        payload['analysis_draft'] = draft
    payload["next_recommendation"] = recommend_next_tools(context)
    # Persist exact feedback, including failed drafts, before the next model turn.
    with (context.artifact_dir('tool_calls') / 'trace.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'tool': name, 'arguments': args, 'observation': payload,
                                **({'raw_arguments': raw_args} if observation.status == 'error' else {})},
                                ensure_ascii=False, default=str) + '\n')
    if context.state.get('continuous_investigation'):
        from bioma.harness.investigation_memory import capability_events
        trace = context.artifact_dir('tool_calls') / 'trace.jsonl'
        sequence = sum(1 for _ in trace.open())
        entry = json.loads(trace.read_text().splitlines()[-1])
        with (context.artifact_dir('tool_calls') / 'capability_events.jsonl').open('a') as archive:
            for event in capability_events(entry, sequence):
                archive.write(json.dumps(event, ensure_ascii=False) + '\n')
                emit('capability.usage', **event)
    return json.dumps(payload, ensure_ascii=False, default=str), args


from pathlib import Path
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from bioma.harness.types import ToolObservation
from bioma.runtime.admission import check_execution, current_execution_permit
from bioma.runtime.deadline import check as check_deadline, remaining
from bioma.llm import ModelBudgetUnavailable, ComputedPendingReview


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["shell", "tool", "inspect_tool", "analysis", "submit", "pause"]
    command: str = ""
    name: str = ""
    source: str = ""
    arguments: dict = Field(default_factory=dict)
    timeout: int = Field(default=120, ge=1, le=120)


class ActionObservation(BaseModel):
    action_id: str
    kind: str
    status: str
    result: dict
    receipt_ref: str | None = None
    receipt_sha256: str | None = None


def _digest(path):
    from bioma.harness.execution_view import digest
    return digest(path)


def _safe_file(root, relative):
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError("A script must be relative to the scientific workspace")
    resolved = (root / candidate).resolve()
    resolved.relative_to(root.resolve())
    if not resolved.is_file():
        raise ValueError("The requested script is not a regular workspace file")
    return resolved


def _receipt_summary(result):
    metrics = result.get("metrics") or {}
    candidates = []
    if metrics.get("receipt_path"):
        candidates.append(metrics["receipt_path"])
    failed = metrics.get("failed_execution_receipt") or {}
    if failed.get("path"):
        candidates.append(failed["path"])
    receipts = []
    for value in dict.fromkeys(str(path) for path in candidates):
        path = Path(value)
        item = {"id": value, "receipt_ref": value, "path": value}
        if path.is_file():
            item["sha256"] = _digest(path)
        receipts.append(item)
    return receipts


def _priority_tool_feedback(result, feedback, context, root, limit=12000):
    """Keep repair-critical fields intact; retain long raw feedback by hash."""
    if len(feedback) <= limit:
        return feedback
    encoded = feedback.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    folder = root / "feedback"
    folder.mkdir(parents=True, exist_ok=True)
    retained = folder / f"{digest}.json"
    if retained.exists():
        if hashlib.sha256(retained.read_bytes()).hexdigest() != digest:
            raise ValueError("Content-addressed feedback receipt changed")
    else:
        retained.write_bytes(encoded)
    diagnostic = (result.get("metrics") or {}).get("diagnostic_context") or {}
    replay_assessment = diagnostic.get("retry_assessment")
    if replay_assessment is None and "retryable" in diagnostic:
        from bioma.harness.diagnostics import retry_assessment
        replay_assessment = retry_assessment(bool(diagnostic["retryable"]))
    recommendation = result.get("next_recommendation") or {}
    critical = {
        "feedback_contract": "bioma.priority_diagnostic.v2",
        "tool_name": result.get("tool_name"),
        "status": result.get("status"),
        "failure": {
            "domain": diagnostic.get("failure_domain"),
            "stage": diagnostic.get("failure_stage"),
            "failed_action": diagnostic.get("failed_action"),
            "unmet_conditions": diagnostic.get("unmet_conditions") or [],
            "automatic_replay_assessment": replay_assessment,
            "agent_decision_required": diagnostic.get("agent_decision_required"),
        },
        "receipts": _receipt_summary(result),
        "unresolved_state": {
            "delivery_complete": bool(context.state.get("delivery_complete")),
            "need_clarification": bool(context.state.get("need_clarification")),
            "review_pending": bool(context.state.get("action_review_pending")),
            "reason": recommendation.get("reason"),
            "delivery_readiness": recommendation.get("delivery_readiness"),
        },
        "warnings": result.get("warnings") or [],
        "full_feedback_receipt": {
            "id": digest,
            "path": str(retained),
            "sha256": digest,
            "bytes": len(encoded),
        },
        "raw_feedback_inlined": False,
    }
    # Critical diagnostics take precedence over the historical display limit.
    # In normal operation this envelope is small; never slice its JSON structure.
    return json.dumps(critical, ensure_ascii=False, default=str)

class ScientificActionExecutor:
    """Single action service backed by existing BioMA guards; no agent loop."""

    def __init__(self, *, context, budget, tools, review_llm, checkpoint):
        self.context, self.budget = context, budget
        self.tools = {item.name: item for item in tools}
        self.review_llm, self.checkpoint = review_llm, checkpoint
        self.workspace = context.artifact_dir("scientific_workspace").resolve()
        for name in ("scripts", "scratch", "results", "reports", ".actions"):
            (self.workspace / name).mkdir(parents=True, exist_ok=True)
        self.trace = context.artifact_dir("action_records") / "shell.jsonl"
        self.trace.parent.mkdir(parents=True, exist_ok=True)

    def get_template_vars(self, **kwargs):
        # Do not expose os.environ to the model as mini's LocalEnvironment does.
        return {"cwd": str(self.workspace), **kwargs}

    def serialize(self):
        return {"info": {"config": {"environment_type": "ScientificActionExecutor",
                                   "workspace": str(self.workspace)}}}

    def _tool(self, name, args):
        feedback, _ = call_harness_tool(
            name, json.dumps(args, ensure_ascii=False), self.tools, self.context,
            self.budget, self.review_llm, self.checkpoint,
        )
        result = json.loads(feedback)
        output = _priority_tool_feedback(result, feedback, self.context, self.trace.parent)
        return {"output": output, "observation": result,
                "returncode": 0 if result.get("status") == "ok" else 1,
                "exception_info": ""}

    def _shell(self, command: str, timeout: int | None, *, action_id, output_files=()):
        from bioma.backends.analysis.processes import code_action_boundary, run_python
        current_execution_permit()
        self.budget.record_tool_call()
        # The archived source is not writable by the sandboxed process.
        archived = self.trace.parent / (action_id + ".sh")
        archived.write_text(command + "\n", encoding="utf-8")
        action_dir = self.workspace / ".actions"
        if action_dir.is_symlink() or action_dir.resolve() != action_dir:
            raise ValueError("Action directory must not be a symlink")
        script = action_dir / (action_id + ".sh")
        with script.open("x", encoding="utf-8") as stream:
            stream.write(command + "\n")
        source_hash = _digest(archived)
        inputs = tuple(Path(p).resolve() for p in self.context.state.get("raw_data_sources", []))
        input_hashes = {str(p):_digest(p) for p in inputs}
        if not isinstance(output_files, (list, tuple)) or any(
            not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
            for name in output_files):
            raise ValueError("Shell output_files must be relative workspace paths")
        if len(set(output_files)) != len(output_files):
            raise ValueError("Shell output_files must be unique")
        runtime = {'shell':'/bin/bash', 'executable_sha256':_digest(Path('/bin/bash')),
                   'boundary':'existing BioMA isolated process',
                   'nested_interpreters':'not_observed', 'packages':None}
        limit = min(120, int(timeout or 120))
        left = remaining()
        if left is not None:
            limit = min(limit, max(1, int(left)))
        from bioma.runtime.tool_actions import invoke
        dispatch = {}
        def execute_shell():
            from bioma.runtime.tool_actions import current_tool_action
            dispatch.update(current_tool_action() or {})
            with code_action_boundary(self.workspace, inputs):
                return run_python(script, executable="/bin/bash", cwd=self.workspace, timeout=limit)
        result = invoke("mini_swe_shell", {"source_sha256": source_hash}, execute_shell,
                        version="bioma-isolated-shell-v1")
        if _digest(script) != source_hash:
            raise ValueError("Sandbox command script was modified while executing")
        output_archive = self.trace.parent / (action_id + ".output.json")
        output_archive.write_text(json.dumps({"stdout":result.stdout,"stderr":result.stderr},ensure_ascii=False))
        versions = {}
        output_errors = []
        import shutil
        for name in output_files:
            try:
                source = _safe_file(self.workspace, name)
                expected = _digest(source)
                target = self.trace.parent / (action_id + ".outputs") / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if _digest(target) != expected:
                    raise ValueError("Output changed during capture")
                versions[str(target)] = expected
            except (ValueError, OSError) as exc:
                output_errors.append({'path':name,'error':str(exc)})
        record = {
            "output_archive":str(output_archive), "output_sha256":_digest(output_archive),
            "action_id": action_id, "executed_source_sha256": source_hash,
            "source_archive": str(archived),
            "input_sha256": input_hashes,
            "runtime":runtime, "dispatch":dispatch, "output_versions":versions,
            "output_errors":output_errors,
            "parameters":{"command":command, "timeout":limit, "output_files":list(output_files)},
            "returncode": result.returncode, "elapsed_not_measured": True,
            "status": "ok" if result.returncode == 0 and not output_errors else "error",
            "scope": "Provisional shell action, not qualified computation or Evidence",
        }
        with self.trace.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.context.record_observation(ToolObservation(
            "mini_swe_shell", record["status"], "Isolated command executed; not scientific evidence.",
            metrics=record))
        self.checkpoint.save_work(self.budget)
        return {"output": (result.stdout + "\n" + result.stderr)[-12000:],
                "returncode": result.returncode or (1 if output_errors else 0),
                "exception_info": json.dumps(output_errors) if output_errors else "",
                "execution_record":record}


    def execute(self, request):
        from bioma.runtime.tool_actions import action_identity_scope
        action_id = uuid4().hex
        with action_identity_scope(action_id):
            return self._execute(request, action_id=action_id)

    def _execute(self, request, *, action_id):
        from bioma.runtime.admission import ExecutionAuthorizationError
        from bioma.runtime.deadline import ExecutionTimeout
        request = ActionRequest.model_validate(request)
        check_deadline()
        check_execution()
        current_execution_permit()
        # Protect direct executor callers as well as session restoration. A live
        # object must not bypass the durable journal after a lost response.
        journal = self.trace.parent / "actions.jsonl"
        pending = set()
        if journal.exists():
            for event in map(json.loads, journal.read_text().splitlines()):
                if event['phase'] == 'started': pending.add(event['action_id'])
                elif event['phase'] == 'completed': pending.discard(event['action_id'])
        if pending or self.context.state.get('unresolved_tool_action'):
            from bioma.harness.checkpoint import ReviewContinuationUnavailable
            raise ReviewContinuationUnavailable('UNRESOLVED_TOOL_ACTION: reconcile before another dispatch')
        if self.budget.remaining_tool_calls <= 0:
            raise ModelBudgetUnavailable("Action tool budget exhausted")
        journal = self.trace.parent / "actions.jsonl"
        start = {"action_id": action_id, "phase": "started", "request": request.model_dump(),
                 "workspace_scope": self.context.state.get("artifact_scope_id") or self.context.task_id,
                 "checkpoint_id": getattr(self.checkpoint, "path", Path("uncheckpointed")).stem}
        with journal.open("a") as stream:
            stream.write(json.dumps(start, ensure_ascii=False)+"\n")
        try:
            if request.kind == "shell":
                value = self._shell(request.command, request.timeout, action_id=action_id,
                                    output_files=request.arguments.get('output_files', ()))
            elif request.kind == "tool":
                value = self._tool(request.name, request.arguments)
            elif request.kind == "inspect_tool":
                if request.name not in self.tools:
                    raise ValueError("Unknown tool. Available: " + ", ".join(sorted(self.tools)))
                self.budget.record_tool_call()
                from bioma.runtime.tool_actions import invoke
                schema = invoke("inspect_tool", {"name":request.name},
                                lambda:self.tools[request.name].to_openai_tool())
                value = {"output":json.dumps(schema,ensure_ascii=False),"returncode":0,"exception_info":""}
            elif request.kind == "analysis":
                # The file itself is the selected program: one execution, then
                # ordinary receipt selection/review, never a second shell run.
                args = dict(request.arguments)
                source = _safe_file(self.workspace, request.source)
                args["code"] = source.read_text(encoding="utf-8")
                value = self._tool("execute_analysis_code", args)
            elif request.kind == "submit":
                args = dict(request.arguments)
                args.setdefault("reason", "Submit selected existing computation for independent review.")
                value = self._tool("finish_investigation", args)
            else:  # An explicit safe-point pause, no authority is persisted.
                self.budget.record_tool_call()
                from bioma.runtime.tool_actions import invoke
                invoke("pause", {}, lambda:None)
                self.context.state["action_pause_requested"] = True
                value = {"output":"Paused at a completed action boundary.","returncode":0,"exception_info":""}
        except ComputedPendingReview as exc:
            self.context.state['action_review_pending'] = {'checkpoint':exc.checkpoint,'reason':str(exc)}
            value = {"output":"Existing computation retained; review remains incomplete.",
                     "returncode":1,"exception_info":str(exc)}
        except (ModelTransportError, ModelOutputBudgetError) as exc:
            review_only = request.kind == 'submit' or (
                request.kind == 'tool' and request.name == 'finish_investigation')
            if (not review_only or self.context.state.get('unresolved_tool_action')
                    or not self.checkpoint.path.is_file()):
                raise
            # The finish dispatch returned and its calculation was checkpointed;
            # only the model review failed. Save feedback before pausing, so a
            # fresh invocation can review the same bytes without replaying work.
            self.context.state['action_review_pending'] = {
                'checkpoint':str(self.checkpoint.path),'reason':str(exc)}
            value = {'output':'Existing computation retained; independent review was interrupted.',
                     'returncode':1,'exception_info':type(exc).__name__+': '+str(exc)}
        except (ExecutionAuthorizationError, ExecutionTimeout, ModelBudgetUnavailable, OSError):
            # Started without a completed observation is not automatically replayable.
            raise
        except Exception as exc:
            value = {"output":"", "returncode":1,
                     "exception_info":type(exc).__name__+": "+str(exc)}
        receipt = None
        try:
            receipt = value.get("observation", {}).get("metrics",{}).get("receipt_path")
        except (ValueError,AttributeError):
            pass
        observation = ActionObservation(action_id=action_id, kind=request.kind,
            status="ok" if value["returncode"]==0 else "error", result=value,
            receipt_ref=receipt, receipt_sha256=_digest(Path(receipt)) if receipt else None)
        with journal.open("a") as stream:
            stream.write(json.dumps({"phase":"completed", **observation.model_dump()},ensure_ascii=False)+"\n")
        self.checkpoint.save_work(self.budget)
        return observation


def review_action_history(context):
    """Verified immutable projection of actual actions, not an agent summary."""
    root = context.artifact_dir('action_records')
    journal = root/'actions.jsonl'
    if not journal.exists():
        return None, {}
    raw = journal.read_bytes()
    if len(raw) > 4_000_000:
        raise ValueError('Action history exceeds bounded review capacity')
    digest = hashlib.sha256(raw).hexdigest()
    archive = root/'review_snapshots'; archive.mkdir(exist_ok=True)
    frozen = archive/(digest+'.jsonl')
    if frozen.exists() and frozen.read_bytes()!=raw:
        raise ValueError('Action snapshot identity collision')
    if not frozen.exists(): frozen.write_bytes(raw)
    fingerprints = {str(frozen):digest}
    started, rows = {}, []
    def verified_file(filename, expected):
        path=Path(filename).resolve(); path.relative_to(context.artifact_dir().resolve())
        content=path.read_bytes()
        if hashlib.sha256(content).hexdigest()!=expected:
            raise ValueError('Action record file changed before review')
        fingerprints[str(path)] = expected
        return content
    for entry in map(json.loads,raw.splitlines()):
        phase = entry['phase']
        if phase in {'session_started','session_resumed'}:
            rows.append(entry)
        elif phase=='started':
            if entry['action_id'] in started:
                raise ValueError('Duplicate action identity')
            started[entry['action_id']]=entry
        elif phase=='completed':
            parent=started.pop(entry['action_id'],None)
            if parent is None:raise ValueError('Action observation lacks its request')
            value=entry['result']
            row={'action_id':entry['action_id'],'request':parent['request'],
                 'status':entry['status'],'returncode':value['returncode'],
                 'receipt_ref':entry.get('receipt_ref'), 'exception_info':value.get('exception_info','')}
            if entry.get('receipt_ref'):
                verified_file(entry['receipt_ref'],entry['receipt_sha256'])
            execution=value.get('execution_record')
            if execution:
                if execution['action_id']!=entry['action_id']:
                    raise ValueError('Shell/action identity mismatch')
                row['executed_command']=verified_file(execution['source_archive'],execution['executed_source_sha256']).decode()
                row['process_output']=json.loads(verified_file(execution['output_archive'],execution['output_sha256']))
            rows.append(row)
    # Review is inside the currently dispatched submit/finish action. Any other
    # unresolved action is not silently interpreted as a completed computation.
    for entry in started.values():
        req=entry['request']
        if req['kind'] not in {'submit', 'response'} and not (req['kind']=='tool' and req['name']=='finish_investigation'):
            raise ValueError('Unresolved action cannot support delivery review')
        rows.append({'action_id':entry['action_id'],'request':req,'status':'current_review_in_progress'})
    if len(json.dumps(rows).encode())>100_000:
        raise ValueError('Complete action review material exceeds budget; no truncated approval')
    return {'scope':'Recorded actions, process outputs and admission observations; not scientific Evidence.',
            'snapshot_sha256':digest,'events':rows}, fingerprints


def review_existing_computation(receipt, context, tools, budget, review_llm, checkpoint):
    """One explicitly selected review, with no actor dispatch or recomputation."""
    from bioma.harness.types import HarnessLoopResult
    # Operator-authorized continuation: exactly one existing finish dispatch.
    # Never fall back to actor code generation, even when review fails.
    if not context.state.get('continuous_investigation'):
        raise ValueError('Receipt continuation requires an investigation work package')
    # Selecting a package starts a new review decision. A failed finish
    # must not inherit delivery authority from an earlier in-memory result.
    for key in ('delivery_complete', 'delivery_review', 'delivery_review_rejected'):
        context.state.pop(key, None)
    args = {
        'reason': 'Review and deliver the explicitly selected existing computation.',
        'receipt_refs': [receipt], 'artifact_paths': []}
    if checkpoint.path.is_file():
        saved = json.loads(checkpoint.path.read_text())['observation']['metrics']
        if saved.get('receipt_path') == receipt and saved.get('answer_candidate_ref'):
            args['answer_candidate_ref'] = saved['answer_candidate_ref']
            from .result_proposal import load
            proposal = load(context, saved['answer_candidate_ref'])
            args['receipt_refs'] = saved.get('selected_support_receipt_refs') or [receipt]
            args['artifact_paths'] = proposal.get('artifact_refs') or []
    feedback, _ = call_harness_tool('finish_investigation', json.dumps(args),
        {tool.name: tool for tool in tools if tool.name == 'finish_investigation'},
        context, budget, review_llm, checkpoint)
    observation = json.loads(feedback)
    complete = bool(context.state.get('delivery_complete'))
    rejected = observation.get('metrics', {}).get('error_type') == 'DeliveryReviewRejected'
    if not complete:
        context.state.update(
            error_code='DELIVERY_REVIEW_REJECTED' if rejected else 'DELIVERY_SUBMISSION_FAILED',
            error_detail=observation['summary'])
    return HarnessLoopResult(
        answer=(context.state.get('final_answer') or 'Existing computation reviewed.') if complete else observation['summary'],
        exit_reason='completed' if complete else
            ('review_rejected' if rejected else 'delivery_submission_failed'))
