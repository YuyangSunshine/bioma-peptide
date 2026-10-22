"""Bounded Method requests inside a live investigation.

The existing router, WP1 and verifier remain authoritative. Reviewed subordinate
work is retained in the runtime ledger, not silently committed as scientific
Evidence or as completion of the enclosing research contract.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from pydantic import Field

from bioma.core.models import StrictModel, TaskRequest
from bioma.core.research import fingerprint
from bioma.evidence.packages import file_digest

_active = ContextVar("bioma_capability_requests", default=None)


class CapabilityDiscovery(StrictModel):
    capability: str | None = Field(default=None,
        description="Exact registered capability identifier, not a search phrase. Omit to list the accessible catalog.")
    method_id: str | None = Field(default=None,
        description="Exact Method identifier returned by the catalog. Omit to list Methods.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6, ge=1, le=20)
    input_offset: int = Field(default=0, ge=0)


class ResultInput(StrictModel):
    result_id: str = Field(min_length=1,
        description="Exact native WorkResult id from discovery or a Method return; not a file path or Ledger event id.")
    role: str | None = Field(default=None, min_length=1,
        description="Optional named reference field from the selected Method input_schema. Omit for generic upstream input, including draft_study.")


class CapabilityRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    capability: str
    method_id: str
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    input_bindings: dict[str, str] = Field(default_factory=dict,
        description="Named reference fields declared by the Method schema; values must be selected input_refs.")
    result_inputs: list[ResultInput] = Field(default_factory=list,
        description="Explicit native result selections, each supplied once with an optional Method input role. Use instead of input_refs/input_bindings for native results. Runtime resolves provenance, not scientific applicability.")
    skill_refs: list[str] = Field(default_factory=list)


@contextmanager
def capability_request_scope(runtime, state, message):
    token = _active.set((runtime, state, message) if runtime else None)
    try:
        yield
    finally:
        _active.reset(token)


def _scope():
    from bioma.runtime.admission import check_execution, current_execution_permit
    check_execution()
    current_execution_permit()
    scope = _active.get()
    if scope is None or scope[0].ledger is None:
        raise ValueError("Capability requests require a live, durable Runtime assignment")
    return scope


def _restricted(backend, binding):
    return (backend.family == "scientific.execution"
            or binding.method.action_contract.resources
            or binding.method.action_contract.risk_level != "low"
            or any(t.external_effect and not t.replay_safe for t in binding.tools))


def discover(*, capability=None, method_id=None):
    runtime, state, message = _scope()
    rows = []
    owners = [owner for owner in dict.fromkeys(runtime.agents.values())
              if not state.active_agents or owner.provider_id in state.active_agents]
    families = sorted({owner.family for owner in owners if getattr(owner, 'bindings', None)})
    if capability is not None and capability not in families:
        raise ValueError("Unknown capability identifier; use an exact accessible identifier or omit the filter. "
                         "Accessible identifiers: " + ", ".join(families))
    methods = sorted({binding.method.id for owner in owners
                      if capability is None or owner.family == capability
                      for binding in getattr(owner, 'bindings', {}).values()
                      if binding.method.id != 'continuous_scientific_investigation'})
    if method_id is not None and method_id not in methods:
        raise ValueError("Unknown Method identifier for this scope. Accessible identifiers: " + ", ".join(methods))
    for owner in owners:
        if capability and owner.family != capability:
            continue
        for binding in getattr(owner, "bindings", {}).values():
            method = binding.method
            if method.id == "continuous_scientific_investigation" or (method_id and method.id != method_id):
                continue  # A request cannot create a second general Agent loop.
            request_schema = CapabilityRequest.model_json_schema()
            request_schema['properties']['capability']['const'] = owner.family
            request_schema['properties']['method_id']['const'] = method.id
            rows.append({
                "capability": owner.family, "provider_id": owner.provider_id,
                "method_id": method.id, "description": method.description,
                **({"input_schema": method.input_schema,
                    "result_contract": method.result.model_dump(mode="json"),
                    "request_action": {"kind":"tool", "name":"request_scientific_capability",
                                       "arguments_schema":request_schema}} if method_id else
                   {"required_input_fields": method.input_schema.get("required", []),
                    "result_kinds": method.result.evidence_kinds}),
                "required_input_kinds": method.required_input_kinds,
                "required_input_qualification": method.required_input_qualification,
                "limitations": list(method.limitations),
                "resources": method.action_contract.resources,
                "available": method.available,
                "requires_separate_authorization": bool(_restricted(owner, binding)),
                "authorization_configured": (state.task_id, owner.family, method.id)
                    in runtime.capability_authorizations,
                "execution_action": "request_scientific_capability",
                "execution_namespace": "professional_method",
                "authority": "discovery_only",
            })
    return sorted(rows, key=lambda row: (row["capability"], row["method_id"], row["provider_id"]))


def _files(state):
    """Bind admitted file bytes, without asserting actual reads or field semantics."""
    paths = list(state.inputs.get("files", []))
    paths += [row["uri"] for row in state.inputs.get("source_inputs", [])
              if isinstance(row, dict) and row.get("uri")]
    paths += [a.uri for a in state.artifacts]
    result = {}
    for name in paths:
        path = Path(name)
        if not path.is_file():
            raise ValueError("Admitted input file unavailable: " + str(name))
        result[str(path.resolve())] = file_digest(path)
    return result


def _reviewed_result_inputs(runtime, state, parent):
    """Project reusable raw WorkResults from native events, never create Evidence.

    Eligibility is recalculated under the live parent permit. The original event
    remains the authority; no result cache or second scientific ledger is written.
    """
    from bioma.core.models import Observation, Artifact
    events = [event for event in runtime.ledger.events(state.task_id)
              if event.kind == "CapabilityRequestReviewed"
              and event.payload.get("parent_assignment_id") == (parent.task_request_id or parent.id)]
    if events and not runtime.ledger.verify_chain(state.task_id):
        raise ValueError("Capability result ledger failed integrity verification")
    results = {}
    for event in events:
        payload = event.payload
        feedback = payload["observation"]
        gaps = []
        if feedback.get("status") != "reviewed" or feedback.get("outcome") != "completed":
            gaps.append("execution_not_reviewed_and_completed")
        artifacts = [Artifact.model_validate(row) for row in payload["delta"].get("artifacts", [])]
        versions = dict(payload["input_manifest"])
        for artifact in artifacts:
            expected = artifact.metadata.get("sha256")
            if not expected or (artifact.uri in versions and versions[artifact.uri] != expected):
                gaps.append("missing_or_conflicting_artifact_identity")
            versions[artifact.uri] = expected
        for filename, expected in versions.items():
            if not Path(filename).is_file() or file_digest(filename) != expected:
                gaps.append("recorded_bytes_missing_or_changed")
        for raw in payload["delta"].get("updates", {}).get("observations", []):
            observation = Observation.model_validate(raw)
            if observation.kind != "work_result":
                continue
            if observation.id in results:
                raise ValueError("Duplicate native capability result identity")
            result_files = set(observation.artifact_ids)
            result_files.update(row.get("id") for row in observation.value.get("artifacts", [])
                                if isinstance(row, dict))
            results[observation.id] = {
                "observation": observation,
                "artifacts": [item for item in artifacts if item.id in result_files],
                "ledger_event_ref": event.event_id,
                "gaps": sorted(set(gaps)),
            }
    return results


def discover_inputs():
    """Expose admitted inputs and explicitly unqualified native result identities."""
    runtime, state, parent = _scope()
    rows = []
    for item in (*state.observations, *state.evidence, *state.artifacts):
        rows.append({"id": item.id, "kind": item.kind, "source": item.source,
            "identity": fingerprint(item.model_dump(mode="json")),
            "label": (getattr(item, "claim", "") or
                      Path(getattr(item, "uri", "")).name or item.kind)[:500]})
    for item in state.inputs.get("source_inputs", []):
        if isinstance(item, dict) and item.get("id"):
            rows.append({"id": item["id"], "kind": "source_input",
                         "identity": fingerprint(item), "label": item.get("uri", "")})
    for identifier, record in _reviewed_result_inputs(runtime, state, parent).items():
        item = record["observation"]
        rows.append({"id": identifier, "kind": "work_result", "source": item.source,
            "identity": fingerprint(item.model_dump(mode="json")),
            "label": item.value.get("kind", "work_result"),
            "ledger_event_ref": record["ledger_event_ref"],
            "available_for_input": not record["gaps"], "gaps": record["gaps"],
            "scientific_support": "not_assessed"})
    return sorted(rows, key=lambda row: (row["kind"], row["id"]))


def read_result(event_ref):
    """Read a native reviewed-work event under fresh parent authority."""
    runtime, state, parent = _scope()
    if not runtime.ledger.verify_chain(state.task_id):
        raise ValueError("Capability result ledger failed integrity verification")
    event = next((e for e in runtime.ledger.events(state.task_id)
                  if e.event_id == event_ref and e.kind == "CapabilityRequestReviewed"), None)
    if (event is None or event.payload.get("parent_assignment_id") !=
            (parent.task_request_id or parent.id)):
        raise ValueError("Capability result is outside the current parent assignment")
    return event


def delivery_result(event_ref, expected_hash=None):
    """Revalidate a native child return for parent contract review, not Evidence.

    The live parent permit and original Ledger remain authoritative. No copied
    receipt or result registry can substitute for either.
    """
    event = read_result(event_ref)
    if expected_hash is not None and event.event_hash != expected_hash:
        raise ValueError("Native Method receipt identity changed")
    runtime, state, parent = _scope()
    records = [row for row in _reviewed_result_inputs(runtime, state, parent).values()
               if row["ledger_event_ref"] == event_ref]
    if len(records) != 1 or any(row["gaps"] for row in records):
        raise ValueError("Native Method result is incomplete or its recorded bytes changed")
    from bioma.core.scientific_capabilities import WorkResult
    for row in records:
        WorkResult.model_validate(row["observation"].value)
    return event


def request(arguments, *, budget, persist_budget=None):
    runtime, state, parent = _scope()
    args = CapabilityRequest.model_validate(arguments)
    if args.method_id == "continuous_scientific_investigation":
        raise ValueError("Recursive Agent investigations are not capability requests")
    native_inputs = _reviewed_result_inputs(runtime, state, parent)
    if args.result_inputs:
        if args.input_refs or args.input_bindings:
            raise ValueError("Use result_inputs or input_refs/input_bindings, not both")
        selected_refs = []
        named_refs = {}
        for selection in args.result_inputs:
            if selection.result_id not in native_inputs:
                raise ValueError("result_id must identify a native WorkResult in this parent assignment")
            if selection.role is not None:
                if selection.role in named_refs:
                    raise ValueError("Each named input role must be selected exactly once")
                named_refs[selection.role] = selection.result_id
            if selection.result_id not in selected_refs:
                selected_refs.append(selection.result_id)
        args = args.model_copy(update={"input_refs":selected_refs,
            "input_bindings":named_refs, "result_inputs":[]})
    selected_results = [native_inputs[ref] for ref in dict.fromkeys(args.input_refs) if ref in native_inputs]
    if any(record["gaps"] for record in selected_results):
        raise ValueError("Selected professional result cannot be reused: " + ", ".join(sorted(
            {gap for record in selected_results for gap in record["gaps"]})))
    observations = {item.id: item for item in state.observations}
    artifacts = {item.id: item for item in state.artifacts}
    for record in selected_results:
        for item, destination in [(record["observation"], observations),
                                  *((artifact, artifacts) for artifact in record["artifacts"])]:
            if item.id in destination and destination[item.id] != item:
                raise ValueError("Selected result conflicts with a parent-admitted identity")
            destination[item.id] = item
    # Narrowing a later child may include explicitly selected raw observations,
    # not qualified Evidence or a mutation of the parent's formal scientific state.
    state = state.model_copy(update={"observations": list(observations.values()),
                                     "artifacts": list(artifacts.values())}, deep=True)
    input_events = {record["observation"].id: record["ledger_event_ref"] for record in selected_results}
    known = {x.id for x in (*state.observations, *state.evidence, *state.artifacts)}
    known |= {x["id"] for x in state.inputs.get("source_inputs", [])
              if isinstance(x, dict) and x.get("id")}
    if set(args.input_refs) - known:
        raise ValueError("Input references are outside the parent-admitted scientific state")
    runtime.refresh_providers()
    candidates = [(owner, owner.bindings[args.method_id])
                  for owner in dict.fromkeys(runtime.agents.values())
                  if getattr(owner, "family", None) == args.capability
                  and (not state.active_agents or owner.provider_id in state.active_agents)
                  and args.method_id in getattr(owner, "bindings", {})]
    if not candidates:
        raise ValueError("No registered Method matches the capability request")
    reserved = {"inputs", "method_id", "objective", "input_refs", "execution_requirements",
                "provider_binding", "accepted_offer", "result_requirements", "budget",
                "research_assignment", "delivery_contracts", "input_receipt_refs"}
    if set(args.input_bindings) & reserved or set(args.input_bindings.values()) - set(args.input_refs):
        raise ValueError("Named inputs must bind selected scientific references, not execution authority")
    for _, binding in candidates:
        if set(args.input_bindings) - set(binding.method.input_schema.get("properties", {})):
            raise ValueError("Named input binding is absent from the selected Method schema")
    # Reject before routing/execution. Inventory alone is not a user mandate.
    if any(_restricted(owner, binding) for owner, binding in candidates):
        if (state.task_id, args.capability, args.method_id) not in runtime.capability_authorizations:
            runtime.ledger.append(state.task_id, "CapabilityAuthorizationRequired", parent.recipient,
                {"parent_message_id": parent.id, "request": args.model_dump(), "executed": False})
            return {"status": "authorization_required", "request": args.model_dump(),
                    "executed": False, "progress_retained": True}
    identifier = fingerprint([state.task_id, parent.task_request_id or parent.id, args.request_id])
    input_files = _files(state)
    identity = fingerprint({"request": args.model_dump(), "inputs": state.inputs,
                            "files": input_files, "input_receipt_refs": input_events,
                            "references": [x.model_dump(mode="json") for x in
                                (*state.observations, *state.evidence, *state.artifacts)
                                if x.id in args.input_refs]})
    task = TaskRequest(id=identifier, requester=parent.recipient,
        required_capability=args.capability, objective=args.objective,
        message_type=candidates[0][1].method.message_type,
        payload={**args.input_bindings, "inputs": state.inputs, "input_refs": args.input_refs,
                 "input_receipt_refs": input_events,
                 "objective": args.objective, "method_id": args.method_id,
                 "execution_requirements": {"method_id": args.method_id}},
        correlation_id=parent.id)
    decision = runtime.capability_router.route(task, active_agents=set(state.active_agents or runtime.agents),
        available_resources=runtime.available_resources, ttl=1,
        capability_binding=args.capability, method_binding=args.method_id)
    if len(decision.messages) != 1:
        return {"status": "unavailable", "executed": False,
                "qualification": list(decision.qualification)}
    message = decision.messages[0]
    runtime.provider_pool.check_assignment(message)
    selected = runtime.agents[message.recipient]
    selected_binding = selected.bindings[args.method_id]
    from bioma.runtime.engine import _authorized_state_projection
    from bioma.core.method_inputs import method_input_reasons
    child_state = _authorized_state_projection(state, message.recipient, message=message,
        backend=selected)
    qualification = method_input_reasons(selected_binding.method, child_state, args.input_refs,
                                         runtime.available_resources)
    if selected_binding.method.required_input_qualification and input_events:
        # A producer's provenance strings cannot self-qualify a native result.
        qualification.append("Native WorkResult inputs remain unqualified; independent qualification is required")
    if qualification:
        return {"status": "unavailable", "executed": False, "qualification": qualification}
    missing = set(selected_binding.method.action_contract.resources) - set(runtime.available_resources or ())
    if missing:
        return {"status": "unavailable", "executed": False, "missing_resources": sorted(missing)}
    history = [e for e in runtime.ledger.events(state.task_id)
               if e.kind.startswith("CapabilityRequest")]
    pending = set()
    for event in history:
        payload = event.payload
        if payload.get("parent_assignment_id") != (parent.task_request_id or parent.id):
            continue
        key = payload["request_id"]
        if event.kind == "CapabilityRequestStarted":
            pending.add(key)
        elif event.kind == "CapabilityRequestReviewed":
            if payload["observation"]["status"] != "reconciliation_required":
                pending.discard(key)
        elif event.kind == "CapabilityRequestInterrupted" and not payload.get("retry_requires_reconciliation", True):
            pending.discard(key)
    if pending - {identifier}:
        raise ValueError("Unresolved capability execution: reconcile before starting a new request ID")
    events = [e for e in history if e.payload.get("request_id") == identifier]
    if events:
        first = events[0]
        if first.payload["identity"] != identity:
            raise ValueError("Capability request ID reused with changed inputs or intent")
        completed = next((e for e in events if e.kind == "CapabilityRequestReviewed"), None)
        if completed:
            if not runtime.ledger.verify_chain(state.task_id):
                raise ValueError("Capability result ledger failed integrity verification")
            payload = completed.payload
            for item in payload["delta"].get("artifacts", []):
                if file_digest(item["uri"]) != item["metadata"]["sha256"]:
                    raise ValueError("Prior capability artifact changed; reassessment required")
            return {**payload["observation"], "reused": True, "ledger_event_ref": completed.event_id}
        raise ValueError("Previous capability attempt has no reusable reviewed result; reconcile before retry")
    runtime.ledger.append(state.task_id, "CapabilityRequestStarted", parent.recipient,
        {"request_id": identifier, "identity": identity, "parent_message_id": parent.id,
         "parent_assignment_id": parent.task_request_id or parent.id,
         "input_manifest": input_files,
         "request": args.model_dump(), "message": message.model_dump(mode="json")},
        idempotency_key="capability-request:" + identifier)
    from bioma.runtime.admission import TransitionAdmission, check_execution, _execution_check
    from bioma.llm import delegated_budget
    parent_guard = _execution_check.get()
    def check_child_assignment(candidate):
        from bioma.runtime.admission import ExecutionAuthorizationError
        if parent_guard:
            parent_guard()  # Parent lifetime/revocation, not its tool allowlist.
        runtime.provider_pool.check_assignment(candidate)
        if (_restricted(selected, selected_binding)
                and (state.task_id, args.capability, args.method_id) not in runtime.capability_authorizations):
            raise ExecutionAuthorizationError("Capability-specific authorization was revoked")
        if set(selected_binding.method.action_contract.resources) - set(runtime.available_resources or ()):
            raise ExecutionAuthorizationError("Capability resources are no longer available")

    admission = TransitionAdmission(runtime.ledger, available_resources=runtime.available_resources,
        memory=runtime.memory, provider_check=check_child_assignment)
    # No new loop, deadline, task budget, registry, or tool grants are inherited.
    # Child admission replaces and then restores the parent permit/tool scope.
    try:
        # The narrowed view includes only explicitly selected native raw results;
        # never reload broader root state or upgrade them to qualified Evidence.
        if child_state.gateway.get("scoped_capability_context"):
            child_state.gateway["scoped_capability_context"].pop("scientific_action_ref", None)
        with delegated_budget(budget, persist=persist_budget):
            delta = admission.execute(runtime.agents[message.recipient], child_state, message)
        check_execution()
        if _files(state) != input_files:
            raise ValueError("Input identity changed during capability execution")
        # A child Method's completed work cannot close the enclosing contract.
        review_delta = delta.model_copy(update={"outcome": "continue"}) if delta.outcome == "completed" else delta
        check, receipt = runtime.verifier.verify_with_receipt(
            child_state, review_delta, expected_agent=message.recipient, source_message=message)
        observation = {"status": "reviewed" if check.ok else "review_rejected",
            "request_id": identifier, "parent_message_id": parent.id,
            "child_message_id": message.id, "outcome": delta.outcome,
            "status_reason": delta.status_reason,
            "work_results": delta.updates.get("observations", []),
            "artifacts": [a.model_dump(mode="json") for a in delta.artifacts],
            "reasons": list(check.reasons), "scientific_support": "not_assessed",
            "authority": "execution_feedback_only", "contract_complete": False}
        if any(row.get("value", {}).get("kind") == "EffectUncertain"
               for row in observation["work_results"]):
            observation.update(status="reconciliation_required", outcome="uncertain")
        event = runtime.ledger.append(state.task_id, "CapabilityRequestReviewed", "harness",
            {"request_id": identifier, "identity": identity,
             "parent_assignment_id": parent.task_request_id or parent.id,
             "input_manifest": input_files,
             "delta": review_delta.model_dump(mode="json"), "receipt": receipt.model_dump(mode="json"),
             "message": message.model_dump(mode="json"), "before": child_state.model_dump(mode="json"),
             "observation": observation})
        return {**observation, "ledger_event_ref": event.event_id, "reused": False}
    except Exception as exc:
        from bioma.runtime.admission import ExecutionAuthorizationError
        known_local_failure = (not _restricted(selected, selected_binding)
            and isinstance(exc, ValueError) and not isinstance(exc, ExecutionAuthorizationError))
        runtime.ledger.append(state.task_id, "CapabilityRequestInterrupted", "harness",
            {"request_id": identifier, "identity": identity, "error_type": type(exc).__name__,
             "parent_assignment_id": parent.task_request_id or parent.id,
             "retry_requires_reconciliation": not known_local_failure,
             "progress_retained": True, "automatic_retry": False})
        raise
