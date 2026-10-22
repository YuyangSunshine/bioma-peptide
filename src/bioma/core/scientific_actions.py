"""Method V2 adapters from existing providers to concrete ScientificActions.

The adapters are read-only views over advertised capability/method contracts.
They do not route, authorize, execute, validate K_s/K_p, or commit state.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from bioma.core.methods import ExecutionMethod
from bioma.core.models import CapabilitySpec, StrictModel, TaskRequest


class ScientificOperator(str, Enum):
    INFERENCE_OPERATOR = "INFERENCE_OPERATOR"
    EXPERIMENT_OPERATOR = "EXPERIMENT_OPERATOR"
    EVIDENCE_OPERATOR = "EVIDENCE_OPERATOR"


class EffectClass(str, Enum):
    PURE = "PURE"
    COMPUTATIONAL = "COMPUTATIONAL"
    PHYSICAL = "PHYSICAL"
    EPISTEMIC = "EPISTEMIC"


class PayloadType(str, Enum):
    CODE = "CODE"
    TOOL_CALL = "TOOL_CALL"
    PROTOCOL = "PROTOCOL"
    DEVICE_GRAPH = "DEVICE_GRAPH"
    EVIDENCE_QUERY = "EVIDENCE_QUERY"


class ActionTarget(StrictModel):
    ref_type: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    version: str | None = None
    content_hash: str | None = None


class ActionCondition(StrictModel):
    condition_id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    required_state: Any = None


class ExpectedOutput(StrictModel):
    output_id: str = Field(min_length=1)
    output_type: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ActionObligation(StrictModel):
    obligation_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    satisfaction_predicate_id: str = Field(min_length=1)
    required: bool = True


class CostEstimate(StrictModel):
    value: float = Field(ge=0)
    unit: str = Field(min_length=1)
    basis: str = Field(min_length=1)


class LatencyEstimate(StrictModel):
    value: float = Field(ge=0)
    unit: Literal["MILLISECONDS", "SECONDS", "MINUTES", "HOURS", "DAYS"] = "SECONDS"
    basis: str = Field(min_length=1)


class ActionProvenance(StrictModel):
    producer_id: str = Field(min_length=1)
    implementation: str = Field(min_length=1)
    created_at: datetime
    source_refs: list[ActionTarget] = Field(min_length=1)
    derivation: str = Field(min_length=1)
    contract_version: Literal["scientific-action-v2"] = "scientific-action-v2"


_EVALUATOR_ONLY_KEYS = frozenset(
    {
        "oracle",
        "oracle_truth",
        "evaluator_truth",
        "ground_truth",
        "hidden_simulator_state",
        "admissible_actions",
        "forbidden_actions",
        "valid_terminal_states",
        "invalid_terminal_states",
        "expected_epistemic_status",
    }
)


def _find_evaluator_key(value: Any, path: str = "parameters") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _EVALUATOR_ONLY_KEYS:
                return f"{path}.{key}"
            found = _find_evaluator_key(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_evaluator_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


class ScientificAction(StrictModel):
    """A proposed action. Its presence conveys no authorization or commitment."""

    action_id: str = Field(pattern=r"^action\.[0-9a-f]{32}$")
    producer_operator: ScientificOperator
    action_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    capability_id: str = Field(min_length=1)
    target: ActionTarget
    preconditions: list[ActionCondition] = Field(default_factory=list)
    expected_outputs: list[ExpectedOutput] = Field(min_length=1)
    artifact_obligations: list[ActionObligation] = Field(default_factory=list)
    evidence_obligations: list[ActionObligation] = Field(default_factory=list)
    effect_class: EffectClass
    payload_type: PayloadType
    payload_ref: ActionTarget
    parameters: dict[str, Any]
    estimated_cost: CostEstimate
    estimated_latency: LatencyEstimate
    provenance: ActionProvenance

    @field_validator("parameters")
    @classmethod
    def no_evaluator_truth(cls, value: dict[str, Any]) -> dict[str, Any]:
        found = _find_evaluator_key(value)
        if found:
            raise ValueError(
                f"evaluator-only truth is forbidden in ScientificAction: {found}"
            )
        return value

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_contract_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_contract_dict(self) -> dict[str, Any]:
        """Return the JSON-Schema representation, omitting absent optionals."""
        return self.model_dump(mode="json", exclude_none=True)


class ScientificActionSet(StrictModel):
    """All currently available concrete candidates for supplied demands."""

    schema_version: Literal["scientific-action-set-v2"] = "scientific-action-set-v2"
    actions: list[ScientificAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_and_ordered(self):
        ids = [action.action_id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("ScientificActionSet contains duplicate action IDs")
        if ids != sorted(ids):
            raise ValueError("ScientificActionSet actions must be sorted by stable action_id")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


_OPERATOR_BY_AGENT = {
    "predagent": ScientificOperator.INFERENCE_OPERATOR,
    "proagent": ScientificOperator.EXPERIMENT_OPERATOR,
    "operagent": ScientificOperator.EXPERIMENT_OPERATOR,
    "comagent": ScientificOperator.EVIDENCE_OPERATOR,
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _stable_id(material: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    return "action." + digest[:32]


def _constant_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _payload_type(
    agent_id: str, method: ExecutionMethod | None, request: TaskRequest
) -> PayloadType:
    if method and method.id == "continuous_scientific_investigation":
        return PayloadType.CODE
    if agent_id == "proagent":
        return PayloadType.PROTOCOL
    if agent_id == "operagent":
        return PayloadType.DEVICE_GRAPH
    if agent_id == "comagent":
        return (
            PayloadType.CODE
            if method and method.id == "scientific_data_analysis"
            else PayloadType.EVIDENCE_QUERY
        )
    if method and method.id == "candidate_substitution_design":
        return PayloadType.CODE
    if method and method.id in {
        "computational_hypothesis",
        "evidence_guided_revision",
    }:
        return PayloadType.EVIDENCE_QUERY
    return PayloadType.TOOL_CALL


def _effect_class(
    agent_id: str,
    method: ExecutionMethod | None,
    capability: CapabilitySpec,
    request: TaskRequest,
) -> EffectClass:
    if agent_id == "operagent":
        return (
            EffectClass.COMPUTATIONAL
            if request.message_type == "protocol.compile.requested"
            else EffectClass.PHYSICAL
        )
    if (
        agent_id == "comagent"
        and capability.id == "scientific.evidence_review"
    ):
        return EffectClass.EPISTEMIC
    if method and method.id == "computational_hypothesis":
        return EffectClass.PURE
    return EffectClass.COMPUTATIONAL


def _obligations(values: Iterable[str], prefix: str) -> list[ActionObligation]:
    return [
        ActionObligation(
            obligation_id=f"{prefix}.{_constant_name(value).lower()}",
            description=f"Produce and bind required {prefix} {value!r}.",
            satisfaction_predicate_id=f"{prefix}.{_constant_name(value).lower()}.satisfied",
        )
        for value in sorted(set(values))
    ]


class ScientificActionAdapter:
    """Adapt typed demands without routing, provider qualification, or execution."""

    def __init__(self, agent: Any):
        peer_ids = tuple(getattr(agent, "peer_ids", ()))
        if len(peer_ids) != 1 or peer_ids[0] not in _OPERATOR_BY_AGENT:
            raise ValueError(
                "A Method V2 adapter requires exactly one supported implementation role"
            )
        self.agent = agent
        self.agent_id = peer_ids[0]
        self.operator = _OPERATOR_BY_AGENT[self.agent_id]

    def candidates(self, request: TaskRequest) -> tuple[ScientificAction, ...]:
        candidates: list[ScientificAction] = []
        for capability in self.agent.capabilities:
            identities = {capability.id, capability.family}
            if not capability.enabled or request.required_capability not in identities:
                continue
            declared = [
                method
                for method in capability.methods
                if method.message_type == request.message_type
            ]
            requested_method = request.payload.get("method_id")
            if requested_method:
                declared = [
                    method for method in declared if method.id == requested_method
                ]
                if not declared:
                    raise ValueError(
                        "TaskRequest references no declared method for its capability"
                    )
            method = declared[0] if len(declared) == 1 else None
            candidates.append(self._adapt(capability, method, request))
        return tuple(sorted(candidates, key=lambda action: action.action_id))

    def _adapt(
        self, capability: CapabilitySpec, method: ExecutionMethod | None,
        request: TaskRequest,
    ) -> ScientificAction:
        method_id = method.id if method else request.payload.get("method_id")
        parameters = {
            "task_request_id": request.id,
            "objective": request.objective,
            "message_type": request.message_type,
            "method_id": method_id,
            "payload": request.payload,
        }
        found = _find_evaluator_key(parameters)
        if found:
            raise ValueError(
                f"evaluator-only truth is forbidden in candidate generation: {found}"
            )
        identity = {
            "producer_operator": self.operator.value,
            "capability_id": capability.id,
            "method_id": method_id,
            "target_request_id": request.id,
            "parameters": parameters,
        }
        action_id = _stable_id(identity)
        preconditions = [
            ActionCondition(
                condition_id=predicate.id,
                predicate=f"{predicate.source}.{predicate.path} {predicate.operator}",
                required_state={
                    "operator": predicate.operator,
                    "value": predicate.value,
                },
            )
            for predicate in (
                method.action_contract.preconditions if method else []
            )
        ]
        outputs = [
            ExpectedOutput(
                output_id=f"evidence.{_constant_name(value).lower()}",
                output_type="EVIDENCE",
                description=f"Produce evidence kind {value!r} under the registered result contract.",
            )
            for value in sorted(
                set(method.result.evidence_kinds)
                if method
                else {"capability_result"}
            )
        ]
        outputs.extend(
            ExpectedOutput(
                output_id=f"artifact.{_constant_name(value).lower()}",
                output_type="ARTIFACT",
                description=f"Produce artifact kind {value!r} under the registered result contract.",
            )
            for value in sorted(
                set(method.result.artifact_kinds) if method else set()
            )
        )
        artifact_kinds = method.result.artifact_kinds if method else []
        verification = (
            method.result.verification_obligations
            if method
            else request.evidence_requirements
        )
        source_refs = [
            ActionTarget(ref_type="TASK_REQUEST", ref_id=request.id),
            ActionTarget(ref_type="CAPABILITY", ref_id=capability.id),
        ]
        if method:
            source_refs.append(
                ActionTarget(ref_type="EXECUTION_METHOD", ref_id=method.id)
            )
        return ScientificAction(
            action_id=action_id,
            producer_operator=self.operator,
            action_type=(
                _constant_name(method.id)
                if method
                else "RUN_" + _constant_name(capability.id)
            ),
            capability_id=capability.id,
            target=ActionTarget(ref_type="TASK_REQUEST", ref_id=request.id),
            preconditions=preconditions,
            expected_outputs=outputs,
            artifact_obligations=_obligations(artifact_kinds, "artifact"),
            evidence_obligations=_obligations(verification, "evidence"),
            effect_class=_effect_class(
                self.agent_id, method, capability, request
            ),
            payload_type=_payload_type(self.agent_id, method, request),
            payload_ref=ActionTarget(
                ref_type="TASK_REQUEST_PAYLOAD",
                ref_id=request.id,
                content_hash="sha256:"
                + hashlib.sha256(
                    _canonical(request.payload).encode("utf-8")
                ).hexdigest(),
            ),
            parameters=parameters,
            estimated_cost=CostEstimate(
                value=capability.estimated_cost,
                unit="CAPABILITY_COST_UNIT",
                basis="advertised capability estimate",
            ),
            estimated_latency=LatencyEstimate(
                value=capability.estimated_latency_seconds,
                basis="advertised capability estimate",
            ),
            provenance=ActionProvenance(
                producer_id=request.requester,
                implementation="TaskRequestScientificActionAdapter",
                created_at=request.created_at,
                source_refs=source_refs,
                derivation="typed TaskRequest adapted before provider qualification",
            ),
        )


def build_scientific_action_set(
    agents: Iterable[Any], requests: Iterable[TaskRequest]
) -> ScientificActionSet:
    """Return the union of role-local candidates for all active implementations."""

    active_agents = tuple(agents)
    active_requests = tuple(requests)
    actions = [
        action
        for agent in active_agents
        for request in active_requests
        for action in ScientificActionAdapter(agent).candidates(request)
    ]
    unique = {action.action_id: action for action in actions}
    return ScientificActionSet(
        actions=sorted(unique.values(), key=lambda action: action.action_id)
    )
