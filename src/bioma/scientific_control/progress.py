"""Pure ResearchContract progress over committed WP2/WP3 state."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from bioma.core.research_contract import ObligationRole, ResearchContract
from .relations import FrozenModel, Reference


class ObligationProgress(FrozenModel):
    obligation_ref: Reference
    role: ObligationRole
    mandatory: bool
    terminal: bool
    status: Literal["RESOLVED", "SATISFIED", "UNRESOLVED", "INSUFFICIENT", "CONFLICTED", "BLOCKED"]
    progress_credit: bool
    acceptance_status: str
    scientific_outcome: str | None = None
    evidence_refs: tuple[Reference, ...] = ()
    artifact_refs: tuple[Reference, ...] = ()
    rationale: str


class ContractProgress(FrozenModel):
    contract_ref: Reference
    contract_version: int = Field(ge=1)
    terminal_mandatory_total: int = Field(ge=0)
    terminal_mandatory_credited: int = Field(ge=0)
    value: float = Field(ge=0, le=1)
    obligations: tuple[ObligationProgress, ...]
    optional_credited: tuple[Reference, ...] = ()


def _review_rows(state):
    from bioma.core.research import completion_assessment
    assessment = completion_assessment(state)
    return {row.requirement_id: row for row in assessment.requirements} if assessment else {}


def contract_progress(contract: ResearchContract, state, *, check_files=True) -> ContractProgress:
    """Compute J_C(E); plans, execution records and forecasts are not inputs."""
    from bioma.core.evidence_acceptance import structural_assessment
    from bioma.core.research import contract_for

    bound = contract_for(state) if state.gateway.get("research_contract") else None
    if bound is not None and bound.identity != contract.identity:
        raise ValueError("committed state binds a different ResearchContract")
    review = _review_rows(state) if bound is not None else {}
    rows = []
    for obligation in contract.requirements:
        role = obligation.effective_role
        terminal = role is not ObligationRole.SUPPORTING
        scientific = None
        if obligation.mode == "claim_assessment":
            assessed = review.get(obligation.id)
            acceptance = assessed.status if assessed else "missing"
            scientific = assessed.claim_status if assessed else None
            evidence_refs = tuple(assessed.evidence_ids) if assessed else ()
            artifact_refs = tuple(assessed.artifact_ids) if assessed else ()
        else:
            assessed = structural_assessment(state, obligation, check_files=check_files)
            acceptance = assessed.status
            evidence_refs = tuple(assessed.evidence_ids)
            artifact_refs = tuple(assessed.artifact_ids)
            if role is ObligationRole.QUESTION and acceptance == "satisfied":
                outcomes = {str(item.value.get("qualified_outcome", "")).casefold()
                    for item in state.evidence if item.id in evidence_refs
                    and isinstance(item.value, dict) and item.value.get("qualified_outcome")}
                if outcomes == {"positive"}:
                    scientific = "qualified_positive"
                elif outcomes == {"negative"}:
                    scientific = "qualified_negative"
                elif outcomes:
                    scientific = "conflicted"
        accepted = acceptance == "satisfied"
        if role is ObligationRole.QUESTION:
            credit = accepted and scientific in {"supported", "contradicted",
                                                  "qualified_positive", "qualified_negative"}
            status = "RESOLVED" if credit else (
                "CONFLICTED" if scientific in {"mixed", "conflicted"} else
                "INSUFFICIENT" if scientific == "insufficient" else "UNRESOLVED")
        elif role is ObligationRole.ACHIEVEMENT:
            credit = accepted and (scientific in {None, "supported"})
            status = "SATISFIED" if credit else (
                "CONFLICTED" if scientific in {"mixed", "conflicted"} else
                "INSUFFICIENT" if scientific == "insufficient" else "UNRESOLVED")
        elif role is ObligationRole.DELIVERY:
            credit = accepted and obligation.mode == "delivery"
            status = "SATISFIED" if credit else "UNRESOLVED"
        else:
            credit = accepted
            status = "SATISFIED" if credit else "UNRESOLVED"
        rows.append(ObligationProgress(obligation_ref=obligation.id, role=role,
            mandatory=obligation.mandatory, terminal=terminal, status=status,
            progress_credit=credit, acceptance_status=acceptance,
            scientific_outcome=scientific, evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
            rationale="Progress derives only from committed accepted scientific state."))
    terminal_rows = [row for row in rows if row.mandatory and row.terminal]
    credited = sum(row.progress_credit for row in terminal_rows)
    return ContractProgress(contract_ref=contract.identity, contract_version=contract.version,
        terminal_mandatory_total=len(terminal_rows), terminal_mandatory_credited=credited,
        value=credited / len(terminal_rows) if terminal_rows else 0.0,
        obligations=tuple(sorted(rows, key=lambda row: row.obligation_ref)),
        optional_credited=tuple(sorted(row.obligation_ref for row in rows
            if not row.mandatory and row.terminal and row.progress_credit)))
