from bioma.core.models import Evidence, ScientificState
from bioma.core.scientific_status import derive_claim_status
from bioma.evidence.synthesis import EvidenceAssessment, EvidenceSynthesis, build_evidence_graph
from bioma.evidence.wp2_reconciliation import map_com_relation, wp2_claim_projection


def link(identifier, claim, target, relation):
    return Evidence(id=identifier, kind="claim_evidence_link", claim="relation",
                    source="test", value={"relation": relation}, parent_ids=[claim, target])


def test_com_supported_cannot_override_wp2_conflicted():
    claim = Evidence(id="c", kind="scientific_claim", claim="C", source="test", value={"scope": []})
    yes = Evidence(id="yes", kind="result", claim="yes", source="test")
    no = Evidence(id="no", kind="result", claim="no", source="test")
    state = ScientificState(task_id="t", objective="C", gateway={"claims": [{"id":"c", "text":"C"}]},
        evidence=[claim, yes, no, link("l1", "c", "yes", "supports"), link("l2", "c", "no", "opposes")])
    synthesis = EvidenceSynthesis(primary_claim="C", claim_status="supported", confidence=.9,
        summary="analysis", method="test", assessments=[EvidenceAssessment(
            evidence_id="yes", relation="supports", rationale="analytical")])
    projection = wp2_claim_projection(state, "c", "C")
    assert projection["authoritative_status"] == "conflicted"
    assert build_evidence_graph(state, synthesis)["nodes"][1]["status_authority"] == "non_authoritative_com_analysis"
    assert build_evidence_graph(state, synthesis)["nodes"][1]["authoritative_status_source"] == "WP2.derive_claim_status"


def test_scope_inapplicable_support_does_not_support_and_wp2_derives_without_com():
    claim = Evidence(id="c", kind="scientific_claim", claim="C", source="test", value={"scope": ["human"]})
    target = Evidence(id="mouse", kind="scientific_claim", claim="mouse", source="test", value={"scope": ["mouse"]})
    records = [claim, target, link("l", "c", "mouse", "supports")]
    assert derive_claim_status(records, "c") == "insufficient"
    state = ScientificState(task_id="t", objective="C", evidence=records)
    assert wp2_claim_projection(state, "c", "C")["relation_tally"]["excluded_by_scope"] == 1


def test_relation_adapter_is_explicit_and_insufficient_is_not_a_wp2_link():
    assert map_com_relation("supports") == "supports"
    assert map_com_relation("contradicts") == "opposes"
    assert map_com_relation("context") == "context"
    assert map_com_relation("insufficient") is None
