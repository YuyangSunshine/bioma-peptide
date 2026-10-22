"""Phase D2 offline proof of the real adaptive production path."""
from datetime import datetime, timezone

from bioma import BioMA
from bioma.backends.base import ScientificBackend, MethodBinding
from bioma.core.methods import ExecutionMethod, ResultContract
from bioma.core.models import Evidence, ScientificTask
from bioma.core.research import EvidenceRequirement, prepare_contract
from bioma.runtime.scientific_control import ControlBudget
from bioma.runtime.effects import EffectUncertain
from bioma.core.scientific_capabilities import WorkResult
from bioma.runtime.result_qualification import WorkResultQualificationService
from bioma.scientific_control.forecasting import RuleOutcomeForecaster


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


class AdaptiveResultAgent(ScientificBackend):
    def __init__(self, *, qualified_outcome=None, peer="adaptive-worker",
                 failure=None):
        self.peer = peer
        self.qualified_outcome = qualified_outcome
        self.failure = failure
        self.calls = 0
        method = ExecutionMethod(id="adaptive-method",
            description="Produce an offline deterministic result", message_type="work.requested",
            result=ResultContract(responsibility="scientific.compute", result_channel="work_result", evidence_kinds=["AnalysisResult"],
                                  verification_obligations=["integrity"]))
        super().__init__("scientific.compute", peer, [MethodBinding(method, self.execute)])


    def execute(self, state, message, tools):
        self.calls += 1
        if self.failure == "ordinary":
            raise RuntimeError("offline provider failure")
        if self.failure == "effect":
            raise EffectUncertain("offline unresolved effect")
        value = ({"qualified_outcome": self.qualified_outcome}
                 if self.qualified_outcome else {"value": 1})
        return WorkResult(family="scientific.compute", kind="AnalysisResult", payload=value,
            provenance={"method": "adaptive-method", "fixture": "controlled-outcome-v1"})


def qualify_fixture(work):
    # A controlled outcome oracle for control/recovery tests, not a scientific validator.
    if work.provenance.get("fixture") != "controlled-outcome-v1":
        return None
    return Evidence(kind="AnalysisResult", claim="Controlled fixture outcome",
        value=work.payload, source=work.provenance["provider_id"], method="fixture-oracle-v1")


def fixture_qualifier():
    return WorkResultQualificationService("controlled-outcome-v1", qualify_fixture)


def governed_task(task_id="adaptive-run", *, question=False, active_agents=None):
    requirement = EvidenceRequirement(id="O", description="Resolve result",
        evidence_kinds=["AnalysisResult"], capability="scientific.compute",
        method_id="adaptive-method", message_type="work.requested",
        mode="evidence")
    gateway = {"acceptance_mode": "work", "selected_entry_capability": "scientific.compute",
        "selected_entry_method": "adaptive-method", "selected_message_type": "work.requested",
        "evidence_requirements": [requirement.model_dump(mode="json")]}
    if question:
        gateway["obligation_roles"] = {"O": "QUESTION"}
    return prepare_contract(ScientificTask(id=task_id, objective="Resolve the offline result.",
        initial_agent="queryagent", initial_capability="research.problem_formulation",
        active_agents=active_agents or ["adaptive-worker"], gateway=gateway))


class PreferFailingProvider(RuleOutcomeForecaster):
    def forecast(self, snapshot, option, grounded, contract, evidence_state):
        result = super().forecast(snapshot, option, grounded, contract, evidence_state)
        cost = 0 if grounded.steps[0].provider_id == "failing-worker" else 1
        return result.model_copy(update={"branches": (
            result.branches[0].model_copy(update={"cost": cost}),)})


def test_adaptive_runtime_freezes_two_cycle_production_trace_and_resumes(tmp_path):
    agent = AdaptiveResultAgent()
    app = BioMA(backends=[agent], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(),
        ledger_path=tmp_path / "ledger.db", control_budget=ControlBudget(rollout_horizon=2))
    app.scientific_control_runtime.clock = lambda: NOW
    task = governed_task()
    state = app.run(task)
    assert state.status == "completed"
    assert state.status_reason["control_outcome"] == "COMPLETED"
    assert agent.calls == 1
    events = app.runtime.ledger.events(task.id)
    kinds = [row.kind for row in events]
    expected = ["AdaptiveContractPlanBootstrapped", "DecisionSnapshot", "OptionProposal",
        "Grounding", "OutcomeForecast", "ControllerDecision", "WP1PermitObserved",
        "ExecutionReceiptObserved", "ScientificCommitObserved", "RevisionTrigger",
        "PlanRevision", "DecisionSnapshot"]
    positions = []
    cursor = 0
    for kind in expected:
        cursor = kinds.index(kind, cursor)
        positions.append(cursor)
        cursor += 1
    assert positions == sorted(positions)
    proposals = next(row for row in events if row.kind == "OptionProposal")
    assert len(proposals.payload["options"]) >= 1
    forecast_at = kinds.index("OutcomeForecast")
    decision_at = kinds.index("ControllerDecision")
    permit_at = kinds.index("WP1PermitObserved")
    assert forecast_at < decision_at < permit_at
    decision = next(row for row in events if row.kind == "ControllerDecision")
    assert decision.payload["selected_grounded_option_identity"]
    assert not any(row.kind == "Evidence" for row in events)  # control records are not evidence
    before = len([row for row in events if row.kind == "WP1PermitObserved"])
    resumed = app.run(task)
    after = len([row for row in app.runtime.ledger.events(task.id)
                 if row.kind == "WP1PermitObserved"])
    assert resumed.model_dump(mode="json") == state.model_dump(mode="json")
    assert before == after == 1 and agent.calls == 1


def test_falsifying_question_is_resolved_by_committed_negative_result(tmp_path):
    agent = AdaptiveResultAgent(qualified_outcome="negative")
    app = BioMA(backends=[agent], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(),
        ledger_path=tmp_path / "question.db")
    app.scientific_control_runtime.clock = lambda: NOW
    state = app.run(governed_task("negative-question", question=True))
    assert state.status_reason["control_outcome"] == "COMPLETED"
    assert state.evidence[0].value["qualified_outcome"] == "negative"


def test_adaptive_failure_is_visible_and_never_falls_back_to_legacy(tmp_path):
    task = governed_task("no-provider")
    app = BioMA(backends=[], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(), ledger_path=tmp_path / "none.db")
    state = app.run(task)
    assert state.status == "blocked"
    assert state.status_reason["control_outcome"] == "BLOCKED"
    assert not any(row.kind == "TransitionCommitted" for row in app.runtime.ledger.events(task.id))


def test_failed_provider_route_is_replaced_without_scientific_mutation(tmp_path):
    failed = AdaptiveResultAgent(peer="failing-worker", failure="ordinary")
    replacement = AdaptiveResultAgent(peer="replacement-worker")
    app = BioMA(backends=[failed, replacement], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(),
        outcome_forecaster=PreferFailingProvider(), ledger_path=tmp_path / "replace.db",
        control_budget=ControlBudget(rollout_horizon=1))
    app.scientific_control_runtime.clock = lambda: NOW
    task = governed_task("provider-replacement",
        active_agents=["failing-worker", "replacement-worker"])
    state = app.run(task)
    assert state.status_reason["control_outcome"] == "COMPLETED"
    assert failed.calls == replacement.calls == 1
    results = [row for row in state.observations if row.kind == "work_result"]
    assert [row.source for row in results] == ["replacement-worker"]
    qualified = [row for row in state.evidence if row.kind == "AnalysisResult"]
    assert len(qualified) == 1
    assert qualified[0].source == "harness"
    assert qualified[0].value["work_result_ref"] == results[0].id
    records = [row.payload for row in app.runtime.ledger.events(task.id)
               if row.kind == "OptionExecutionRecord"]
    assert [row["lifecycle"] for row in records] == ["FAILED", "COMPLETED"]
    trigger = next(e.payload for e in app.runtime.ledger.events(task.id)
        if e.kind == "RevisionTrigger" and e.payload["cause"] == "EXECUTION_FAILURE")
    import json
    detail = json.loads(trigger["details_json"])["execution_failure"]
    assert detail["reason"] == "offline provider failure"
    assert detail["attempt_refs"]



def test_effect_unknown_suspends_and_resume_never_repeats_effect(tmp_path):
    agent = AdaptiveResultAgent(failure="effect")
    app = BioMA(backends=[agent], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(),
        ledger_path=tmp_path / "effect.db")
    app.scientific_control_runtime.clock = lambda: NOW
    task = governed_task("effect-unknown")
    suspended = app.run(task)
    assert suspended.status_reason["control_outcome"] == "SUSPENDED"
    assert agent.calls == 1
    resumed = app.run(task)
    assert resumed.model_dump(mode="json") == suspended.model_dump(mode="json")
    assert agent.calls == 1


def test_resume_after_proposal_transport_failure_preserves_snapshot(tmp_path):
    import pytest
    from bioma.scientific_control.generation import EvidenceDrivenOptionGenerator
    class Interrupted(EvidenceDrivenOptionGenerator):
        calls = 0
        def generate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("interrupted model transport before proposal")
            return super().generate(*args, **kwargs)
    agent = AdaptiveResultAgent()
    app = BioMA(backends=[agent], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(),
        option_generator=Interrupted(), ledger_path=tmp_path/"resume.db")
    task = governed_task("resume-interruption")
    with pytest.raises(RuntimeError, match="interrupted model transport"):
        app.run(task)
    # Real clock changes between runs. Resumption must not rewrite old evidence.
    state = app.run(task)
    assert state.status == "completed"
    assert agent.calls == 1
    rows = [e for e in app.runtime.ledger.events(task.id)
            if e.idempotency_key == "decision-snapshot:0"]
    assert len(rows) == 1


def test_resume_refreshes_unproposed_runtime_catalog_without_overwriting(tmp_path):
    import pytest
    from bioma.scientific_control.generation import EvidenceDrivenOptionGenerator
    class Interrupted(EvidenceDrivenOptionGenerator):
        calls = 0
        def generate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("proposal interrupted")
            return super().generate(*args, **kwargs)
    agent = AdaptiveResultAgent()
    app = BioMA(backends=[agent], control_policy="adaptive_v3", result_qualifier=fixture_qualifier(),
        option_generator=Interrupted(), ledger_path=tmp_path/"catalog-resume.db")
    task = governed_task("catalog-resume")
    with pytest.raises(RuntimeError, match="proposal interrupted"):
        app.run(task)
    app.runtime.available_resources = {"newly-discovered-local-resource"}
    assert app.run(task).status == "completed"
    snapshots = [e for e in app.runtime.ledger.events(task.id)
        if e.kind == "DecisionSnapshot" and e.idempotency_key.startswith("decision-snapshot:0")]
    assert len(snapshots) == 2
    assert snapshots[0].payload["runtime_fingerprint"] != snapshots[1].payload["runtime_fingerprint"]
    assert agent.calls == 1
