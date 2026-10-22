from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect

from bioma.core.models import AgentMessage, Observation, ScientificState, StateDelta
from pydantic import ValidationError
from bioma.core.receipts import VerificationReceipt


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reasons: tuple[str, ...] = ()


class VerificationError(ValueError):
    pass


class VerificationEngine:
    """Validate transition integrity before the shared state is committed."""

    def __init__(self, observation_trust=None, *, historical=False):
        from bioma.runtime.security import TrustStore
        self.observation_trust = observation_trust or TrustStore()
        self.historical = historical

    def verify(
        self,
        state: ScientificState,
        delta: StateDelta,
        *,
        expected_agent: str,
        source_message: AgentMessage,
    ) -> VerificationResult:
        reasons: list[str] = []
        from bioma.core.transitions import verify_transition_proof
        try:
            verify_transition_proof(state, delta, source_message)
        except (ValueError, KeyError, TypeError) as exc:
            reasons.append('Transition admission proof rejected: ' + str(exc))
        from bioma.core.methods import validate_method_result
        try:
            method_delta = delta
            if delta.transition:
                method_delta = StateDelta.model_validate(
                    delta.transition.get('raw_owner_delta') or delta.transition['owner_delta'])
            validate_method_result(source_message, method_delta)
        except ValueError as exc:
            reasons.append(str(exc))
        if delta.agent_id != expected_agent:
            reasons.append(
                f"Delta agent {delta.agent_id!r} does not match recipient "
                f"{expected_agent!r}."
            )
        if delta.source_message_id != source_message.id:
            reasons.append("Delta source_message_id does not match the delivered message.")
        if delta.expected_version != state.version:
            reasons.append(
                f"Delta expects state version {delta.expected_version}, "
                f"but current version is {state.version}."
            )

        unknown_updates = set(delta.updates) - ScientificState.UPDATABLE_FIELDS
        if unknown_updates:
            reasons.append(f"Unknown state updates: {sorted(unknown_updates)}.")

        known_evidence = {item.id for item in state.evidence}
        available_evidence = set(known_evidence)
        for item in delta.evidence:
            if item.id in available_evidence:
                reasons.append(f"Duplicate evidence id: {item.id!r}.")
            if item.source != delta.agent_id:
                reasons.append(
                    f"Evidence {item.id!r} has source {item.source!r}; "
                    f"expected {delta.agent_id!r}."
                )
            unknown = set(item.parent_ids) - available_evidence
            if unknown:
                reasons.append(
                    f"Evidence {item.id!r} has unknown parents: {sorted(unknown)}."
                )
            available_evidence.add(item.id)

        # WP2: reserved-role guards over the existing evidence channel. This
        # contributes no reason unless the delta introduces a reserved kind, so
        # legacy deltas keep exactly the verification reasons they had before.
        from bioma.core.scientific_views import reserved_role_reasons
        reasons.extend(reserved_role_reasons(delta, committed=state.evidence))

        known_artifacts = {item.id for item in state.artifacts}
        new_artifacts: set[str] = set()
        for artifact in delta.artifacts:
            if artifact.id in known_artifacts or artifact.id in new_artifacts:
                reasons.append(f"Duplicate artifact id: {artifact.id!r}.")
            if artifact.source != delta.agent_id:
                reasons.append(
                    f"Artifact {artifact.id!r} has source {artifact.source!r}; "
                    f"expected {delta.agent_id!r}."
                )
            unknown = set(artifact.evidence_ids) - available_evidence
            if unknown:
                reasons.append(
                    f"Artifact {artifact.id!r} references unknown evidence: "
                    f"{sorted(unknown)}."
                )
            new_artifacts.add(artifact.id)

        available_artifacts = known_artifacts | new_artifacts
        for item in delta.evidence:
            unknown = set(item.artifact_ids) - available_artifacts
            if unknown:
                reasons.append(
                    f"Evidence {item.id!r} references unknown artifacts: {sorted(unknown)}."
                )

        known_observations = {item.id for item in state.observations}
        observation_values = delta.updates.get("observations", [])
        if not isinstance(observation_values, list):
            reasons.append("State update 'observations' must be a list.")
            observation_values = []
        for value in observation_values:
            try:
                observation = Observation.model_validate(value)
            except ValidationError as exc:
                reasons.append(f"Invalid observation update: {exc}.")
                continue
            if observation.id in known_observations:
                reasons.append(f"Duplicate observation id: {observation.id!r}.")
            if observation.source != delta.agent_id:
                reasons.append(
                    f"Observation {observation.id!r} has source {observation.source!r}; "
                    f"expected {delta.agent_id!r}."
                )
            unknown = set(observation.artifact_ids) - available_artifacts
            if unknown:
                reasons.append(
                    f"Observation {observation.id!r} references unknown artifacts: "
                    f"{sorted(unknown)}."
                )
            known_observations.add(observation.id)
            if observation.kind == 'simulated_experiment' and delta.agent_id == 'operagent':
                from bioma.core.measurements import verify_simulation
                try:
                    verify_simulation(observation, delta.updates.get('machine_code') or {}, state.task_id,
                        state.protocol.get('text', ''), source_message.task_request_id or source_message.id)
                except (ValueError, TypeError, KeyError) as exc:
                    reasons.append('Invalid simulation admission: ' + str(exc))

        # Scientific success needs an observation lineage, regardless of which
        # provider proposed it. A successful tool call is not an experiment.
        evidence_by_id = {e.id: e for e in [*state.evidence, *delta.evidence]}
        observations = [*state.observations]
        for value in observation_values:
            try:
                observations.append(Observation.model_validate(value))
            except ValidationError:
                pass
        from bioma.core.measurements import verify_observation
        empirical = set()
        for observation in observations:
            if observation.kind != 'experimental_observation':
                continue
            try:
                verify_observation(observation, [*state.artifacts, *delta.artifacts],
                    self.observation_trust, state.task_id, check_files=not self.historical)
                empirical.add(observation.id)
            except (ValueError, PermissionError, TypeError, OSError) as exc:
                reasons.append(f'Invalid measurement admission: {exc}')
        for item in delta.evidence:
            value = item.value if isinstance(item.value, dict) else {}
            if value.get('data_kind') != 'experimental_observation':
                continue
            lineage, pending = set(), [item.id]
            while pending:
                ref = pending.pop()
                if ref in lineage:
                    continue
                lineage.add(ref)
                if ref in evidence_by_id:
                    pending.extend(evidence_by_id[ref].parent_ids)
            if not any(isinstance(evidence_by_id[ref].value, dict)
                       and evidence_by_id[ref].value.get('observation_id') in empirical
                       for ref in lineage if ref in evidence_by_id):
                reasons.append('Experimental evidence requires a traceable observation and source artifact.')

        known_constraints = {item.id for item in state.constraints}

        for constraint in delta.constraints:
            if constraint.id in known_constraints:
                reasons.append(f"Duplicate constraint id: {constraint.id!r}.")
            known_constraints.add(constraint.id)
            if constraint.satisfied is False and constraint.severity in {"error", "fatal"}:
                reasons.append(f"Unsatisfied {constraint.severity} constraint: {constraint.id}.")

        outgoing_ids: set[str] = set()
        for message in delta.messages:
            if message.id in outgoing_ids or message.id == source_message.id:
                reasons.append(f"Duplicate message id: {message.id!r}.")
            outgoing_ids.add(message.id)
            if message.sender != delta.agent_id:
                reasons.append(
                    f"Outgoing message {message.id!r} has sender {message.sender!r}; "
                    f"expected {delta.agent_id!r}."
                )
            if message.ttl != source_message.ttl - 1:
                reasons.append(
                    f"Outgoing message {message.id!r} has TTL {message.ttl}; "
                    f"expected {source_message.ttl - 1}."
                )

        if delta.outcome != "continue" and delta.messages:
            reasons.append("A terminal StateDelta cannot emit outgoing messages.")

        from bioma.core.research import validate_assessment, contract_for
        prospective = state.model_copy(update={'evidence': [*state.evidence, *delta.evidence],
            'artifacts': [*state.artifacts, *delta.artifacts], 'constraints': [*state.constraints, *delta.constraints]})
        for evidence in delta.evidence:
            if evidence.kind == 'research_assessment':
                try:
                    contract = contract_for(state)
                    if contract and contract.schema_version == 'research-contract-v2':
                        from bioma.core.research import qualified_review_assessment
                        if (delta.agent_id != 'harness'
                                or source_message.message_type != 'runtime.evidence_qualification'):
                            raise ValueError('Research review requires independent runtime qualification')
                        qualified_review_assessment(prospective, evidence, check_files=not self.historical)
                    else:
                        if delta.agent_id != 'comagent':
                            raise ValueError('Historical assessment requires its recorded owner')
                        validate_assessment(prospective, evidence, check_files=not self.historical)
                except (ValueError, TypeError) as exc:
                    reasons.append(str(exc))
        if delta.outcome == 'completed' and contract_for(state):
            from bioma.core.acceptance import completion_status
            if not completion_status(prospective, check_files=not self.historical)['complete']:
                reasons.append('Completion requires all declared acceptance conditions')
        from bioma.core.protocol_policy import active_policy
        if active_policy().evidence_gate:
            from bioma.runtime.acceptance import validate_delivery_delta
            try:
                validate_delivery_delta(state, delta, source_message, check_files=not self.historical)
            except (ValueError, TypeError) as exc:
                reasons.append(str(exc))

        return VerificationResult(ok=not reasons, reasons=tuple(reasons))

    def approve(
        self,
        state: ScientificState,
        delta: StateDelta,
        *,
        expected_agent: str,
        source_message: AgentMessage,
    ) -> StateDelta:
        result = self.verify(
            state,
            delta,
            expected_agent=expected_agent,
            source_message=source_message,
        )
        if not result.ok:
            raise VerificationError(" ".join(result.reasons))
        approved = delta.model_copy(deep=True)
        approved.status = "verified"
        return approved

    def verify_with_receipt(
        self, state: ScientificState, delta: StateDelta, *, expected_agent: str,
        source_message: AgentMessage, verifier_id: str = "harness-node",
    ) -> tuple[VerificationResult, VerificationReceipt]:
        result = self.verify(state, delta, expected_agent=expected_agent, source_message=source_message)
        inputs = {
            "state": state.model_dump(mode="json"),
            "expected_agent": expected_agent,
            "delta": delta.model_dump(mode="json"),
            "source_message": source_message.model_dump(mode="json"),
        }
        policy = {"engine": type(self).__name__, "ruleset": "transition-integrity-v1",
                  "implementation_sha256": hashlib.sha256(inspect.getsource(type(self).verify).encode()).hexdigest()}
        policy['observation_trust'] = self.observation_trust.snapshot()
        if delta.transition:
            for name in ('bioma.core.transitions', 'bioma.core.action_contracts'):
                policy[name] = hashlib.sha256(inspect.getsource(__import__(name, fromlist=['_'])).encode()).hexdigest()
        policy['method_contract_sha256'] = hashlib.sha256(inspect.getsource(__import__('bioma.core.methods', fromlist=['_'])).encode()).hexdigest()
        policy['measurement_rules_sha256'] = hashlib.sha256(inspect.getsource(__import__('bioma.core.measurements', fromlist=['verify_observation'])).encode()).hexdigest()
        if state.gateway.get('research_contract'):
            policy['research_rules_sha256'] = hashlib.sha256(inspect.getsource(__import__('bioma.core.research', fromlist=['validate_assessment'])).encode()).hexdigest()
            policy['research_acceptance_sha256'] = hashlib.sha256(inspect.getsource(__import__('bioma.backends.analysis.evidence_audit', fromlist=['structural_assessment'])).encode()).hexdigest()
        if state.gateway.get('acceptance_protocol'):
            for name in ('bioma.core.acceptance', 'bioma.core.evidence_acceptance', 'bioma.runtime.acceptance'):
                policy[name] = hashlib.sha256(inspect.getsource(__import__(name, fromlist=['_'])).encode()).hexdigest()
        receipt = VerificationReceipt.calculate(
            run_id=state.task_id, verifier_id=verifier_id, inputs=inputs,
            policy=policy, decision="approved" if result.ok else "rejected",
            reasons=result.reasons,
        )
        receipt = receipt.model_copy(update={'policy_context': policy})
        return result, receipt
