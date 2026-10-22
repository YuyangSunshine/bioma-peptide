"""Deterministic evidence acceptance shared by owners, Runtime and reviewers."""
from bioma.core.research import AUDIT_KINDS, RequirementAssessment, fingerprint
from bioma.evidence.packages import file_digest
from bioma.core.action_contracts import lookup, MISSING
import math


def _predicate(value, predicate):
    value = lookup(value, predicate.field)
    if value is MISSING:
        return False
    if predicate.operator in {'eq', 'gte', 'lte'} and any(
            type(v) in {int, float} and not math.isfinite(v) for v in (value, predicate.value)):
        return False
    if predicate.operator == "exists":
        return value is not None
    if predicate.operator == "eq":
        numeric = type(value) in {int, float} and type(predicate.value) in {int, float}
        return (numeric or type(value) is type(predicate.value)) and value == predicate.value
    if predicate.operator == "contains":
        return isinstance(value, (str, list, dict)) and predicate.value in value
    if type(value) not in {int, float} or type(predicate.value) not in {int, float}:
        return False
    return value >= predicate.value if predicate.operator == "gte" else value <= predicate.value


def _authoritative_requirement(state, requirement):
    """A legacy execution DTO cannot weaken a v2 acceptance obligation."""
    from bioma.core.research import contract_for
    contract = contract_for(state)
    if contract and contract.schema_version == 'research-contract-v2':
        actual = next((r for r in contract.requirements if r.id == requirement.id), None)
        if actual is None:
            raise ValueError('unknown contract acceptance obligation')
        return actual
    return requirement


def matching_evidence(state, requirement, *, _path=frozenset()):
    requirement = _authoritative_requirement(state, requirement)
    from bioma.core.research_contract import AcceptanceObligation
    from bioma.core.research import contract_for
    stable_contract = contract_for(state) if isinstance(requirement, AcceptanceObligation) else None
    # Eventual completion of a parent obligation cannot retroactively turn an
    # older output into evidence that consumed that parent's scientific result.
    if requirement.id in _path:
        return []
    dependencies = []
    if requirement.depends_on:
        from bioma.core.research import contract_for
        contract = contract_for(state)
        by_requirement = {r.id: r for r in contract.requirements} if contract else {}
        for key in requirement.depends_on:
            parent = by_requirement.get(key)
            if parent is None:
                return []
            if parent.mode == 'claim_assessment':
                # Claim judgement and accepted review basis have their own
                # current-snapshot gate in core.acceptance.
                continue
            candidates = matching_evidence(state, parent, _path=_path | {requirement.id})
            if len(candidates) < parent.min_count:
                return []
            dependencies.append(({e.id for e in candidates}, parent.min_count))
    by_evidence = {e.id: e for e in state.evidence}
    def ancestors(evidence):
        seen = set()
        pending = list(evidence.parent_ids)
        while pending:
            key = pending.pop()
            if key in seen or key not in by_evidence:
                continue
            seen.add(key)
            pending.extend(by_evidence[key].parent_ids)
        return seen
    matches = []
    for evidence in state.evidence:
        if evidence.kind in AUDIT_KINDS or evidence.kind not in requirement.evidence_kinds:
            continue
        value = evidence.value if isinstance(evidence.value, dict) else {}
        if (evidence.method == 'analysis_precondition_check' or value.get('success') is False
                or value.get('status') in {'failed', 'blocked', 'error'}):
            continue
        package = value.get("evidence_package") or {}
        method_id = getattr(requirement, 'method_id', None)
        capability = getattr(requirement, 'capability', None)
        if method_id and package.get('method_id') != method_id:
            continue
        if method_id and capability and package.get('responsibility') != capability:
            continue
        if stable_contract:
            request = next((r for r in state.task_requests if r.id == package.get('task_request_id')), None)
            engine = (request.payload.get('execution_requirements') or {}).get('engine') if request else None
            restrictions = [r for r in stable_contract.method_restrictions
                            if r.obligation_ref is None or r.obligation_ref == requirement.id]
            if any((r.method_id and package.get('method_id') != r.method_id) or
                   (r.capability and package.get('responsibility') != r.capability) or
                   (r.engine and engine != r.engine) for r in restrictions):
                continue
        if requirement.input_hash and requirement.input_hash not in {value.get("input_hash"), package.get("input_hash"), value.get("sequence_sha256")}:
            continue
        if requirement.input_bindings and requirement.source not in {"entry_contract", "delivery_contract"}:
            if package.get("input_hash") != fingerprint(requirement.input_bindings):
                continue
        data_kind = package.get("data_kind") or value.get("data_kind") or evidence.kind
        if requirement.data_kinds and data_kind not in requirement.data_kinds:
            continue
        if not all(_predicate(value, p) for p in requirement.predicates):
            continue
        if dependencies:
            consumed = ancestors(evidence)
            if any(len(consumed & ids) < count for ids, count in dependencies):
                continue
        matches.append(evidence)
    return matches


def structural_assessment(state, requirement, *, check_files=True):
    requirement = _authoritative_requirement(state, requirement)
    import json
    from pathlib import Path
    from bioma.core.deliverables import delivery_status, task_contracts
    evidence = matching_evidence(state, requirement)
    ids = {e.id for e in evidence}
    artifacts = []
    invalid = False
    for artifact in state.artifacts:
        if not ids.intersection(artifact.evidence_ids):
            continue
        if requirement.artifact_kinds and artifact.kind not in requirement.artifact_kinds:
            continue
        try:
            expected = artifact.metadata.get("sha256")
            if not expected or (check_files and file_digest(artifact.uri) != expected):
                invalid = True
                continue
        except (OSError, ValueError):
            invalid = True
            continue
        artifacts.append(artifact)
    satisfied = len(evidence) >= requirement.min_count and set(requirement.artifact_kinds) <= {a.kind for a in artifacts}
    satisfied = satisfied and set(requirement.artifact_names) <= {Path(a.uri).name for a in artifacts}
    failures = []
    for predicate in requirement.artifact_predicates:
        if not check_files:
            continue
        found = False
        for artifact in artifacts:
            path = Path(artifact.uri)
            if path.name != predicate.artifact_name or path.stat().st_size > 2_000_000:
                continue
            try:
                found = found or _predicate(json.loads(path.read_text(encoding='utf-8-sig')), predicate)
            except (ValueError, OSError):
                pass
        satisfied = satisfied and found
        if not found:
            failures.append(f'{predicate.artifact_name}:{predicate.field} {predicate.operator} {predicate.value!r}')
    if requirement.mode == "delivery":
        rows = [c for c in task_contracts(state) if c["id"] == requirement.delivery_id]
        if check_files:
            satisfied = satisfied and bool(rows) and delivery_status(rows, state)[0]["status"] == "verified"
        else:
            satisfied = bool(rows) and satisfied and any(requirement.delivery_id in
                (e.value.get('delivery_ids') or []) for e in evidence)
    return RequirementAssessment(requirement_id=requirement.id,
        status="satisfied" if satisfied and not invalid else "invalid" if invalid else "missing",
        rationale="Required evidence, input identity, predicates and artifacts were checked." if satisfied and not invalid
                  else "Unsatisfied artifact predicates: " + '; '.join(failures) if failures
                  else "Required applicable evidence or intact artifacts are missing.",
        limitations=failures,
        evidence_ids=[e.id for e in evidence], artifact_ids=[a.id for a in artifacts])
