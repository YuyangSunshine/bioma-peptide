import hashlib
import json
from pathlib import Path

from pydantic import Field

from bioma.capabilities.scoped import ScopedSkillResolver
from bioma.core.methods import ExecutionMethod, ResultContract
from bioma.core.models import ScientificState
from bioma.core.research_contract import AcceptanceObligation, ResearchContract
from bioma.core.scientific_capabilities import WorkResult
from bioma.backends.base import MethodBinding
from bioma.backends.compute import compute_backend
from bioma.backends.research import build_research_backend
from bioma.runtime.engine import DecentralizedRuntime
from bioma.scientific_control import (NodeKind, OptionExecutor, OptionLifecycle, OptionStep,
    PlanNode, ResearchPlan, ScientificOption, ground_option, new_execution_record)
from bioma.skills.registry import SkillRegistry
from bioma.tools.base import Tool, ToolInput
from bioma.tools.registry import ToolRegistry


class QueryInput(ToolInput):
    query: str = Field(min_length=1)


class CountInput(ToolInput):
    text: str = Field(min_length=1)


def _source_valid(result, args):
    return result == {"query": args["query"], "text": "ACDEFG"}


def _count_valid(result, args):
    return result == len(args["text"])


def _method(family, method_id, result_kind):
    return ExecutionMethod(id=method_id, description=method_id,
        message_type="task.submitted", result=ResultContract(responsibility=family,
        result_channel="work_result", evidence_kinds=[result_kind],
        verification_obligations=["typed result", "input identity"]))


def _fixture(tmp_path):
    source_tool = Tool(name="lookup_sequence", description="Retrieve a public sequence",
        category="research", version="1", author="test", func=lambda query: {"query": query, "text": "ACDEFG"},
        args_schema=QueryInput, result_kind="source", result_validator=_source_valid,
        replay_safe=True)
    count_tool = Tool(name="count_residues", description="Count sequence residues",
        category="analysis", version="1", author="test", func=lambda text: len(text),
        args_schema=CountInput, result_kind="calculation", result_validator=_count_valid)
    registry = ToolRegistry([])
    registry.add(source_tool); registry.add(count_tool)
    skill_path = tmp_path / "skills" / "sequence-runtime"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("""---
name: sequence-runtime
description: Retrieve and calculate over a scientific sequence.
metadata: {domain: bioinformatics, status: active}
routing: {tags: [sequence, retrieval, calculation]}
tools: [lookup_sequence, count_residues]
---
Use only tools granted to the selected method.
""", encoding="utf-8")
    skills = SkillRegistry([tmp_path / "skills"]); skills.scan()
    resolver = ScopedSkillResolver(skills, registry)
    research_method = _method("scientific.research", "source_lookup", "SourceSet")
    compute_method = _method("scientific.compute", "source_length", "Calculation")

    def research_execute(state, message, tools):
        result = tools[0].call(query=message.payload["inputs"]["query"])
        return WorkResult(family="scientific.research", kind="SourceSet",
            payload=result, provenance={"tool": tools[0].name})

    def compute_execute(state, message, tools):
        source = message.payload["inputs"]["source_inputs"][0]
        upstream = WorkResult.model_validate(source["value"])
        value = tools[0].call(text=upstream.payload["text"])
        return WorkResult(family="scientific.compute", kind="Calculation",
            payload={"length": value}, input_refs=[source["id"]],
            provenance={"tool": tools[0].name, "upstream": source["id"]})

    research = build_research_backend(additional_backends=(MethodBinding(
        research_method, research_execute, (source_tool,)),), scoped_skill_resolver=resolver)
    compute = compute_backend((MethodBinding(
        compute_method, compute_execute, (count_tool,)),), scoped_skill_resolver=resolver)
    runtime = DecentralizedRuntime([research, compute], ledger_path=tmp_path / "controller.sqlite3")
    return runtime, registry, source_tool, count_tool, research_method, compute_method


def _models(runtime, research_method, compute_method):
    contract = ResearchContract(query="Retrieve a sequence and calculate its length", input_hash="input:1",
        requirements=(AcceptanceObligation(id="O", description="A calculated result",
                                            evidence_kinds=("Calculation",)),))
    plan = ResearchPlan(plan_id="PLAN", version=3, contract_ref=contract.identity,
        evidence_snapshot_ref="evidence:0", nodes=(PlanNode(node_id="N", kind=NodeKind.WORK_ITEM,
        description="Source then compute", contract_obligation_refs=("O",)),))
    option = ScientificOption(option_id="SOURCE-COMPUTE", contract_ref=contract.identity,
        contract_version=contract.version, plan_ref=plan.plan_id, plan_version=plan.version,
        plan_identity_hash=plan.version_identity[2], target_plan_node_refs=("N",),
        target_contract_obligation_refs=("O",), scientific_intent="Use retrieved source in calculation",
        expected_result_classes=("Calculation",), termination_condition="accepted result exists",
        proposer_id="controller", provenance_refs=(plan.version_identity[2],), steps=(
            OptionStep(step_id="SOURCE", description="Retrieve sequence", capability="scientific.research",
                message_type="task.submitted", input_bindings_json=json.dumps({"query": "example protein"}),
                expected_result_classes=("SourceSet",), method_hint=research_method.id),
            OptionStep(step_id="COMPUTE", description="Calculate source length", capability="scientific.compute",
                message_type="task.submitted", required_input_classes=("SourceSet",),
                expected_result_classes=("Calculation",), depends_on_step_ids=("SOURCE",),
                method_hint=compute_method.id)))
    grounded_result = ground_option(option, contract=contract, plan=plan,
        state=ScientificState(task_id="ground", objective=contract.query),
        registry=runtime.capability_registry,
        assessors={name: agent.assess for name, agent in runtime.agents.items()},
        active_agents=set(runtime.agents), available_resources=runtime.available_resources)
    assert grounded_result.status == "GROUNDED"
    grounded = grounded_result.alternatives[0]
    record = new_execution_record(option, grounded, execution_instance_id="SCOPED-INTEGRATION")
    return contract, plan, option, grounded, record


def _task_id(step_id):
    return "option." + hashlib.sha256(f"SCOPED-INTEGRATION:{step_id}".encode()).hexdigest()


def test_controller_binds_grant_to_action_contract_plan_method_and_wp1(tmp_path):
    runtime, _, _, _, research_method, compute_method = _fixture(tmp_path)
    contract, plan, option, grounded, record = _models(runtime, research_method, compute_method)
    completed = OptionExecutor(runtime).execute(option, grounded, contract=contract, plan=plan, record=record)
    assert completed.lifecycle == OptionLifecycle.COMPLETED
    assert set(completed.accepted_result_classes) == {"Calculation", "SourceSet"}
    source_result = runtime.ledger.replay_state(_task_id("SOURCE")).observations[0]
    compute_state = runtime.ledger.replay_state(_task_id("COMPUTE"))
    computed = WorkResult.model_validate(compute_state.observations[0].value)
    assert computed.input_refs == [source_result.id]
    assert computed.payload == {"length": 6}
    for step in grounded.steps:
        events = runtime.ledger.events(_task_id(step.step_id))
        observed = next(event.payload for event in events if event.kind == "ToolActionObserved")
        permitted = next(event.payload for event in events if event.kind == "ExecutionPermitted")
        context = observed["tool_grant_context"]
        assert observed["tool_grant_ref"].startswith("tool-grant:")
        assert context["parent_scientific_action_ref"].startswith("scientific-action:")
        assert context["contract_ref"] == contract.identity
        assert context["contract_version"] == contract.version
        assert context["plan_ref"] == plan.plan_id
        assert context["plan_version"] == plan.version
        assert context["provider_id"] == step.provider_id
        assert context["method_id"] == step.method_id
        assert context["wp1_attempt_ref"] == f"attempt:{permitted['proposal_id']}:{permitted['attempt']}"
        assert context["wp1_mandate_ref"] == f"mandate:{permitted['proposal_id']}"


def test_stale_plan_blocks_before_any_tool_call(tmp_path):
    runtime, _, _, _, research_method, compute_method = _fixture(tmp_path)
    contract, plan, option, grounded, record = _models(runtime, research_method, compute_method)
    stale_plan = plan.model_copy(update={"version": plan.version + 1})
    result = OptionExecutor(runtime).execute(option, grounded, contract=contract,
                                             plan=stale_plan, record=record)
    assert result.lifecycle == OptionLifecycle.BLOCKED
    assert all(not runtime.ledger.events(_task_id(step.step_id)) for step in grounded.steps)


def test_revoked_tool_after_grounding_fails_closed_and_requires_regrounding(tmp_path):
    runtime, registry, source_tool, _, research_method, compute_method = _fixture(tmp_path)
    contract, plan, option, grounded, record = _models(runtime, research_method, compute_method)
    registry.remove(source_tool.name)
    result = OptionExecutor(runtime).execute(option, grounded, contract=contract, plan=plan, record=record)
    assert result.lifecycle != OptionLifecycle.COMPLETED
    events = runtime.ledger.events(_task_id("SOURCE"))
    assert not any(event.kind == "ToolActionObserved" for event in events)
    assert "stale" in result.status_reason.lower() or "replaced" in result.status_reason.lower()
