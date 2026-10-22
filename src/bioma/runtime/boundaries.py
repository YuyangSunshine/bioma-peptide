"""Deterministic authority boundaries applied after provider execution."""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from bioma.core.action_contracts import Predicate
from bioma.core.models import Observation


def has_work_authority(state, proposal) -> bool:
    return (state.gateway.get("intake_version") == "research-contract-intake-v1"
            and proposal.capability_id != "research.problem_formulation")


def specialize_work_contract(state, proposal):
    """Make a WORK action promise an operational result, preserving all guards."""
    if not has_work_authority(state, proposal):
        return proposal
    contract = proposal.contract.model_copy(deep=True)
    contract.writable_fields = sorted(set(contract.writable_fields) | {"observations"})
    contract.expected_effects = [
        Predicate(id="work_result_observation_returned", source="result",
                  path="updates.observations", operator="nonempty")
        if row.path == "evidence" and row.operator == "nonempty" else row
        for row in contract.expected_effects
    ]
    return proposal.model_copy(update={"contract": contract,
        "write_set": sorted(set(proposal.write_set) | {"observations"})})


def normalize_work_result(state, proposal, delta):
    """Remove provider result-shaped evidence from shared scientific authority."""
    if not has_work_authority(state, proposal) or not delta.evidence:
        return delta
    candidate_ids = [item.id for item in delta.evidence]
    artifacts = [item.model_copy(deep=True) for item in delta.artifacts]
    artifact_ids = [item.id for item in artifacts]
    candidates = set(candidate_ids)
    for artifact in artifacts:
        bound = [ref for ref in artifact.evidence_ids if ref in candidates]
        if bound:
            artifact.metadata = {**artifact.metadata, "candidate_result_refs": bound}
            artifact.evidence_ids = [ref for ref in artifact.evidence_ids if ref not in candidates]
    observation = Observation(
        id=str(uuid5(NAMESPACE_URL, proposal.identity + ':work-result:' + ':'.join(candidate_ids))),
        kind="work_result", source=delta.agent_id, method=proposal.method_id,
        value={"candidate_results": [item.model_dump(mode="json") for item in delta.evidence],
               "outcome": delta.outcome}, artifact_ids=artifact_ids,
        created_at=delta.evidence[0].created_at)
    updates = dict(delta.updates)
    updates["observations"] = [*updates.get("observations", []),
                               observation.model_dump(mode="json")]
    return delta.model_copy(update={"evidence": [], "artifacts": artifacts,
                                    "updates": updates})
