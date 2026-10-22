"""WP2 checkpoint 1: reserved-role views, scope model, commit-time guards.

Deterministic: no LLM, no device, no network. These tests pin the frozen WP2
pieces introduced by checkpoint 1:

* the scope model and its exact ``covers`` rows (``UNDECLARED`` vs
  ``DECLARED(frozenset())``),
* commit-time guards E1a (reserved-role schema), E3 (link integrity),
  E4a (within-delta duplicates) and E5a (scope shape validation),
* the three compatibility obligations (pre-WP2 ledger fails closed, post-WP2
  ledger replays, non-WP2 deltas keep their exact verification reasons).
"""
from __future__ import annotations

import pytest

from bioma.core.models import AgentMessage, Evidence, ScientificState, StateDelta
from bioma.core.scientific_relations import (
    CONTEXT,
    OPPOSES,
    RELATION_KEY,
    SUPPORTS,
    RelationVocabularyViolation,
    link_relation,
    parse_relation,
)
from bioma.core.scientific_scope import (
    DeclaredScope,
    ScopeShapeViolation,
    UNDECLARED,
    covers,
    parse_scope,
)
from bioma.core.scientific_views import (
    CLAIM_EVIDENCE_LINK_KIND,
    RESERVED_KINDS,
    SCIENTIFIC_CLAIM_KIND,
    claim_scope,
    is_reserved_kind,
    parse_claim,
    parse_link,
    partition_evidence,
    reserved_role_reasons,
)
from bioma.core.verification import VerificationEngine
from bioma.runtime.ledger import RunLedger


# --- shared builders -------------------------------------------------------


def _message() -> AgentMessage:
    return AgentMessage(id="source", sender="user", recipient="agent",
                        message_type="task.submitted")


def _delta(state, evidence):
    return StateDelta(agent_id="agent", source_message_id=_message().id,
                      expected_version=state.version, evidence=evidence)


def _verify(evidence, *, committed=()):
    state = ScientificState(task_id="wp2", objective="assess a claim",
                            evidence=list(committed))
    message = _message()
    delta = _delta(state, evidence)
    return VerificationEngine().verify(state, delta, expected_agent="agent",
                                       source_message=message)


def _claim(claim_id, statement="X binds Y", *, scope=None):
    value = None if scope is None else {"scope": scope}
    return Evidence(id=claim_id, kind=SCIENTIFIC_CLAIM_KIND, source="agent",
                    claim=statement, value=value)


def _substantive(evidence_id="obs-1"):
    return Evidence(id=evidence_id, kind="prediction", source="agent",
                    claim="measured affinity")


def _link(link_id, parents, *, relation=SUPPORTS):
    """A link carries one canonical relation; ``None`` omits the payload key."""
    value = None if relation is None else {RELATION_KEY: relation}
    return Evidence(id=link_id, kind=CLAIM_EVIDENCE_LINK_KIND, source="agent",
                    claim="links claim to evidence", parent_ids=list(parents),
                    value=value)


def _wp2_reasons(result):
    return [reason for reason in result.reasons if reason.startswith("[WP2")]


# --- scope model -----------------------------------------------------------


def test_undeclared_is_not_declared_unconditional():
    assert parse_scope(None) is UNDECLARED
    assert parse_scope({}) is UNDECLARED
    declared = parse_scope({"scope": []})
    assert declared == DeclaredScope(frozenset())
    assert declared is not UNDECLARED
    assert declared != UNDECLARED
    assert declared.unconditional is True


def test_covers_rows_are_frozen():
    unconditional = DeclaredScope(frozenset())
    scoped = DeclaredScope(frozenset({"human"}))
    assert covers(UNDECLARED, unconditional) is True
    assert covers(UNDECLARED, scoped) is False


def test_covers_declared_subset_and_unspecified_fail_closed():
    wide = DeclaredScope(frozenset({"human", "mouse"}))
    narrow = DeclaredScope(frozenset({"human"}))
    assert covers(wide, narrow) is True
    assert covers(narrow, wide) is False
    # Undocumented combinations fail closed rather than silently succeeding.
    assert covers(narrow, UNDECLARED) is False
    assert covers(UNDECLARED, UNDECLARED) is True


def test_exact_string_matching_has_no_synonyms_or_units():
    assert covers(DeclaredScope(frozenset({"Human"})),
                  DeclaredScope(frozenset({"human"}))) is False
    assert covers(DeclaredScope(frozenset({"10mg"})),
                  DeclaredScope(frozenset({"0.01g"}))) is False


@pytest.mark.parametrize("declared", ["human", 3, {"a": 1}])
def test_scope_shape_validation_rejects_non_sequences(declared):
    with pytest.raises(ScopeShapeViolation):
        parse_scope({"scope": declared})


@pytest.mark.parametrize("declared", [[""], ["  "], [None], ["human", 7]])
def test_scope_shape_validation_rejects_bad_entries(declared):
    with pytest.raises(ScopeShapeViolation):
        parse_scope({"scope": declared})


def test_scope_duplicates_collapse_because_matching_is_exact_strings():
    parsed = parse_scope({"scope": ["human", "human", "mouse"]})
    assert parsed == DeclaredScope(frozenset({"human", "mouse"}))


# --- reserved kinds and partition -----------------------------------------


def test_reserved_kinds_are_exactly_the_two_frozen_values():
    assert RESERVED_KINDS == frozenset(
        {SCIENTIFIC_CLAIM_KIND, CLAIM_EVIDENCE_LINK_KIND}
    )
    assert is_reserved_kind("scientific_claim") is True
    assert is_reserved_kind("claim_evidence_link") is True


def test_partition_is_total_and_all_other_kinds_stay_open():
    claim = _claim("c1")
    link = _link("l1", ["c1"])
    open_kinds = [
        Evidence(id="e1", kind="prediction", source="agent", claim="p"),
        Evidence(id="e2", kind="brand_new_kind", source="agent", claim="x"),
    ]
    partition = partition_evidence([claim, link, *open_kinds])
    assert partition.claims == (claim,)
    assert partition.links == (link,)
    assert [item.id for item in partition.substantive] == ["e1", "e2"]
    assert (len(partition.claims) + len(partition.substantive)
            + len(partition.links)) == 4


def test_typed_views_read_existing_fields_only():
    claim = _claim("c1", scope=["human"])
    view = parse_claim(claim)
    assert view.evidence_id == "c1"
    assert view.statement == "X binds Y"
    assert view.scope == DeclaredScope(frozenset({"human"}))
    assert claim_scope(_claim("c2")) is UNDECLARED

    link = parse_link(_link("l1", ["c1", "e1"]))
    assert link.parents == ("c1", "e1")


def test_parse_link_requires_at_least_one_parent():
    from bioma.core.scientific_views import ReservedRoleSchemaViolation

    with pytest.raises(ReservedRoleSchemaViolation):
        parse_link(Evidence(kind=CLAIM_EVIDENCE_LINK_KIND, source="agent",
                            claim="orphan link"))


# --- commit-time guards E1a / E3 / E4a / E5a ------------------------------


def test_e1a_rejects_a_claim_whose_value_is_not_a_mapping():
    result = _verify([Evidence(id="c1", kind=SCIENTIFIC_CLAIM_KIND, source="agent",
                               claim="bad payload", value=["not", "a", "mapping"])])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E1a]") for reason in result.reasons)


def test_e1a_rejects_an_endpointless_link():
    result = _verify([_link("l1", [])])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E1a]") for reason in result.reasons)


def test_e5a_rejects_a_malformed_scope():
    result = _verify([_claim("c1", scope="human")])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E5a]") for reason in result.reasons)


def test_e3_rejects_a_link_with_unknown_parents():
    result = _verify([_link("l1", ["ghost"])])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E3]") for reason in result.reasons)


def test_e3_requires_a_link_to_reference_a_scientific_claim():
    result = _verify([_substantive("obs-1"), _link("l1", ["obs-1"])])
    assert result.ok is False
    reasons = _wp2_reasons(result)
    assert any("must reference at least one scientific_claim" in r for r in reasons)


# --- commit-time guard E1c: canonical relation vocabulary -------------------


def test_relation_vocabulary_is_exactly_three_canonical_literals():
    from bioma.core.scientific_relations import LINK_RELATIONS, LINK_RELATION_SET

    assert LINK_RELATIONS == (SUPPORTS, OPPOSES, CONTEXT)
    assert LINK_RELATION_SET == frozenset({SUPPORTS, OPPOSES, CONTEXT})
    # The relation vocabulary shares no literal with the derived status set.
    from bioma.core.scientific_status import DERIVED_STATUSES

    assert LINK_RELATION_SET.isdisjoint(DERIVED_STATUSES)


def test_e1c_rejects_a_link_with_no_relation():
    result = _verify([_claim("c1"), _substantive("obs-1"),
                      _link("l1", ["c1", "obs-1"], relation=None)])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E1c]") for reason in result.reasons)


def test_e1c_rejects_a_link_with_an_unknown_relation():
    result = _verify([_claim("c1"), _substantive("obs-1"),
                      _link("l1", ["c1", "obs-1"], relation="contradicts")])
    assert result.ok is False
    reasons = _wp2_reasons(result)
    assert any(reason.startswith("[WP2 E1c]") for reason in reasons)


def test_e1c_rejects_a_non_string_relation():
    result = _verify([_claim("c1"), _substantive("obs-1"),
                      _link("l1", ["c1", "obs-1"], relation=1)])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E1c]") for reason in result.reasons)


def test_missing_relation_is_never_defaulted_to_supports(tmp_path):
    """A link without a relation cannot be committed, so it can never be read
    as a support either at commit time or at derivation time."""
    with pytest.raises(RelationVocabularyViolation):
        parse_relation(_link("l1", ["c1", "obs-1"], relation=None))
    assert link_relation(_link("l1", ["c1", "obs-1"], relation=None)) is None
    result = _verify([_claim("c1"), _substantive("obs-1"),
                      _link("l1", ["c1", "obs-1"], relation=None)])
    assert result.ok is False


@pytest.mark.parametrize("relation", [SUPPORTS, OPPOSES, CONTEXT])
def test_every_canonical_relation_is_accepted_at_commit(relation):
    result = _verify([_claim("c1"), _substantive("obs-1"),
                      _link("l1", ["c1", "obs-1"], relation=relation)])
    assert result.ok is True
    assert _wp2_reasons(result) == []
    assert parse_relation(_link("l1", ["c1", "obs-1"], relation=relation)) == relation


def test_e4a_keeps_links_with_the_same_parents_but_different_relations():
    """Support and opposition of the same target are two distinct assertions."""
    result = _verify([
        _claim("c1"),
        _substantive("obs-1"),
        _link("l1", ["c1", "obs-1"], relation=SUPPORTS),
        _link("l2", ["c1", "obs-1"], relation=OPPOSES),
    ])
    assert result.ok is True
    assert _wp2_reasons(result) == []


def _admitted_run(state, evidence, protocol, chain):
    actor = protocol.actor(name="agent", capability=protocol.capability("agent", writable=("evidence",)),
        handler=lambda current, message: _delta(current, evidence).model_copy(
            update={"source_message_id": message.id}))
    return chain(actor=actor, state=state)


def test_e1c_holds_over_a_real_commit(tmp_path, protocol, chain):
    state = ScientificState(task_id="wp2-relation", objective="assess a claim")
    message = _message()
    evidence = [_claim("c1", scope=["human"]), _substantive("obs-1"),
                _link("l1", ["c1", "obs-1"], relation=OPPOSES)]
    run = _admitted_run(state, evidence, protocol, chain)
    ledger, after = run.ledger, run.after

    committed = {item.id: item for item in after.evidence}
    assert link_relation(committed["l1"]) == OPPOSES
    assert ledger.verify_chain("wp2-relation") is True


def test_e4a_rejects_two_identical_claims_in_one_delta():
    result = _verify([
        _claim("c1", "same statement", scope=["human"]),
        _claim("c2", "same statement", scope=["human"]),
    ])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E4a]") for reason in result.reasons)


def test_e4a_keeps_distinct_contradicting_claims():
    """Different (statement, scope) pairs are not duplicates: both must survive."""
    result = _verify([
        _claim("c1", "X binds Y", scope=["human"]),
        _claim("c2", "X binds Y", scope=["mouse"]),
    ])
    assert _wp2_reasons(result) == []


def test_e4a_rejects_a_duplicated_link_within_one_delta():
    result = _verify([
        _claim("c1"),
        _substantive("obs-1"),
        _link("l1", ["c1", "obs-1"]),
        _link("l2", ["c1", "obs-1"]),
    ])
    assert result.ok is False
    assert any(reason.startswith("[WP2 E4a]") for reason in result.reasons)


def test_a_well_formed_claim_and_link_contribute_no_wp2_reason():
    result = _verify([
        _claim("c1", scope=[]),
        _substantive("obs-1"),
        _link("l1", ["c1", "obs-1"]),
    ])
    assert _wp2_reasons(result) == []
    assert result.ok is True


# --- compatibility ---------------------------------------------------------


def test_non_wp2_delta_reasons_are_unchanged_by_the_hook(monkeypatch):
    """The hook is provably inert for deltas with no reserved kind."""
    evidence = [_substantive("obs-1")]
    delta = _delta(ScientificState(task_id="wp2", objective="x"), evidence)
    assert reserved_role_reasons(delta, committed=()) == []

    def baseline_reasons():
        state = ScientificState(task_id="wp2", objective="assess a claim")
        message = _message()
        return VerificationEngine().verify(
            state, _delta(state, evidence), expected_agent="agent",
            source_message=message,
        ).reasons

    with_hook = baseline_reasons()
    monkeypatch.setattr(
        "bioma.core.scientific_views.reserved_role_reasons", lambda *a, **k: []
    )
    assert baseline_reasons() == with_hook
    assert not any(reason.startswith("[WP2") for reason in with_hook)


def test_post_wp2_ledger_replays_and_chain_verifies(tmp_path, protocol, chain):
    state = ScientificState(task_id="wp2-replay", objective="assess a claim")
    message = _message()
    evidence = [_claim("c1", scope=["human"]), _substantive("obs-1"),
                _link("l1", ["c1", "obs-1"])]
    run = _admitted_run(state, evidence, protocol, chain)
    ledger, after = run.ledger, run.after
    assert ledger.verify_chain("wp2-replay") is True
    assert ledger.replay_state("wp2-replay").version == after.version


def test_pre_wp2_ledger_fails_closed_with_unsupported_policy(tmp_path, protocol, chain):
    """A receipt whose policy hash predates the WP2 hook cannot be replayed.

    Recorded ledgers are never silently reinterpreted under a different
    verification policy: they fail closed and are retained for audit.
    """
    state = ScientificState(task_id="wp2-legacy", objective="assess a claim")
    message = _message()
    run = _admitted_run(state, [_claim("c1", scope=["human"])], protocol, chain)
    legacy = RunLedger(tmp_path / "legacy.db")
    for event in run.ledger.events("wp2-legacy"):
        payload = dict(event.payload)
        if event.kind == "TransitionCommitted":
            payload["receipt"] = {**payload["receipt"], "policy_hash": "0" * 64}
        legacy.append("wp2-legacy", event.kind, event.actor, payload)
    assert legacy.verify_chain("wp2-legacy") is True
    with pytest.raises(ValueError, match="Unsupported verification policy"):
        legacy.replay_state("wp2-legacy")
