"""WP2 runtime/projection invariants: artifact identity (E7) and projection
safety (E8).

Neither property is a pre-commit guard. Artifact identity is a property of how
the runtime names scientific output, and projection safety is a property of the
read-only views the runtime hands to an agent. Both are established by the
runtime and are checked here as audits, never claimed by a verifier looking at
a single delta.

E7  artifact identity
    An artifact's identity is its content and authorship, not the transport
    path it happened to be written to. One artifact id therefore maps to one
    (kind, source, content, evidence) identity, and two deltas that differ only
    in artifact transport path are the same scientific output.

E8  projection safety
    A projection is a read-only, scoped view of committed state. Its
    *enforceable* property is exactly three things, each audited here:

      1. the projection contains no invented record
         (:func:`assert_projection_never_invents`);
      2. the projection reveals only the authorised committed-state closure
         (:func:`assert_projection_within_authorised_closure`);
      3. building the projection is read-only
         (:func:`assert_projection_is_read_only`).

    E8 does *not* claim that the runtime enforces the stronger separation
    ``B_i^t ∉ S_t`` between an agent's belief and shared state. That separation
    is *architectural*: WP2 implements no private-belief store at all, so there
    is nowhere for a belief to be committed and the property holds by
    construction rather than by a runtime check. Memory records that live in
    the separate memory subsystem are kept outside ``S_t`` by that boundary;
    record *ownership* (``record.owner == agent``) is a property of the memory
    store, not a definition of belief, and is never equated with one here.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from bioma.core.models import Artifact, Evidence, ScientificState, StateDelta
from bioma.core.protocol_invariants import ProtocolViolation
from bioma.core.transitions import execution_identity


class ArtifactIdentityViolation(ProtocolViolation):
    """E7: one artifact identity was observed carrying two different contents."""

    code, invariant = "artifact_identity", "E7"


class ProjectionSafetyViolation(ProtocolViolation):
    """E8: a projection mutated its source, invented a record, or revealed a
    record outside the authorised committed-state closure."""

    code, invariant = "projection_safety", "E8"


# --- E7: artifact identity -------------------------------------------------


def artifact_identity(artifact: Artifact) -> tuple[Any, ...]:
    """Content-based identity of one artifact: transport path excluded."""
    return (
        artifact.id,
        artifact.kind,
        artifact.source,
        tuple(sorted(artifact.evidence_ids)),
        artifact.metadata.get("sha256"),
    )


def assert_artifact_identity_stable(artifacts: Iterable[Artifact]) -> None:
    """E7: an artifact id never names two different scientific outputs."""
    seen: dict[str, tuple[Any, ...]] = {}
    for artifact in artifacts:
        identity = artifact_identity(artifact)
        existing = seen.get(artifact.id)
        if existing is not None and existing != identity:
            raise ArtifactIdentityViolation(
                f"artifact {artifact.id!r} has two identities: "
                f"{existing!r} and {identity!r}"
            )
        seen[artifact.id] = identity


def assert_identity_ignores_transport(first: StateDelta, second: StateDelta) -> None:
    """E7: two deltas differing only in artifact transport path are one output.

    ``execution_identity`` is the runtime's scientific-output identity; the
    check fails if that identity can be moved by rewriting an artifact's
    ``uri``.
    """
    if execution_identity(first) != execution_identity(second):
        raise ArtifactIdentityViolation(
            "scientific output identity depends on artifact transport path"
        )


# --- E8: projection safety -------------------------------------------------


def _record_ids(state: ScientificState) -> dict[str, set[str]]:
    return {
        "evidence": {item.id for item in state.evidence},
        "artifacts": {item.id for item in state.artifacts},
        "observations": {item.id for item in state.observations},
    }


def assert_projection_never_invents(
    source: ScientificState, projection: ScientificState
) -> None:
    """E8: a projection contains no record the source state does not contain."""
    committed = _record_ids(source)
    for field, ids in _record_ids(projection).items():
        invented = sorted(ids - committed[field])
        if invented:
            raise ProjectionSafetyViolation(
                f"projection invents {field}: {invented}"
            )


def assert_projection_is_read_only(
    state: ScientificState, project: Callable[[ScientificState], Any]
) -> Any:
    """E8: building a projection must not mutate the state it reads."""
    before = state.model_dump(mode="json")
    view = project(state)
    if state.model_dump(mode="json") != before:
        raise ProjectionSafetyViolation("projection mutated the state it read")
    return view


def assert_projection_within_authorised_closure(
    events: Iterable[Any], policy: Any, graph: dict[str, Any]
) -> None:
    """E8: a projection reveals only the authorised committed-state closure.

    ``policy`` is the same projection policy the graph was built with, and
    ``graph`` is the projected symbolic graph. Every projected node must name
    an event the policy authorises: a readable kind, or the agent's own event
    when that is allowed. Nothing outside that closure may appear.
    """
    authorised = {
        str(getattr(event, "event_id", ""))
        for event in events
        if getattr(event, "kind", None) in policy.readable_kinds
        or (
            policy.allow_own_events
            and getattr(event, "actor", None) == policy.agent_id
        )
    }
    leaked = sorted(
        str(node.get("id"))
        for node in graph.get("nodes", ())
        if str(node.get("id")) not in authorised
    )
    if leaked:
        raise ProjectionSafetyViolation(
            "projection reveals events outside the authorised closure: "
            + ", ".join(leaked)
        )


def assert_records_excluded_from_state(
    state: ScientificState, uncommitted_ids: Iterable[str]
) -> None:
    """E8: ids the caller says are not committed must not appear in ``S_t``.

    E8 does not decide what a belief is. The caller names records that belong
    to another store (for example the memory subsystem); this checks only that
    such a record never became a committed scientific-state record.
    """
    committed = _record_ids(state)
    leaked = sorted(
        str(identifier)
        for identifier in uncommitted_ids
        if any(str(identifier) in ids for ids in committed.values())
    )
    if leaked:
        raise ProjectionSafetyViolation(
            "non-committed record appeared in shared state: " + ", ".join(leaked)
        )


def agent_owned_memory_ids(records: Iterable[Any], agent_id: str) -> frozenset[str]:
    """Memory records *owned* by this agent inside the memory subsystem.

    Ownership is a memory-store property (``record.owner == agent_id``); it is
    not a definition of belief and is never treated as one. The runtime uses it
    to scope recall; E8 uses it only to name records that live in the memory
    store rather than in committed scientific state.
    """
    return frozenset(
        str(record.id) for record in records
        if getattr(record, "owner", None) == agent_id
    )


def peer_owned_memory_ids(records: Iterable[Any], agent_id: str) -> frozenset[str]:
    """Memory records owned by another agent, recalled as context only."""
    return frozenset(
        str(record.id) for record in records
        if getattr(record, "owner", None) != agent_id
    )


def assert_memory_excluded_from_state(
    state: ScientificState, records: Iterable[Any], agent_id: str
) -> None:
    """E8: memory records stay outside ``S_t``; only references cross over.

    A memory record may *reference* committed evidence (``evidence_refs``) —
    that is the sanctioned bridge — but the record itself is never committed,
    whether the agent owns it or recalled it from a peer. WP2 implements no
    private-belief store, so this is a memory-store boundary, not a claim about
    what an agent believes.
    """
    records = tuple(records)
    committed = _record_ids(state)
    owned = sorted(
        identifier for identifier in agent_owned_memory_ids(records, agent_id)
        if any(identifier in ids for ids in committed.values())
    )
    peer = sorted(
        identifier for identifier in peer_owned_memory_ids(records, agent_id)
        if any(identifier in ids for ids in committed.values())
    )
    if owned or peer:
        parts = []
        if owned:
            parts.append("agent-owned memory: " + ", ".join(owned))
        if peer:
            parts.append("peer memory recalled as context: " + ", ".join(peer))
        raise ProjectionSafetyViolation(
            "agent-local memory leaked into shared state (" + "; ".join(parts) + ")"
        )


def artifact_identity_index(state: ScientificState) -> dict[str, tuple[Any, ...]]:
    return {artifact.id: artifact_identity(artifact) for artifact in state.artifacts}


__all__ = [
    "ArtifactIdentityViolation",
    "ProjectionSafetyViolation",
    "agent_owned_memory_ids",
    "artifact_identity",
    "artifact_identity_index",
    "assert_artifact_identity_stable",
    "assert_identity_ignores_transport",
    "assert_memory_excluded_from_state",
    "assert_projection_is_read_only",
    "assert_projection_never_invents",
    "assert_projection_within_authorised_closure",
    "assert_records_excluded_from_state",
    "peer_owned_memory_ids",
]
