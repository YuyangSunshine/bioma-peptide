from __future__ import annotations

from .models import AgentMessage, ScientificState, StateDelta

TERMINAL_EVIDENCE = {
    "prediction_only": "prediction",
    "protocol_design": "protocol",
    "table_analysis": "analysis",
    "evidence_synthesis": "evidence_graph_synthesis",
    "closed_loop": "conclusion",
}

def _required_terminal_evidence(state: ScientificState) -> str | None:
    from bioma.core.methods import entry_result_contract
    result = entry_result_contract(state.gateway)
    # A producer's WorkResult is validated above, but is not qualified Evidence.
    # ResearchContract completion remains subject to the independent acceptance gate.
    if result.get('result_channel') == 'work_result':
        return None
    kinds = result.get('evidence_kinds', [])
    return kinds[0] if len(kinds) == 1 else TERMINAL_EVIDENCE.get(state.intent)


class ScopeViolation(ValueError):
    pass


TRANSIENT_ERROR_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "timeout",
    "timed out",
    "connection error",
    "connectionerror",
    "temporarily unavailable",
    "502",
    "503",
    "504",
)
REPAIRABLE_ERROR_MARKERS = (
    "validation error",
    "invalid json",
    "schema",
    "cannot terminate before",
)
BLOCKING_ERROR_MARKERS = (
    "missing llm api key",
    "missing executable",
    "resource unavailable",
    "permission denied",
)


def runtime_recovery_action(exc: Exception, attempt: int) -> str:
    """Return a deterministic recovery action without weakening invariants."""
    from bioma.tools.errors import ToolError
    from bioma.llm import ModelOutputError, ModelOutputBudgetError
    if isinstance(exc, ModelOutputBudgetError):
        return 'blocked'
    if isinstance(exc, ModelOutputError):
        return "repair" if attempt < 1 else "exhausted"
    if isinstance(exc, ToolError):
        if not exc.retriable:
            return "fail"
        return "retry" if attempt < 2 else "exhausted"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "retry" if attempt < 2 else "exhausted"
    message = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in message for marker in TRANSIENT_ERROR_MARKERS):
        return "retry" if attempt < 2 else "exhausted"
    if any(marker in message for marker in REPAIRABLE_ERROR_MARKERS):
        return "repair" if attempt < 1 else "exhausted"
    if any(marker in message for marker in BLOCKING_ERROR_MARKERS):
        return "blocked"
    return "fail"


class AgentHarness:
    """Hard execution boundary for one peer transition."""

    def enforce(
        self,
        state: ScientificState,
        delta: StateDelta,
        source_message: AgentMessage,
    ) -> StateDelta:
        active = set(state.active_agents)
        from bioma.core.methods import validate_method_result
        try:
            method_delta = delta
            if delta.transition:
                method_delta = StateDelta.model_validate(
                    delta.transition.get('raw_owner_delta') or delta.transition['owner_delta'])
            validate_method_result(source_message, method_delta)
        except ValueError as exc:
            raise ScopeViolation(str(exc)) from exc
        candidate_contracts = [c for c in source_message.payload.get('delivery_contracts', []) if c.get('candidate_spec')]
        if candidate_contracts and delta.outcome not in {'failed', 'blocked'}:
            from bioma.core.deliverables import delivery_status
            proposed = state.model_copy(update={'evidence': [*state.evidence, *delta.evidence],
                                                 'artifacts': [*state.artifacts, *delta.artifacts]})
            if any(c['status'] != 'verified' for c in delivery_status(candidate_contracts, proposed)):
                raise ScopeViolation('Candidate design contract validation error: invalid or missing candidate content')
        if source_message.recipient not in active:
            raise ScopeViolation(f"Agent {source_message.recipient!r} is disabled.")
        disabled_targets = {
            message.recipient
            for message in delta.messages
            if message.recipient not in active
        }
        if disabled_targets:
            raise ScopeViolation(
                f"Outgoing messages target disabled agents: {sorted(disabled_targets)}."
            )
        required_evidence = _required_terminal_evidence(state)
        from bioma.core.research import contract_for
        if contract_for(state) and delta.outcome == 'completed':
            prospective = state.model_copy(update={'evidence': [*state.evidence, *delta.evidence],
                'artifacts': [*state.artifacts, *delta.artifacts], 'constraints': [*state.constraints, *delta.constraints]})
            from bioma.core.acceptance import completion_status
            if not completion_status(prospective)['complete']:
                raise ScopeViolation('Task cannot terminate before its work, research and delivery acceptance conditions are satisfied.')
            return delta
        if delta.outcome == "failed":
            failures = {"failure", "execution_timeout", "execution_failure", "incomplete_deliverables"}
            if any(item.kind in failures for item in delta.evidence):
                return delta
        if delta.outcome in {"completed", "failed"} and required_evidence:
            evidence_kinds = {item.kind for item in [*state.evidence, *delta.evidence]}
            if required_evidence not in evidence_kinds:
                raise ScopeViolation(
                    f"Task cannot terminate before {required_evidence!r} evidence is committed."
                )
        return delta
