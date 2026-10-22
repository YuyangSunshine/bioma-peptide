"""Read-only independent assessment of versioned scientific requirements."""
from __future__ import annotations

from bioma.core.research import (ResearchAssessment, RequirementAssessment,
    contract_for, evidence_snapshot)


from bioma.core.evidence_acceptance import matching_evidence, structural_assessment


def _pending(state, requirement):
    pending = set((state.metrics.get("research_review") or {}).get("pending_request_ids", []))
    if not pending:
        return False
    from bioma.core.research_planning import execution_requirements_for
    requirement = next((r for r in execution_requirements_for(state) if r.id == requirement.id), None)
    if requirement is None:
        return False
    pending = set((state.metrics.get("research_review") or {}).get("pending_request_ids", []))
    for request in state.task_requests:
        if request.id not in pending:
            continue
        assignment = request.payload.get("research_assignment") or {}
        if requirement.id in assignment.get("requirement_ids", []):
            return True
        if (requirement.capability and request.message_type == requirement.message_type
                and (not requirement.method_id or requirement.method_id == request.payload.get('method_id'))
                and request.required_capability == requirement.capability):
            inputs = request.payload.get("inputs") or {}
            if not requirement.input_bindings or inputs == requirement.input_bindings:
                return True
    return False


def assess_contract(state, *, synthesizer):
    """Read-only professional review shared by direct methods and archive code.

    Produces an assessment proposal only: no repair dispatch, files, Evidence,
    state writes, or authority to complete a research task.
    """
    from bioma.core.acceptance import review_requirements, protocol_for, required_ids
    contract = contract_for(state)
    protocol = protocol_for(state)
    if contract is None or protocol is None or protocol.mode != 'research':
        raise ValueError('Full-contract review requires research acceptance')
    requirements = review_requirements(state)
    required = required_ids(contract)
    rows = {}
    for requirement in requirements:
        if requirement.mode == "claim_assessment":
            continue
        row = structural_assessment(state, requirement)
        if row.status != "satisfied" and _pending(state, requirement):
            row.status, row.rationale = "pending", "An existing assignment is producing the required evidence."
        rows[requirement.id] = row
    # Scientific claim assessment uses evidence, never previous review opinions
    # as independent support. Delay model calls while production is in flight.
    pending_work = bool((state.metrics.get("research_review") or {}).get("pending_request_ids"))
    claim_rows = [r for r in requirements if r.mode == "claim_assessment"]
    reviews = {}
    synthesis = None
    if not pending_work:
        for requirement in requirements:
            row = rows.get(requirement.id)
            if row and row.status == 'satisfied' and requirement.semantic_review:
                judgment = synthesizer.review_requirement(state, requirement, matching_evidence(state, requirement))
                rows[requirement.id] = row.model_copy(update=judgment)
    if claim_rows and not pending_work:
        from bioma.evidence.synthesis import substantive_evidence
        scientific = substantive_evidence(state)
        local = state.model_copy(update={"evidence": scientific,
            "gateway": {**state.gateway, "claims": contract.claims}})
        synthesis = synthesizer.synthesize_all(local)
        reviews = {r["claim_id"]: r for r in synthesis.claim_assessments}
    for requirement in claim_rows:
        selected = [reviews[c] for c in requirement.claim_ids if c in reviews]
        if len(selected) != len(requirement.claim_ids):
            rows[requirement.id] = RequirementAssessment(requirement_id=requirement.id,
                status="pending" if pending_work else "insufficient",
                rationale="Every contracted claim requires an explicit assessment.")
            continue
        adequate = all(r["claim_status"] != "insufficient" or requirement.allow_insufficient for r in selected)
        cited = list(dict.fromkeys(a["evidence_id"] for r in selected for a in r["assessments"]))
        # A positive/negative scientific judgement must cite scientific evidence.
        grounded = all(r["claim_status"] == "insufficient" or any(a["relation"] in {"supports", "contradicts"} for a in r["assessments"]) for r in selected)
        rows[requirement.id] = RequirementAssessment(requirement_id=requirement.id,
            status="satisfied" if adequate and grounded else "insufficient",
            rationale="\n".join(r["summary"] for r in selected), evidence_ids=cited,
            review_method=synthesis.method,
            claim_status=selected[0]["claim_status"] if len({r["claim_status"] for r in selected}) == 1 else "mixed")
    # Dependency obligations must hold even when a matching downstream file exists.
    for _ in requirements:
        changed = False
        for r in requirements:
            if rows[r.id].status == "satisfied" and any(rows[d].status != "satisfied" for d in r.depends_on):
                rows[r.id].status, rows[r.id].rationale = "missing", "A required prerequisite remains unsatisfied."
                changed = True
        if not changed:
            break
    assessment = ResearchAssessment(contract_id=contract.identity, contract_version=contract.version,
        snapshot_hash=evidence_snapshot(state), requirements=[rows[r.id] for r in requirements],
        complete=all(rows[r.id].status == 'satisfied' for r in requirements if r.id in required),
        claim_synthesis=synthesis.model_dump(mode='json') if synthesis else None)
    return assessment
