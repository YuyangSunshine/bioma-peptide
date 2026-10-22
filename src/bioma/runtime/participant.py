from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from bioma.core.models import (
    AgentMessage,
    CapabilitySpec,
    ScientificState,
    StateDelta,
    TaskRequest,
)


class RuntimeParticipant(ABC):
    """Abstract Runtime transport participant; production uses ScientificBackend."""

    def __init__(
        self,
        peer_ids: Iterable[str],
    ) -> None:
        self.peer_ids = tuple(peer_ids)
        if not self.peer_ids or len(set(self.peer_ids)) != len(self.peer_ids):
            raise ValueError("Agent peer ids must be non-empty and unique.")
        self.capabilities: list[CapabilitySpec] = []

    def advertise(self, *capabilities: CapabilitySpec) -> None:
        from bioma.capabilities.registry import advertise_capabilities
        for capability in capabilities:
            advertise_capabilities(self, capability)

    def assess(self, request, capability):
        from bioma.core.methods import assess_capability
        return assess_capability(self, request, capability)

    def request_capability(
        self,
        source: AgentMessage,
        *,
        sender: str,
        required_capability: str,
        objective: str,
        message_type: str,
        payload: dict | None = None,
        **kwargs,
    ) -> TaskRequest:
        return TaskRequest(
            requester=sender,
            required_capability=required_capability,
            objective=objective,
            message_type=message_type,
            payload=payload or {},
            correlation_id=source.correlation_id or source.task_request_id or source.id,
            causation_id=source.id,
            **kwargs,
        )

    from bioma.core.transitions import propose_transition as default_proposal

    from bioma.core.transitions import propose_transition as propose

    @abstractmethod
    def handle(self, state: ScientificState, message: AgentMessage) -> StateDelta:
        """Handle one peer message against a read-only state snapshot."""
        raise NotImplementedError
