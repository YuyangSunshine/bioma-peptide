import json
import unittest
from datetime import datetime, timezone

from bioma.runtime.participant import RuntimeParticipant
from bioma.core.methods import ExecutionMethod, ResultContract
from bioma.core.models import CapabilitySpec, ScientificState, ScientificTask
from bioma.core.research import EvidenceRequirement, contract_for, prepare_contract
from bioma.core.research_planning import plan_for
from bioma.runtime.engine import DecentralizedRuntime
from bioma.scientific_control.forecast import decision_snapshot
from bioma.scientific_control.generation import EvidenceDrivenOptionGenerator, validate_generated_options


class InventoryAgent(RuntimeParticipant):
    def __init__(self):
        super().__init__(["inventory-provider"])
        result = ResultContract(responsibility="adaptive.result", evidence_kinds=["result"],
                                verification_obligations=["integrity"])
        self.advertise(CapabilitySpec(id="adaptive.result", family="adaptive.result",
            agent_id="inventory-provider", accepts_message_types=["work.requested"],
            methods=[ExecutionMethod(id="alternative", description="Independent alternative",
                message_type="work.requested", result=result)]))

    def handle(self, state, message):
        raise AssertionError("generation must not execute a provider")


def fixture(tmp_path):
    requirement = EvidenceRequirement(id="O", description="Resolve result",
        evidence_kinds=["result"], capability="adaptive.result", method_id="initial",
        message_type="work.requested", mode="evidence")
    task = prepare_contract(ScientificTask(id="dynamic-options", objective="Resolve result",
        initial_capability="research.problem_formulation", gateway={
            "acceptance_mode": "work", "selected_entry_capability": "adaptive.result",
            "selected_entry_method": "initial", "selected_message_type": "work.requested",
            "evidence_requirements": [requirement.model_dump(mode="json")]}))
    state = ScientificState.from_task(task)
    contract, plan = contract_for(state), plan_for(state)
    runtime = DecentralizedRuntime([InventoryAgent()], ledger_path=tmp_path / "ledger.sqlite3")
    snapshot = decision_snapshot(contract=contract, plan=plan, state=state,
        registry=runtime.capability_registry, active_agents=set(runtime.agents),
        available_resources=runtime.available_resources, remaining_budget=3,
        timestamp=datetime(2026, 10, 10, tzinfo=timezone.utc))
    return state, contract, plan, runtime, snapshot


class EvidenceDrivenGenerationTests(unittest.TestCase):
    def test_live_matching_capability_adds_bounded_alternative_without_mutation(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as root:
            state, contract, plan, runtime, snapshot = fixture(Path(root))
            frozen = (contract.model_dump_json(), plan.model_dump_json(), state.model_dump_json())
            options = EvidenceDrivenOptionGenerator().generate(snapshot, contract, plan, state,
                                                                 runtime.capability_registry.all())
            validated = validate_generated_options(options, snapshot=snapshot,
                                                    contract=contract, plan=plan)
            self.assertTrue(any(step.method_hint == "initial" for o in validated for step in o.steps))
            dynamic = [o for o in validated if any(s.method_hint == "alternative" for s in o.steps)]
            self.assertEqual(len(dynamic), 1)
            self.assertEqual(dynamic[0].target_contract_obligation_refs, ("O",))
            self.assertEqual(frozen, (contract.model_dump_json(), plan.model_dump_json(),
                                      state.model_dump_json()))


class CommittedResultAgent(RuntimeParticipant):
    def __init__(self, outcomes=("positive",), peer="committed-result"):
        super().__init__([peer])
        self.peer = peer
        self.outcomes = outcomes
        result = ResultContract(responsibility="adaptive.result", evidence_kinds=["result"],
                                verification_obligations=["integrity"])
        self.advertise(CapabilitySpec(id="adaptive.result", family="adaptive.result",
            agent_id=peer, accepts_message_types=["work.requested"],
            methods=[ExecutionMethod(id="initial", description="Qualified fixture observation",
                message_type="work.requested", result=result)]))

    def handle(self, state, message):
        from bioma.core.models import Evidence, StateDelta
        rows = [Evidence(id=f"result-{value}", kind="result", claim=f"Result is {value}",
            value={"qualified_outcome": value}, source=self.peer, method="initial")
            for value in self.outcomes]
        return StateDelta(agent_id=self.peer, source_message_id=message.id,
                          expected_version=state.version, evidence=rows, outcome="completed")


class EvidenceConditionedRuntimeTests(unittest.TestCase):
    def test_default_forecast_keeps_unknown_scientific_outcome_explicit(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma.scientific_control.forecasting import RuleOutcomeForecaster
        from bioma.scientific_control.grounding import ground_option
        with TemporaryDirectory() as root:
            state, contract, plan, runtime, snapshot = fixture(Path(root))
            options = EvidenceDrivenOptionGenerator().generate(snapshot, contract, plan, state,
                                                                 runtime.capability_registry.all())
            option = next(o for o in options if any(s.method_hint == "alternative" for s in o.steps))
            grounded = ground_option(option, contract=contract, plan=plan, state=state,
                registry=runtime.capability_registry,
                assessors={peer: agent.assess for peer, agent in runtime.agents.items()},
                active_agents=set(runtime.agents), available_resources=runtime.available_resources)
            forecast = RuleOutcomeForecaster().forecast(snapshot, option, grounded.alternatives[0],
                                                         contract, state)
            self.assertFalse(forecast.scientific_outcome_known)
            self.assertEqual(forecast.covered_obligation_refs, ("O",))
            self.assertEqual(forecast.branches[0].outcome_class, "INCONCLUSIVE")
            self.assertTrue(all(effect.projected_status == "UNCHANGED"
                                for effect in forecast.branches[0].obligation_effects))

    def test_legacy_direct_evidence_actor_is_not_production_scientific_authority(self):
        from bioma import BioMA
        for outcomes in [('negative',), ('positive',), ('positive','negative')]:
            with self.subTest(outcomes=outcomes):
                with self.assertRaisesRegex(ValueError,'ScientificBackend instances'):
                    BioMA(backends=[CommittedResultAgent(outcomes)], control_policy='adaptive_v3', ledger_path=None)


class LocalResearchSourceClient:
    def search(self, query):
        from bioma.core.scientific_capabilities import Source
        return [Source(id="local-source", identifier="local:authorized-record",
            citation="Authorized local record", spans={"finding": "The measured value is 4."},
            provenance={"repository": "controlled-fixture", "retrieved_by": "search"})]
    def fetch(self, source):
        return source
    def expand_citations(self, source):
        return []
    def synthesize(self, sources, question):
        from bioma.core.scientific_capabilities import ResearchBundle, RetrievedClaim
        return ResearchBundle(sources=sources, retrieved_claims=[RetrievedClaim(
            statement="The measured value is 4.", source_id="local-source", span_ids=["finding"])],
            limitations=["Single authorized local record."],
            provenance={"backend": "LocalResearchSourceClient", "question": question})


def provider_task(task_id, family, method, result_kind, *, inputs=None):
    requirement = EvidenceRequirement(id="O", description="Acquire and qualify the result",
        evidence_kinds=[result_kind], capability=family, method_id=method,
        message_type="task.submitted", mode="evidence", input_bindings=inputs or {})
    return prepare_contract(ScientificTask(id=task_id, objective="Determine the recorded measured value.",
        inputs=inputs or {},
        initial_capability="research.problem_formulation", gateway={
            "acceptance_mode": "work", "selected_entry_capability": family,
            "selected_entry_method": method, "selected_message_type": "task.submitted",
            "evidence_requirements": [requirement.model_dump(mode="json")]}))


class GovernedNeutralProviderTests(unittest.TestCase):
    def test_configured_research_backend_is_qualified_then_admitted_by_independent_rule(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma import BioMA
        from bioma.core.models import Evidence
        from bioma.backends.research import build_research_backend
        from bioma.runtime.result_qualification import WorkResultQualificationService
        from bioma.core.scientific_capabilities import ResearchBundle

        def qualify(work):
            bundle = ResearchBundle.model_validate(work.payload)
            claim = bundle.retrieved_claims[0]
            source = next(row for row in bundle.sources if row.id == claim.source_id)
            if claim.span_ids[0] not in source.spans:
                return None
            return Evidence(kind="ResearchBundle", claim=claim.statement,
                value={"qualified_outcome": "positive", "source_id": source.id,
                       "identifier": source.identifier, "citation": source.citation,
                       "span_ids": claim.span_ids, "limitations": bundle.limitations},
                source="harness")

        with TemporaryDirectory() as root:
            qualifier = WorkResultQualificationService("source-span-qualification-v1", qualify)
            app = BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                control_policy="adaptive_v3", ledger_path=Path(root) / "research.sqlite3",
                result_qualifier=qualifier)
            state = app.run(provider_task("governed-research", "scientific.research",
                                          "deep_research", "ResearchBundle"))
            self.assertEqual(state.status_reason["control_outcome"], "COMPLETED")
            self.assertEqual(state.evidence[0].value["identifier"], "local:authorized-record")
            work = next(row.value for row in state.observations if row.kind == "work_result")
            self.assertEqual(work["payload"]["retrieval_trace"][-1]["query"],
                             "Acquire and qualify the result")
            self.assertNotIn("Execute grounded option step",
                             work["payload"]["retrieval_trace"][-1]["query"])
            events = app.runtime.ledger.events("governed-research")
            kinds = {event.kind for event in events}
            self.assertTrue({"WP1PermitObserved", "ExecutionReceiptObserved",
                             "ScientificCommitObserved",
                             "AuthoritativeScientificStateTransition"} <= kinds)
            record = next(e.payload for e in events if e.kind == "OptionExecutionRecord")
            self.assertTrue(record["result_refs"] and record["evidence_refs"])

    def test_verified_compute_result_is_recomputed_before_evidence_admission(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma import BioMA
        from bioma.core.models import Evidence
        from bioma.backends.base import MethodBinding
        from bioma.backends.catalog import method
        from bioma.backends.compute import compute_backend
        from bioma.core.scientific_capabilities import WorkResult
        from bioma.runtime.result_qualification import WorkResultQualificationService

        def execute(state, message, tools):
            values = message.payload["inputs"]["values"]
            return WorkResult(family="scientific.compute", kind="Calculation",
                payload={"values": values, "sum": sum(values)},
                provenance={"method": "verified_sum", "implementation": "python-sum"})

        def qualify(work):
            values = work.payload["values"]
            if work.payload["sum"] != sum(values):
                return None
            return Evidence(kind="Calculation", claim="The supplied values sum to 10.",
                value={"qualified_outcome": "positive", "sum": sum(values),
                       "verification": "independent-recomputation",
                       "evidence_package": {"input_hash": __import__("bioma.core.research",
                           fromlist=["fingerprint"]).fingerprint({"values": values}),
                           "method_id": "verified_sum", "responsibility": "scientific.compute"}},
                source="harness")

        provider = compute_backend([MethodBinding(
            method("scientific.compute", "verified_sum", "Calculation"), execute)])
        task = provider_task("governed-compute", "scientific.compute", "verified_sum",
                             "Calculation", inputs={"values": [1, 2, 3, 4]})
        with TemporaryDirectory() as root:
            app = BioMA(backends=[provider], control_policy="adaptive_v3",
                ledger_path=Path(root) / "compute.sqlite3",
                result_qualifier=WorkResultQualificationService("sum-recompute-v1", qualify))
            state = app.run(task)
            self.assertEqual(state.status_reason["control_outcome"], "COMPLETED",
                             state.status_reason)
            self.assertEqual(state.evidence[0].value["sum"], 10)
            self.assertEqual(state.evidence[0].value["verification"],
                             "independent-recomputation")


class CounterfactualActionSelectionTests(unittest.TestCase):
    def test_same_question_stops_after_qualified_evidence_instead_of_repeating_action(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma.core.models import Evidence
        with TemporaryDirectory() as root:
            unresolved, contract, plan, runtime, first = fixture(Path(root))
            proposed = EvidenceDrivenOptionGenerator().generate(first, contract, plan, unresolved,
                                                                 runtime.capability_registry.all())
            resolved = unresolved.model_copy(update={"evidence": [Evidence(id="qualified-result",
                kind="result", claim="Qualified answer", value={"qualified_outcome": "positive"},
                source="harness", method="independent-rule")], "version": 1})
            second = decision_snapshot(contract=contract, plan=plan, state=resolved,
                registry=runtime.capability_registry, active_agents=set(runtime.agents),
                available_resources=runtime.available_resources, remaining_budget=2,
                timestamp=datetime(2026, 10, 10, tzinfo=timezone.utc))
            after = EvidenceDrivenOptionGenerator().generate(second, contract, plan, resolved,
                                                              runtime.capability_registry.all())
            self.assertTrue(proposed)
            self.assertEqual(second.progress_value, 1.0)
            self.assertEqual(after, ())

class CoarseQueryProductionApiTests(unittest.TestCase):
    def test_query_does_not_turn_a_research_provider_into_an_implicit_planner(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma import BioMA
        from bioma.gateway import QueryInterpretationError
        from bioma.backends.research import build_research_backend
        with TemporaryDirectory() as root:
            app=BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                ledger_path=Path(root)/'query.db')
            with self.assertRaisesRegex(QueryInterpretationError,'execution service is unavailable'):
                app.query('Use a closed loop literature review to choose the next scientific action.')


class SemanticOptionGenerationTests(unittest.TestCase):
    def test_model_selects_only_catalog_method_and_preserves_subquestion(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma.scientific_control.generation import LLMEvidenceDrivenOptionGenerator

        class Provider:
            def chat(self, messages, *, json_mode=False):
                self.messages = messages
                return json.dumps({"choices": [{
                    "node_ref": "not-ready", "obligation_refs": ["O"],
                    "capability_id": "adaptive.result", "method_id": "alternative",
                    "scientific_question": "Premature action", "rationale": "Must reject",
                    "intent_class": "DIRECT_PROGRESS"
                }, {
                    "node_ref": "O", "obligation_refs": ["O"],
                    "capability_id": "adaptive.result", "method_id": "alternative",
                    "scientific_question": "Which independent observation resolves the result?",
                    "rationale": "Use an independently registered method for the unresolved obligation.",
                    "intent_class": "EVIDENCE_ACQUISITION",
                }]})

        with TemporaryDirectory() as root:
            state, contract, plan, runtime, snapshot = fixture(Path(root))
            state = state.model_copy(update={'status_reason': {
                'control_execution_feedback':[{'lifecycle':'BLOCKED','reason':'accepted result absent'}]}})
            provider = Provider()
            options = LLMEvidenceDrivenOptionGenerator(provider, model_id="semantic-test").generate(
                snapshot, contract, plan, state, runtime.capability_registry.all())
            self.assertEqual(len(options), 1)
            self.assertEqual(options[0].steps[0].method_hint, "alternative")
            self.assertEqual(options[0].steps[0].description,
                             "Which independent observation resolves the result?")
            prompt = json.loads(provider.messages[1]["content"])
            self.assertEqual(prompt["method_catalog"][0]["method_id"], "alternative")
            self.assertEqual(prompt["scientific_state_version"], state.version)
            self.assertEqual(prompt["qualified_evidence_content"], [e.model_dump(mode="json") for e in state.evidence])
            self.assertEqual(prompt["observed_work_results"], [o.model_dump(mode="json") for o in state.observations if o.kind=="work_result"])
            self.assertEqual(prompt["execution_feedback"], state.status_reason)
            self.assertNotIn("implementation_identity", provider.messages[0]["content"])

    def test_model_cannot_invent_a_method(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma.scientific_control.generation import LLMEvidenceDrivenOptionGenerator

        class Provider:
            def chat(self, messages, *, json_mode=False):
                return json.dumps({"choices": [{
                    "node_ref": "O", "obligation_refs": ["O"],
                    "capability_id": "adaptive.result", "method_id": "invented",
                    "scientific_question": "Resolve the result.",
                    "rationale": "Invented method must be rejected.",
                    "intent_class": "DIRECT_PROGRESS",
                }]})

        with TemporaryDirectory() as root:
            state, contract, plan, runtime, snapshot = fixture(Path(root))
            generator = LLMEvidenceDrivenOptionGenerator(Provider(), model_id="semantic-test")
            with self.assertRaisesRegex(ValueError, "outside the qualified catalog"):
                generator.generate(snapshot, contract, plan, state,
                                   runtime.capability_registry.all())


class UnproductiveMethodRecoveryTests(unittest.TestCase):
    def test_qualification_rejection_preserves_feedback_without_scientific_credit(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma import BioMA
        from bioma.backends.research import build_research_backend
        from bioma.runtime.result_qualification import WorkResultQualificationService
        from bioma.runtime.scientific_control import ControlBudget

        with TemporaryDirectory() as root:
            app = BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                control_policy="adaptive_v3", ledger_path=Path(root) / "rejection.sqlite3",
                result_qualifier=WorkResultQualificationService("reject-all-v1", lambda work: None),
                control_budget=ControlBudget(max_control_cycles=3))
            state = app.run(provider_task("qualification-rejection", "scientific.research",
                                          "deep_research", "ResearchBundle"))
            events = app.runtime.ledger.events("qualification-rejection")
            outcomes = [row.payload for row in events
                        if row.kind == "WorkResultQualificationOutcome"]
            self.assertEqual(len(outcomes), 3)
            feedback = state.status_reason['control_execution_feedback']
            self.assertEqual(len(feedback), 3)
            self.assertTrue(all(row['execution_ref'] and row['bindings'] for row in feedback))
            self.assertTrue(all(row["status"] == "REJECTED" for row in outcomes))
            completed = state.status_reason['control_completed_actions']
            self.assertEqual(len(completed), 3)
            # Method identity alone is not action identity: another execution
            # can contribute new observed inputs without qualified Evidence.
            self.assertEqual(len({r['execution_ref'] for r in completed.values()}), 3)
            self.assertFalse(state.evidence)
            plan_revisions = [row.payload for row in events if row.kind == "PlanRevision"]
            self.assertTrue(all(row.get("retained") for row in plan_revisions))
            self.assertTrue(all(row["plan_identity"][1] == 1 for row in plan_revisions))


class RevisionBudgetRegressionTests(unittest.TestCase):
    def test_revision_budget_refusal_preserves_execution_and_terminates(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma import BioMA
        from bioma.llm import ModelBudgetUnavailable
        from bioma.backends.research import build_research_backend
        from bioma.runtime.result_qualification import WorkResultQualificationService
        from bioma.runtime.scientific_control import ControlBudget

        class RefusedRevision:
            def propose(self, *args):
                raise ModelBudgetUnavailable("test request allowance exhausted")

        with TemporaryDirectory() as root:
            app = BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                control_policy="adaptive_v3", ledger_path=Path(root)/"ledger.sqlite3",
                result_qualifier=WorkResultQualificationService("reject-test", lambda work: None),
                control_budget=ControlBudget(max_control_cycles=3))
            app.scientific_control_runtime.revision_policy = RefusedRevision()
            task = provider_task("revision-refused", "scientific.research", "deep_research", "ResearchBundle")
            state = app.run(task)
            self.assertEqual(state.status_reason["control_outcome"], "BUDGET_EXHAUSTED")
            events = app.runtime.ledger.events(task.id)
            kinds = [e.kind for e in events]
            self.assertIn("OptionExecutionRecord", kinds)
            self.assertIn("PlanRevisionDeferred", kinds)
            self.assertIn("ScientificControlTerminated", kinds)
            self.assertNotIn("PlanRevision", kinds)
            self.assertEqual(app.run(task).model_dump(), state.model_dump())

    def test_execution_budget_refusal_is_not_scientific_failure(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from bioma import BioMA
        from bioma.llm import ModelBudgetUnavailable
        from bioma.backends.research import build_research_backend
        with TemporaryDirectory() as root:
            app = BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                control_policy="adaptive_v3", ledger_path=Path(root)/"ledger.sqlite3")
            task = provider_task("execution-refused", "scientific.research", "deep_research", "ResearchBundle")
            with patch('bioma.runtime.scientific_control.OptionExecutor.execute',
                       side_effect=ModelBudgetUnavailable('tools ceiling reached')):
                state = app.run(task)
            self.assertEqual(state.status, 'blocked')
            self.assertEqual(state.status_reason['control_outcome'], 'BUDGET_EXHAUSTED')
            events = app.runtime.ledger.events(task.id)
            deferred = next(e.payload for e in events if e.kind == 'OptionExecutionDeferred')
            self.assertTrue(deferred['child_task_refs'])
            self.assertFalse(state.evidence)
            self.assertEqual(app.run(task).model_dump(), state.model_dump())

    def test_blocked_result_feedback_is_preserved_without_scientific_credit(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from bioma import BioMA
        from bioma.backends.research import build_research_backend
        from bioma.runtime.scientific_control import ControlBudget
        from bioma.scientific_control.executor import OptionLifecycle
        with TemporaryDirectory() as root:
            app = BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                control_policy="adaptive_v3", ledger_path=Path(root)/"ledger.sqlite3",
                control_budget=ControlBudget(max_control_cycles=2, max_revision_attempts=0))
            task = provider_task("blocked-feedback", "scientific.research", "deep_research", "ResearchBundle")
            def blocked(*args, record, **kwargs):
                return record.model_copy(update={'lifecycle':OptionLifecycle.BLOCKED,
                    'status_reason':'accepted result classes absent'})
            with patch('bioma.runtime.scientific_control.OptionExecutor.execute', side_effect=blocked):
                state = app.run(task)
            feedback = state.status_reason['control_execution_feedback']
            self.assertEqual(len(feedback), 2)
            self.assertTrue(all(r['lifecycle']=='BLOCKED' and r['reason']=='accepted result classes absent' for r in feedback))
            self.assertFalse(state.evidence)
            self.assertEqual(state.version, 0)

    def test_noop_consumes_attempt_and_disabled_policy_is_not_called(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from bioma import BioMA
        from bioma.backends.research import build_research_backend
        from bioma.runtime.result_qualification import WorkResultQualificationService
        from bioma.runtime.scientific_control import ControlBudget

        class NoopRevision:
            calls = 0
            def propose(self, *args):
                self.calls += 1
                return None

        for maximum in (0, 1):
            with self.subTest(maximum=maximum), TemporaryDirectory() as root:
                app = BioMA(backends=[build_research_backend(LocalResearchSourceClient())],
                    control_policy="adaptive_v3", ledger_path=Path(root)/"ledger.sqlite3",
                    result_qualifier=WorkResultQualificationService("reject-test", lambda work: None),
                    control_budget=ControlBudget(max_control_cycles=3, max_revision_attempts=maximum))
                policy = NoopRevision()
                app.scientific_control_runtime.revision_policy = policy
                task = provider_task("revision-noop", "scientific.research", "deep_research", "ResearchBundle")
                app.run(task)
                rows = [e.payload for e in app.runtime.ledger.events(task.id) if e.kind == "PlanRevision"]
                self.assertEqual(policy.calls, maximum)
                self.assertEqual(sum(r["policy_called"] for r in rows), maximum)
                self.assertTrue(all(r["retained"] for r in rows))

    def test_scope_schema_matches_runtime_validation(self):
        from jsonschema import Draft202012Validator
        from pydantic import ValidationError
        from bioma.scientific_control.goal_planning import Question, _SCOPE_LEVELS
        validator = Draft202012Validator(Question.model_json_schema())
        for scope in ["unspecified", *_SCOPE_LEVELS]:
            for level in ("computed", "predicted", "retrieved", "observed"):
                value = dict(id="q", question="Check", scientific_object="object", scope="scope",
                    evidence_level=level, evidence_obligation="support", verification_scope=scope)
                try:
                    Question.model_validate(value)
                    accepted = True
                except ValidationError:
                    accepted = False
                self.assertEqual(validator.is_valid(value), accepted, (scope, level))


if __name__ == "__main__":
    unittest.main()
