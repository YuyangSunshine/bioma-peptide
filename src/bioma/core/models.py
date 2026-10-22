from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from bioma.core.methods import ExecutionMethod


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScientificTask(StrictModel):
    """A scientific task with an explicit entry peer and run scope."""

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    objective: str = Field(min_length=1)
    initial_agent: str = Field(default="queryagent", min_length=1)
    initial_capability: str | None = None
    initial_method_id: str | None = None
    initial_message_type: str | None = None
    active_agents: list[str] = Field(default_factory=list)
    intent: str = "explicit"
    scope_reason: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    max_cycles: int = Field(default=3, ge=1)
    gateway: dict[str, Any] = Field(default_factory=dict)
    external_work_results: list["ExternalWorkResultReference"] = Field(default_factory=list)


class ExternalWorkResultReference(StrictModel):
    """Immutable pointer to a governed WorkResult from another BioMA run."""

    source_ledger: str = Field(min_length=1)
    source_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_task_id: str = Field(min_length=1)
    work_result_id: str = Field(min_length=1)
    execution_receipt_ref: str = Field(min_length=1)
    remote_job_id: str | None = Field(default=None, min_length=1)
    model_name: str | None = Field(default=None, min_length=1)
    model_version: str | None = Field(default=None, min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_uri: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_uri: str = Field(min_length=1)
    validation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_scope: Literal["local_read"] = "local_read"
    qualification_status: Literal["independently_validated"] = "independently_validated"


class CapabilitySpec(StrictModel):
    id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    description: str = ""
    family: str = ""
    methods: list["ExecutionMethod"] = Field(default_factory=list)
    execution_profiles: list[dict[str, Any]] = Field(default_factory=list)
    accepts_message_types: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    preconditions: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    tool_hints: list[str] = Field(default_factory=list)
    skill_hints: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    verification_strategy: str = "transition_integrity"
    max_risk_level: Literal["low", "medium", "high"] = "low"
    estimated_cost: float = Field(default=0.0, ge=0.0)
    estimated_latency_seconds: float = Field(default=0.0, ge=0.0)
    reliability: float = Field(default=1.0, ge=0.0, le=1.0)
    priority: int = 0
    enabled: bool = True
    action_modes: list[Literal["llm", "tool", "code", "protocol", "verification"]] = Field(default_factory=list)
    runtime_profile: dict[str, Any] = Field(default_factory=dict)


class TaskRequest(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    requester: str = Field(min_length=1)
    required_capability: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    message_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_requirements: list[str] = Field(default_factory=list)
    budget: float | None = Field(default=None, ge=0.0)
    deadline: datetime | None = None
    risk_level: Literal["low", "medium", "high"] = "low"
    selection_mode: Literal["single", "all", "k_of_n"] = "single"
    max_providers: int = Field(default=1, ge=1)
    required: bool = True
    correlation_id: str | None = None
    causation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class TaskOffer(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_request_id: str
    agent_id: str
    capability_id: str
    method_id: str | None = None
    score: float
    reason: str
    estimated_cost: float = 0.0
    estimated_latency_seconds: float = 0.0
    reliability: float = 1.0
    created_at: datetime = Field(default_factory=utc_now)


class Commitment(StrictModel):
    """Layer A: the requester accepts one offer and assumes the task obligation.

    A Commitment is a *responsibility* relation, never a state commit. The
    paper-level links below are optional so existing journals stay readable;
    when present they bind the accepted offer and its state version.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_request_id: str
    agent_id: str
    capability_id: str
    method_id: str | None = None
    lease_expires_at: datetime | None = None
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    offer_id: str | None = None
    accepted_obligations: list[str] = Field(default_factory=list)
    resource_budget: float | None = Field(default=None, ge=0.0)
    parent_state_version: int | None = Field(default=None, ge=0)


class Evidence(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    value: Any = None
    source: str
    method: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    parent_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ControlRecord(StrictModel):
    """Auditable control-plane provenance; never scientific evidence."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["query_spec"]
    subject_hash: str = Field(min_length=1)
    source: str
    created_at: datetime = Field(default_factory=utc_now)


class Constraint(StrictModel):
    id: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    scope: Literal[
        "biological", "device", "temporal", "resource", "safety", "general"
    ] = "general"
    severity: Literal["warning", "error", "fatal"] = "error"
    satisfied: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AgentMessage(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    sender: str
    recipient: str = Field(min_length=1)
    message_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    task_request_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str | None = None
    ttl: int = Field(default=8, ge=1)
    created_at: datetime = Field(default_factory=utc_now)

    def handoff(
        self,
        *,
        sender: str,
        recipient: str,
        message_type: str,
        payload: dict[str, Any] | None = None,
    ) -> "AgentMessage":
        if self.ttl == 1:
            raise ValueError("Message TTL exhausted before peer handoff.")
        return AgentMessage(
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            payload=payload or {},
            task_request_id=self.task_request_id,
            correlation_id=self.correlation_id,
            causation_id=self.id,
            idempotency_key=self.idempotency_key,
            ttl=self.ttl - 1,
        )


class Artifact(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class Observation(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    kind: str = Field(min_length=1)
    value: Any
    method: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ProvenanceRecord(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    source_message_id: str
    version_before: int
    version_after: int
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class StateDelta(StrictModel):
    """The only write contract available to an agent."""

    agent_id: str
    source_message_id: str
    expected_version: int = Field(ge=0)
    updates: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    control_records: list[ControlRecord] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    task_requests: list[TaskRequest] = Field(default_factory=list)
    offers: list[TaskOffer] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    outcome: Literal["continue", "completed", "blocked", "failed"] = "continue"
    status_reason: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "verified", "rejected"] = "proposed"
    transition: dict[str, Any] | None = None


class ScientificState(StrictModel):
    """Committed scientific state shared by all agents."""

    task_id: str
    objective: str
    active_agents: list[str] = Field(default_factory=list)
    intent: str = "explicit"
    scope_reason: str = ""
    initial_capability: str | None = None
    initial_method_id: str | None = None
    initial_message_type: str | None = None
    gateway: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    max_cycles: int = Field(default=3, ge=1)
    cycle: int = Field(default=0, ge=0)
    version: int = Field(default=0, ge=0)
    status: Literal["running", "completed", "blocked", "failed"] = "running"
    status_reason: dict[str, Any] = Field(default_factory=dict)
    predictions: list[dict[str, Any]] = Field(default_factory=list)
    protocol: dict[str, Any] = Field(default_factory=dict)
    machine_code: dict[str, Any] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    control_records: list[ControlRecord] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    agent_status: dict[str, str] = Field(default_factory=dict)
    provenance: list[ProvenanceRecord] = Field(default_factory=list)
    task_requests: list[TaskRequest] = Field(default_factory=list)
    offers: list[TaskOffer] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)

    UPDATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "cycle",
            "predictions",
            "protocol",
            "machine_code",
            "observations",
            "metrics",
            "agent_status",
        }
    )
    APPEND_FIELDS: ClassVar[frozenset[str]] = frozenset({"predictions", "observations"})
    MERGE_FIELDS: ClassVar[frozenset[str]] = frozenset({"metrics", "agent_status"})

    @property
    def outcome(self) -> dict:
        """Separate execution, scientific support and delivery; never commit state."""
        from bioma.core.acceptance import outcome_summary
        return outcome_summary(self)

    @property
    def acceptance(self) -> dict:
        """Current scientific validity, including files; not a persisted authority.

        metrics['acceptance'] is the historical evaluation at a transition.
        Reading current validity must not rewrite that transition or its ledger.
        """
        from bioma.core.acceptance import completion_status
        return completion_status(self)

    @classmethod
    def from_task(cls, task: ScientificTask) -> "ScientificState":
        return cls(
            task_id=task.id,
            objective=task.objective,
            active_agents=task.active_agents,
            intent=task.intent,
            scope_reason=task.scope_reason,
            initial_capability=task.initial_capability,
            initial_method_id=task.initial_method_id,
            initial_message_type=task.initial_message_type,
            gateway=task.gateway,
            inputs=task.inputs,
            success_criteria=task.success_criteria,
            max_cycles=task.max_cycles,
        )

    def commit(self, delta: StateDelta) -> "ScientificState":
        """Return a new state after a verified, version-matched transition."""
        if delta.status != "verified":
            raise ValueError("Only a verified StateDelta can be committed.")
        if delta.expected_version != self.version:
            raise ValueError(
                f"Stale StateDelta: expected version {delta.expected_version}, "
                f"current version is {self.version}."
            )

        next_state = self.model_copy(deep=True)
        for key, value in delta.updates.items():
            if key not in self.UPDATABLE_FIELDS:
                raise KeyError(f"State field is not agent-updatable: {key}")
            current = getattr(next_state, key)
            if key in self.APPEND_FIELDS:
                if key == 'observations':
                    value = [Observation.model_validate(item) for item in value]
                current.extend(value)
            elif key in self.MERGE_FIELDS:
                current.update(value)
            else:
                setattr(next_state, key, value)

        next_state.evidence.extend(delta.evidence)
        next_state.control_records.extend(delta.control_records)
        next_state.artifacts.extend(delta.artifacts)
        next_state.constraints.extend(delta.constraints)
        next_state.task_requests.extend(delta.task_requests)
        next_state.offers.extend(delta.offers)
        next_state.commitments.extend(delta.commitments)
        next_state.agent_status[delta.agent_id] = delta.outcome
        next_state.status = {
            "continue": "running",
            "completed": "completed",
            "blocked": "blocked",
            "failed": "failed",
        }[delta.outcome]
        next_state.status_reason = dict(delta.status_reason)
        next_state.version += 1
        next_state.provenance.append(
            ProvenanceRecord(
                agent_id=delta.agent_id,
                source_message_id=delta.source_message_id,
                version_before=self.version,
                version_after=next_state.version,
                evidence_ids=[item.id for item in delta.evidence],
                artifact_ids=[item.id for item in delta.artifacts],
            )
        )
        return ScientificState.model_validate(next_state.model_dump())
