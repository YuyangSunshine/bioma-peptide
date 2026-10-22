"""Expose configured scientific tools to Com without a Pred-owned private path."""

from __future__ import annotations

import hashlib
import json
import platform
from uuid import uuid4
from pydantic import Field

from bioma.harness.types import HarnessTool, ToolObservation
from bioma.harness.types import HarnessToolInput


class SearchInput(HarnessToolInput):
    query: str = Field(
        description="Search terms for scientific tool names or descriptions."
    )


class InvokeInput(HarnessToolInput):
    skill_refs: list[str] = Field(default_factory=list, description='Successfully read stable skill IDs informing this selection; optional, audited and not scientific credit.')
    tool_name: str
    arguments: dict = Field(default_factory=dict)


def discovery_tools(registry, wrappers):
    if registry is None:
        return []
    from bioma.capabilities.scoped import tool_identity
    by_name = {tool.name: tool for tool in wrappers}

    def search(context, args):
        terms = args["query"].casefold().split()
        ranked = sorted(
            (tool for tool in registry.all() if tool.name in by_name),
            key=lambda tool: sum(
                word in (tool.name + " " + tool.description).casefold()
                for word in terms
            ),
            reverse=True,
        )
        matches = []
        for tool in ranked[:8]:
            identity = tool_identity(tool)
            wrapper = by_name[tool.name]
            if wrapper.source_tool is not tool or wrapper.implementation_identity != identity:
                raise ValueError("Scientific tool discovery binding changed: " + tool.name)
            definition = tool.to_openai_tool()
            definition["function"]["x-bioma"] = {
                "version": tool.version,
                "implementation_identity": identity,
                "result_kind": tool.result_kind,
            }
            matches.append(definition)
        return ToolObservation(
            tool_name="find_scientific_tools",
            status="ok",
            summary="Configured tool schemas; availability of a schema does not prove service health.",
            metrics={"tools": matches},
            patch={'discovered_tool_schemas': {
                **context.state.get('discovered_tool_schemas', {}),
                **{item['function']['name']: item for item in matches}}},
        )

    def invoke(context, args):
        tool = by_name.get(args["tool_name"])
        if tool is None:
            raise ValueError(
                "Unknown configured scientific tool: " + args["tool_name"]
                + "; invoke_scientific_tool accepts only names returned by find_scientific_tools. "
                "If this name came from discover_scientific_capabilities, it is a professional Method "
                "and must be executed with request_scientific_capability. Available configured tool names: "
                + ", ".join(sorted(by_name))
            )
        return tool.call(context, args["arguments"])

    return [
        HarnessTool(
            name="find_scientific_tools",
            description="Discover installed scientific tools and their exact argument schemas; search before invocation.",
            args_schema=SearchInput,
            handler=search,
        ),
        HarnessTool(
            name="invoke_scientific_tool",
            description="Invoke only a configured Tool name returned by find_scientific_tools. Professional Method IDs returned by discover_scientific_capabilities must instead use request_scientific_capability. Produces a durable receipt. Required external services must actually be called.",
            args_schema=InvokeInput,
            handler=invoke,
        ),
    ]


def _execution_input_files(arguments, files, downloaded):
    """Bind original inputs and downloaded files explicitly passed to this executor.

    Discovery documents remain in recorded retrieval receipts; merely downloading
    a document does not mean a registered executor read it. Scalar inputs such as
    an amino-acid sequence are bound by the complete tool argument receipt.
    """
    def values(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from values(item)
    referenced = set(values(arguments))
    return [p for p in files if p not in downloaded or p in referenced]


def shared_tools(registry):
    if registry is None:
        return []
    from bioma.capabilities.scoped import tool_identity
    wrappers = []
    for tool in registry.all():

        def invoke(context, args, tool=tool):
            if tool.external_effect and not tool.replay_safe:
                raise ValueError(
                    "Non-replay-safe external actions require an Oper-managed durable action plan; Com cannot dispatch them directly."
                )
            action_id = uuid4().hex
            from bioma.capabilities.scoped import authorize_tool_invocation
            grant = authorize_tool_invocation(tool, args)
            execution_error = None
            failure_diagnostic = None
            try:
                result = tool.call(action_instance_id=action_id, **args)
            except Exception as exc:
                from bioma.runtime.admission import ExecutionAuthorizationError
                from bioma.runtime.deadline import ExecutionTimeout
                from bioma.llm import ModelBudgetUnavailable
                if isinstance(exc, (ExecutionAuthorizationError, ExecutionTimeout,
                                    ModelBudgetUnavailable)):
                    raise
                if not tool.replay_safe:
                    raise
                from bioma.tools.errors import ToolError, requests_tool_error
                try:
                    import requests
                    if isinstance(exc, requests.exceptions.RequestException):
                        exc = requests_tool_error(exc, backend=tool.name)
                except ImportError:
                    pass
                execution_error = {
                    'type': type(exc).__name__,
                    'message': str(exc)[:2000],
                    'retriable': bool(getattr(exc, 'retriable', False)),
                }
                if isinstance(exc, ToolError):
                    execution_error.update({
                        'error_type': exc.error_type,
                        'backend': exc.backend,
                        'retry_after_seconds': exc.retry_after_seconds,
                    })
                from bioma.harness.diagnostics import diagnostic_context
                failure_diagnostic = diagnostic_context(tool.name, exc)
                result = {'status': 'failed', 'error': execution_error}
            result_validated = False
            validation_error = None
            if execution_error is None and tool.result_validator is not None:
                try:
                    result_validated = tool.result_validator(result, args) is True
                except Exception as exc:
                    from bioma.runtime.admission import ExecutionAuthorizationError
                    from bioma.runtime.deadline import ExecutionTimeout
                    from bioma.llm import ModelBudgetUnavailable
                    if isinstance(exc, (ExecutionAuthorizationError, ExecutionTimeout, ModelBudgetUnavailable)):
                        raise
                    validation_error = {'type':type(exc).__name__, 'message':str(exc)}
            validation_failed = (execution_error is None and
                                 tool.result_validator is not None and not result_validated)
            failed = execution_error is not None or validation_failed or (isinstance(result, dict) and (
                result.get("status") in {"error", "failed", "blocked"}
                or result.get("success") is False
            ))
            receipt = context.artifact_dir("tool_receipts") / (action_id + ".json")
            payload = {
                "tool": tool.name,
                "version": tool.version,
                "action_id": action_id,
                "arguments": args,
                "arguments_sha256": hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest(),
                "result": result,
                "result_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest(),
                "tool_implementation_identity": tool_identity(tool),
                "tool_grant_ref": grant.identity if grant is not None else None,
                "parent_scientific_action_ref": grant.parent_scientific_action_ref if grant is not None else None,
                "result_kind": tool.result_kind,
                "result_validated": result_validated,
                "validation_error": validation_error,
                "execution_error": execution_error,
                "runtime": {"python": platform.python_version()},
                "status": "failed" if failed else "ok",
            }
            from bioma.runtime.tool_actions import current_tool_action
            payload["dispatch"] = current_tool_action()
            receipt.write_text(
                json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
            )
            previous = list(context.state.get("tool_receipts") or [])
            previous.append(
                {
                    "tool": tool.name,
                    "status": payload["status"],
                    "path": str(receipt),
                    "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                    "tool_implementation_identity": payload["tool_implementation_identity"],
                    "tool_grant_ref": payload["tool_grant_ref"],
                    "arguments_sha256": payload["arguments_sha256"],
                    "result_sha256": payload["result_sha256"],
                    "result_kind": payload["result_kind"],
                    "result_validated": payload["result_validated"],
                }
            )
            # The tool has returned and its native receipt is durable. Keep its
            # identity even if subsequent file admission or packaging fails.
            context.state['tool_receipts'] = previous
            try:
                admitted = dict(context.state.get('admitted_tool_inputs') or {})
                # A successful typed result can expose files, never an arbitrary path
                # supplied by the actor. Bind each file to this exact tool receipt.
                if result_validated and not failed and isinstance(result, dict):
                    from pathlib import Path
                    files = result.get('files') or {}
                    selected = result.get('analysis_inputs', list(files))
                    if not isinstance(selected, list) or any(name not in files for name in selected):
                        raise ValueError('Tool analysis inputs must belong to its validated file manifest')
                    for filename in selected:
                        digest = files[filename]
                        path = Path(filename).resolve()
                        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                            raise ValueError('Tool output file identity mismatch')
                        admitted[str(path)] = {'sha256': digest, 'receipt_path': str(receipt),
                                               'receipt_sha256': previous[-1]['sha256']}
                package = None
                if (context.state.get('harness_mode', 'current') != 'current'
                        and result_validated and not failed and isinstance(result, dict)
                        and tool.result_kind in {'calculation', 'computed_prediction'}
                        and result.get('files')):
                    # Registered scientific executors already performed the work.
                    # Package their declared outputs mechanically; do not ask the
                    # actor to write a dummy script merely to manufacture a finish.
                    from dataclasses import asdict
                    from pathlib import Path
                    import shutil
                    selected = result.get('analysis_inputs', list(result['files']))
                    folder = context.artifact_dir('delivery_packages', action_id)
                    output = folder / 'outputs'
                    output.mkdir()
                    paths = []
                    for filename in selected:
                        target = output / Path(filename).name
                        if target.exists():
                            raise ValueError('Scientific tool outputs have conflicting basenames')
                        shutil.copyfile(filename, target)
                        if hashlib.sha256(target.read_bytes()).hexdigest() != result['files'][filename]:
                            raise ValueError('Scientific tool output changed during packaging')
                        paths.append(str(target))
                    if paths:
                        bound_receipt = folder / 'receipt.json'
                        shutil.copyfile(receipt, bound_receipt)
                        sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        package = ToolObservation(tool.name, 'ok',
                            'Validated tool computation packaged for independent delivery review; not Evidence.',
                            artifacts=paths,
                            metrics={'summary': result, 'summary_status': 'published',
                                'receipt_path': str(bound_receipt), 'runtime': payload['runtime'],
                                'input_sha256': {p: sha(p) for p in _execution_input_files(
                                    args, context.state.get('raw_data_sources', []),
                                    context.state.get('admitted_tool_inputs', {}))},
                                'sha256': {p: sha(p) for p in paths},
                                'execution_sha256': {str(bound_receipt): sha(bound_receipt), str(receipt): sha(receipt)}},
                            patch={'analysis_code_paths': [], 'execution_receipts': [str(bound_receipt)],
                                'analysis_results': {'analysis_outputs': {'registered_tool': paths}}})
                        package.metrics['dispatch'] = payload['dispatch']
                        package.metrics['execution_arguments'] = args
            except Exception as exc:
                previous[-1]['status'] = 'processing_failed'
                previous[-1]['processing_error'] = {'type':type(exc).__name__, 'message':str(exc)}
                raise
            return ToolObservation(
                tool_name=tool.name,
                status="error" if failed else "ok",
                summary=("Configured scientific tool execution failed; a durable failure receipt was retained. Retry metadata applies only to automatic unchanged replay; the Agent may choose a later authorized action from current facts."
                         if execution_error else
                         "Shared scientific Tool result failed its registered validator; raw result retained."
                         if validation_failed else "Configured scientific tool returned a recorded result."),
                metrics={"result": result, "receipt_path": str(receipt),
                         **({"diagnostic_context": failure_diagnostic} if failure_diagnostic else {})},
                warnings=["TOOL_EXECUTION_FAILED"] if execution_error else [],
                execution_package=asdict(package) if package else None,
                patch={**({'delivery_complete':False} if package else {}), "tool_receipts": previous, "admitted_tool_inputs": admitted,
                       "raw_data_sources": list(dict.fromkeys([
                           *context.state.get('raw_data_sources', []), *admitted]))},
            )

        wrappers.append(
            HarnessTool(
                name=tool.name,
                description=tool.description,
                args_schema=tool.args_schema,
                handler=invoke,
                version=tool.version,
                implementation_identity=tool_identity(tool),
                result_kind=tool.result_kind,
                source_tool=tool,
            )
        )
    return wrappers
