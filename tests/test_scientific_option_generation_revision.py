"""Phase D1 evidence-conditioned option generation and constrained revision."""
from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from bioma.core.models import Evidence, ScientificState
from bioma.core.research_contract import (
    AcceptanceObligation, MethodRestriction, ObligationRole, ResearchContract,
)
from bioma.scientific_control import (
    BoundedRevisionPolicy, DecisionSnapshot, Lifecycle, MethodBinding, NodeKind,
    ObligationProgress, OptionIntent, PlanNode, PlanDerivedOptionGenerator,
    ResearchPlan, RevisionOperation, RevisionOperationKind, RevisionProposal,
    RevisionTrigger, RevisionTriggerEvent, apply_revision_proposal,
    validate_generated_options, validate_revision_proposal,
)


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def fixture(*, restrictions=(), alternatives=True, unresolved=()):
    obligation = AcceptanceObligation(id="O", description="Resolve the question",
        evidence_kinds=("result",), role=ObligationRole.QUESTION)
    contract = ResearchContract(query="Q", input_hash="I", requirements=(obligation,),
                                method_restrictions=restrictions)
    bindings = (MethodBinding(binding_id="B1", capability="cap", method_id="M1"),)
    if alternatives:
        bindings += (MethodBinding(binding_id="B2", capability="cap", method_id="M2"),)
    node = PlanNode(node_id="N", kind=NodeKind.QUESTION, description="Resolve O",
        contract_obligation_refs=("O",), candidate_bindings=bindings,
        selected_binding_id="B1")
    plan = ResearchPlan(plan_id="P", version=1, contract_ref=contract.identity,
        evidence_snapshot_ref="E:0", nodes=(node,))
    progress = ObligationProgress(obligation_ref="O", role=ObligationRole.QUESTION,
        mandatory=True, terminal=True, status="UNRESOLVED", progress_credit=False,
        acceptance_status="missing", rationale="fixture")
    snapshot = DecisionSnapshot(contract_ref=contract.identity, contract_version=1,
        plan_ref="P", plan_version=1, plan_identity_hash=plan.version_identity[2],
        committed_state_ref="E", committed_state_version=0, runtime_fingerprint="R",
        unresolved_effect_refs=unresolved, ready_plan_node_refs=("N",),
        obligation_progress=(progress,), progress_value=0, timestamp=NOW)
    state = ScientificState(task_id="T", objective="Q")
    return contract, plan, snapshot, state


def trigger(contract, plan, snapshot, cause, *, details=None, refs=("evidence:E",), affected=("N",)):
    return RevisionTriggerEvent(trigger_id="trigger:" + cause.value,
        cause=cause, contract_ref=contract.identity, plan_identity=plan.version_identity,
        decision_snapshot_identity=snapshot.identity, authoritative_refs=refs,
        affected_node_refs=affected, details_json=json.dumps(details or {}),
        created_at=NOW, provenance_refs=("runtime",))


def test_plan_generator_proposes_multiple_finite_scientific_options():
    contract, plan, snapshot, state = fixture()
    options = PlanDerivedOptionGenerator().generate(snapshot, contract, plan, state, ())
    validated = validate_generated_options(options, snapshot=snapshot, contract=contract, plan=plan)
    assert len(validated) == 4  # two bindings x direct/falsification
    assert {item.intent_class for item in validated} == {
        OptionIntent.DIRECT_PROGRESS, OptionIntent.FALSIFICATION}
    assert all(item.source_snapshot_identity == snapshot.identity for item in validated)


def test_duplicate_equivalent_proposals_are_deduplicated_deterministically():
    contract, plan, snapshot, state = fixture(alternatives=False)
    original = PlanDerivedOptionGenerator().generate(snapshot, contract, plan, state, ())[0]
    duplicate = original.model_copy(update={"option_id": "duplicate", "proposer_id": "role-agent",
                                            "provenance_refs": ("other",)})
    first = validate_generated_options((duplicate, original), snapshot=snapshot,
                                       contract=contract, plan=plan)
    second = validate_generated_options((original, duplicate), snapshot=snapshot,
                                        contract=contract, plan=plan)
    assert first == second and len(first) == 1


def test_generated_option_staleness_and_unknown_targets_fail_closed():
    contract, plan, snapshot, state = fixture(alternatives=False)
    option = PlanDerivedOptionGenerator().generate(snapshot, contract, plan, state, ())[0]
    with pytest.raises(ValueError, match="stale"):
        validate_generated_options((option.model_copy(update={"source_snapshot_identity": "old"}),),
            snapshot=snapshot, contract=contract, plan=plan)
    with pytest.raises(ValueError, match="unknown"):
        validate_generated_options((option.model_copy(update={"target_plan_node_refs": ("missing",)}),),
            snapshot=snapshot, contract=contract, plan=plan)


def test_explicit_user_method_restriction_is_preserved_before_grounding():
    restriction = MethodRestriction(obligation_ref="O", method_id="M1")
    contract, plan, snapshot, state = fixture(restrictions=(restriction,))
    options = PlanDerivedOptionGenerator().generate(snapshot, contract, plan, state, ())
    with pytest.raises(ValueError, match="method restriction"):
        validate_generated_options(options, snapshot=snapshot, contract=contract, plan=plan)


def test_contradictory_committed_evidence_proposes_discriminative_route():
    contract, plan, snapshot, _ = fixture()
    event = trigger(contract, plan, snapshot, RevisionTrigger.CONTRADICTION)
    proposal = BoundedRevisionPolicy().propose(event, snapshot, contract, plan)
    assert proposal.operations[0].kind is RevisionOperationKind.ADD_NODE
    revision, revised = apply_revision_proposal(proposal, trigger=event, snapshot=snapshot,
        contract=contract, plan=plan)
    added = next(node for node in revised.nodes if node.node_id != "N")
    assert "Discriminate" in added.description and added.contract_obligation_refs == ("O",)
    assert revision.trigger_type is RevisionTrigger.CONTRADICTION


def test_negative_scientific_evidence_changes_route_not_contract():
    contract, plan, snapshot, state = fixture()
    before = contract.identity, contract.model_dump_json(), state.model_dump_json()
    event = trigger(contract, plan, snapshot, RevisionTrigger.EVIDENCE_UPDATE,
                    details={"scientific_outcome": "qualified_negative"})
    proposal = BoundedRevisionPolicy().propose(event, snapshot, contract, plan)
    _, revised = apply_revision_proposal(proposal, trigger=event, snapshot=snapshot,
        contract=contract, plan=plan)
    assert before == (contract.identity, contract.model_dump_json(), state.model_dump_json())
    assert revised.version == 2


def test_provider_failure_replaces_selected_binding_without_scientific_mutation():
    contract, plan, snapshot, state = fixture()
    replacement = MethodBinding(binding_id="B3", capability="cap", method_id="M3",
                                provider_id="replacement")
    event = trigger(contract, plan, snapshot, RevisionTrigger.EXECUTION_FAILURE,
                    details={"replacement_binding": replacement.model_dump(mode="json")},
                    refs=("attempt:failed",))
    proposal = BoundedRevisionPolicy().propose(event, snapshot, contract, plan)
    _, revised = apply_revision_proposal(proposal, trigger=event, snapshot=snapshot,
        contract=contract, plan=plan)
    node = revised.nodes[0]
    assert node.selected_binding_id == "B3"
    assert {item.binding_id for item in node.candidate_bindings} == {"B1", "B2", "B3"}
    assert state.evidence == [] and contract.identity == plan.contract_ref


def test_unselected_viable_alternatives_survive_ordinary_revision():
    contract, plan, snapshot, _ = fixture()
    replacement = MethodBinding(binding_id="B3", capability="cap", method_id="M3")
    event = trigger(contract, plan, snapshot, RevisionTrigger.CAPABILITY_CHANGE,
                    details={"replacement_binding": replacement.model_dump(mode="json")})
    proposal = BoundedRevisionPolicy().propose(event, snapshot, contract, plan)
    _, revised = apply_revision_proposal(proposal, trigger=event, snapshot=snapshot,
        contract=contract, plan=plan)
    assert {item.binding_id for item in revised.nodes[0].candidate_bindings} >= {"B1", "B2"}


def test_forecast_only_information_cannot_create_authoritative_trigger():
    contract, plan, snapshot, _ = fixture()
    with pytest.raises(ValidationError, match="forecast output"):
        trigger(contract, plan, snapshot, RevisionTrigger.EVIDENCE_UPDATE,
                details={"authority": "forecast"}, refs=("forecast:F",))


def test_superseded_node_retains_lineage_and_replacement():
    contract, plan, snapshot, _ = fixture()
    event = trigger(contract, plan, snapshot, RevisionTrigger.CONTRADICTION)
    replacement = PlanNode(node_id="N2", kind=NodeKind.WORK_ITEM, description="Replacement",
        contract_obligation_refs=("O",), supersedes_node_ref="N",
        candidate_bindings=plan.nodes[0].candidate_bindings)
    proposal = RevisionProposal(proposal_id="RP", contract_ref=contract.identity,
        source_plan_identity=plan.version_identity, decision_snapshot_identity=snapshot.identity,
        trigger_ref=event.trigger_id, operations=(
            RevisionOperation(kind=RevisionOperationKind.SUPERSEDE_NODE, node_ref="N",
                              rationale="Replace route"),
            RevisionOperation(kind=RevisionOperationKind.ADD_NODE, node=replacement,
                              rationale="Preserve replacement lineage")),
        rationale="Traceable replacement", provenance_refs=(event.trigger_id,))
    _, revised = apply_revision_proposal(proposal, trigger=event, snapshot=snapshot,
        contract=contract, plan=plan)
    assert next(node for node in revised.nodes if node.node_id == "N").lifecycle is Lifecycle.SUPERSEDED
    assert next(node for node in revised.nodes if node.node_id == "N2").supersedes_node_ref == "N"


def test_stale_trigger_and_revision_are_rejected():
    contract, plan, snapshot, _ = fixture()
    event = trigger(contract, plan, snapshot, RevisionTrigger.OPTION_COMPLETED)
    stale = event.model_copy(update={"plan_identity": ("P", 2, "new")})
    with pytest.raises(ValueError, match="stale"):
        BoundedRevisionPolicy().propose(stale, snapshot, contract, plan)
    evidence_event = trigger(contract, plan, snapshot, RevisionTrigger.EVIDENCE_UPDATE)
    proposal = BoundedRevisionPolicy().propose(evidence_event, snapshot, contract, plan)
    with pytest.raises(ValueError, match="stale"):
        validate_revision_proposal(proposal, trigger=evidence_event,
            snapshot=snapshot.model_copy(update={"plan_version": 2}), contract=contract, plan=plan)


def test_missing_mandatory_obligation_coverage_is_rejected():
    contract, plan, snapshot, _ = fixture()
    event = trigger(contract, plan, snapshot, RevisionTrigger.OPTION_BLOCKED)
    proposal = RevisionProposal(proposal_id="RP", contract_ref=contract.identity,
        source_plan_identity=plan.version_identity, decision_snapshot_identity=snapshot.identity,
        trigger_ref=event.trigger_id, operations=(RevisionOperation(
            kind=RevisionOperationKind.SUPERSEDE_NODE, node_ref="N", rationale="Remove only route"),),
        rationale="Invalid removal", provenance_refs=(event.trigger_id,))
    with pytest.raises(ValueError, match="mandatory-obligation coverage"):
        validate_revision_proposal(proposal, trigger=event, snapshot=snapshot,
                                   contract=contract, plan=plan)


def test_effect_unknown_blocks_incompatible_revision():
    contract, plan, snapshot, _ = fixture(unresolved=("effect:unknown",))
    event = trigger(contract, plan, snapshot, RevisionTrigger.CAPABILITY_CHANGE,
        details={"replacement_binding": MethodBinding(binding_id="B3", capability="cap",
                 method_id="M3").model_dump(mode="json")})
    proposal = BoundedRevisionPolicy().propose(event, snapshot, contract, plan)
    with pytest.raises(ValueError, match="EFFECT_UNKNOWN"):
        validate_revision_proposal(proposal, trigger=event, snapshot=snapshot,
                                   contract=contract, plan=plan)


def test_no_progress_completion_retains_plan_without_version_churn():
    contract, plan, snapshot, state = fixture()
    event = trigger(contract, plan, snapshot, RevisionTrigger.OPTION_COMPLETED)
    proposal = BoundedRevisionPolicy().propose(event, snapshot, contract, plan)
    assert proposal is None
    assert plan.version == 1 and state.evidence == []
    assert not {"evidence", "claim", "value"}.intersection(RevisionProposal.model_fields)


def test_completed_action_dedup_tracks_information_not_control_version():
    from bioma.scientific_control.generation import action_information_key, completed_action
    from bioma.core.models import Observation
    state = ScientificState(task_id='T', objective='Q')
    inputs = {'file': 'frozen.csv', 'parameter': 1}
    key = action_information_key(state, ['q1'], 'compute', 'fit', inputs)
    state = state.model_copy(update={'status_reason': {'control_completed_actions': {key: {'execution_ref':'real'}}}})
    assert completed_action(state, ['q1'], 'compute', 'fit', inputs)
    assert completed_action(state.model_copy(update={'version': 22}), ['q1'], 'compute', 'fit', inputs)
    assert not completed_action(state, ['q2'], 'compute', 'fit', inputs)
    assert not completed_action(state, ['q1'], 'independent', 'fit', inputs)
    assert not completed_action(state, ['q1'], 'compute', 'fit', {**inputs,'parameter':2})
    changed = state.model_copy(update={'observations': [Observation(kind='measurement',source='test',value={'x':2})]})
    assert not completed_action(changed, ['q1'], 'compute', 'fit', inputs)


def test_same_method_can_bind_distinct_work_but_exact_work_stays_duplicate():
    from bioma.core.models import CapabilitySpec
    from bioma.core.methods import ExecutionMethod,ResultContract
    from bioma.scientific_control.generation import LLMEvidenceDrivenOptionGenerator, action_information_key, completed_action
    contract,plan,snapshot,state=fixture(alternatives=False)
    cap=CapabilitySpec(id='cap',family='scientific.compute',agent_id='compute',
        accepts_message_types=['work.requested'],methods=[ExecutionMethod(id='M1',description='Local computation',
            message_type='work.requested',result=ResultContract(responsibility='scientific.compute',evidence_kinds=['result'],verification_obligations=['integrity']))])
    state=state.model_copy(update={'inputs':{'files':['frozen.csv']}})
    key=action_information_key(state,['O'],'compute','M1',state.inputs)
    state=state.model_copy(update={'status_reason':{'control_completed_actions':{key:{'execution_ref':'prior'}}}})
    class Model:
        def chat(self,messages,**kwargs):
            payload=json.loads(messages[-1]['content'])
            assert any(row['method_id']=='M1' for row in payload['method_catalog'])
            common={'node_ref':'N','obligation_refs':['O'],'capability_id':'cap','method_id':'M1',
                    'scientific_question':'Fit local observations','rationale':'Compare model degrees','intent_class':'DIRECT_PROGRESS'}
            return json.dumps({'choices':[{**common,'analysis_work':{'operation':'polynomial_fit','variables':['y','x'],'parameters':{'degree':d}}} for d in [1,2]]})
    generator=LLMEvidenceDrivenOptionGenerator(Model(),model_id='offline')
    options=generator.generate(snapshot,contract,plan,state,[cap])
    assert len(options)==2 and len({o.option_id for o in options})==2
    inputs=[json.loads(o.steps[0].input_bindings_json) for o in options]
    completed=action_information_key(state,['O'],'compute','M1',inputs[0])
    state=state.model_copy(update={'status_reason':{'control_completed_actions':{completed:{'execution_ref':'actual'}}}})
    assert completed_action(state,['O'],'compute','M1',inputs[0])
    assert not completed_action(state,['O'],'compute','M1',inputs[1])
    class Stop:
        def chat(self,*args,**kwargs):return json.dumps({'choices':[],'abstention_reason':'No additional work justified'})
    generator=LLMEvidenceDrivenOptionGenerator(Stop(),model_id='offline')
    assert generator.generate(snapshot,contract,plan,state,[cap])==()
    assert generator.last_audit['catalog_size']>0
    assert generator.last_audit['termination_cause']=='MODEL_ABSTAINED'
