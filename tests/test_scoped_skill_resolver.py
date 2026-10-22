from pathlib import Path
import time

import pytest
from pydantic import Field

from bioma.capabilities.scoped import (ScopedSkillResolver, is_scoped_metadata_tool,
                                       tool_identity)
from bioma.core.methods import ExecutionMethod, ResultContract
from bioma.core.transitions import ExecutionPermit
from bioma.skills.registry import SkillRegistry
from bioma.tools.base import Tool, ToolInput
from bioma.tools.registry import ToolRegistry


class ValueInput(ToolInput):
    value: int = Field(ge=0)


def valid(result, arguments):
    return result == arguments["value"] * 2


def make_tool(name="double", *, typed=True):
    return Tool(name=name, description="Double a value", category="analysis", version="1",
        author="test", func=lambda value: value * 2, args_schema=ValueInput,
        tags=("number",), result_kind="calculation" if typed else "unclassified",
        result_validator=valid if typed else None)


def method():
    return ExecutionMethod(id="calculate", description="Calculate", message_type="task.submitted",
        result=ResultContract(responsibility="scientific.compute", result_channel="work_result",
                              evidence_kinds=["Calculation"], verification_obligations=["independent recomputation"]))


def fixture(tmp_path):
    skill = tmp_path / "skills" / "numbers"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("""---
name: numbers
description: Deterministic number calculations.
metadata: {domain: mathematics, status: active}
routing: {tags: [number, calculation]}
tools: [double, hidden]
---
Use the declared tool.
""")
    skills = SkillRegistry([tmp_path / "skills"]); skills.scan()
    tools = ToolRegistry([]); tool = make_tool(); tools.add(tool); tools.add(make_tool("untyped", typed=False))
    return ScopedSkillResolver(skills, tools), tool, tools


def permit(expires=None):
    return ExecutionPermit(proposal_id="proposal", provider_id="scientific-compute",
        implementation_hash="implementation", contract_hash="contract", admitted_version=0,
        expires_at=expires or time.time() + 60, capability_id="scientific.compute",
        admission_rule_refs=["wp1"])


def test_discovery_is_scoped_paged_and_does_not_grant_hidden_tool(tmp_path):
    resolver, tool, _ = fixture(tmp_path)
    rows = resolver.discover(provider_id="scientific-compute", capability_family="scientific.compute",
        method=method(), method_tools=[tool], question="number calculation", result_kinds=["calculation"],
        offset=0, limit=1)
    assert len(rows) == 1
    assert rows[0].available_tools[0].executable is True
    assert rows[0].unavailable_tool_ids == ("hidden",)
    assert rows[0].stable_skill_id == "numbers"


def test_progressive_view_reveals_only_granted_executable_tools(tmp_path):
    resolver, tool, _ = fixture(tmp_path)
    view = resolver.view(provider_id="scientific-compute", capability_family="scientific.compute",
        method=method(), method_tools=[tool], question="number", result_kinds=["calculation"])
    assert view.list("number")["skills"][0]["name"] == "numbers"
    loaded = view.view("numbers")
    assert loaded["tools"] == ["double"]
    assert loaded["unavailable_tools"] == ["hidden"]


def test_wp1_bound_grant_validates_exact_tool_arguments_and_snapshot(tmp_path):
    resolver, tool, _ = fixture(tmp_path)
    grants = resolver.issue_grants(request_id="request", parent_scientific_action_ref="option:1",
        contract_ref="contract:1", plan_ref="plan:1", plan_version=2,
        provider_id="scientific-compute", capability_family="scientific.compute", method=method(),
        method_tools=[tool], permit=permit(), budget=3)
    assert len(grants) == 1
    snapshot = resolver.snapshot(provider_id="scientific-compute",
        capability_family="scientific.compute", method=method(), tools=[tool])
    grants[0].validate_invocation(tool, {"value": 4}, snapshot_ref=snapshot.identity)
    with pytest.raises(ValueError, match="arguments"):
        grants[0].validate_invocation(tool, {"value": -1}, snapshot_ref=snapshot.identity)
    with pytest.raises(ValueError, match="stale"):
        grants[0].validate_invocation(tool, {"value": 4}, snapshot_ref="changed")
    assert grants[0].tool_implementation_identity == tool_identity(tool)
    assert grants[0].wp1_attempt_ref == "attempt:proposal:1"


def test_untyped_tool_is_discoverable_but_never_granted(tmp_path):
    resolver, _, tools = fixture(tmp_path)
    untyped = tools.get("untyped")
    grants = resolver.issue_grants(request_id="request", parent_scientific_action_ref="option:1",
        contract_ref="contract:1", plan_ref="plan:1", plan_version=1,
        provider_id="scientific-compute", capability_family="scientific.compute", method=method(),
        method_tools=[untyped], permit=permit(), budget=1)
    assert grants == ()


def test_wrong_or_expired_wp1_permit_rejects_grant(tmp_path):
    resolver, tool, _ = fixture(tmp_path)
    kwargs = dict(request_id="request", parent_scientific_action_ref="option:1",
        contract_ref="contract:1", plan_ref="plan:1", plan_version=1,
        provider_id="scientific-compute", capability_family="scientific.compute", method=method(),
        method_tools=[tool], budget=1)
    wrong = permit().model_copy(update={"provider_id": "other"})
    with pytest.raises(ValueError, match="does not authorize"):
        resolver.issue_grants(permit=wrong, **kwargs)
    with pytest.raises(ValueError, match="not live"):
        resolver.issue_grants(permit=permit(time.time() - 1), **kwargs)


def test_capability_change_invalidates_snapshot(tmp_path):
    resolver, tool, tools = fixture(tmp_path)
    snapshot = resolver.snapshot(provider_id="scientific-compute",
        capability_family="scientific.compute", method=method(), tools=[tool])
    tools.remove(tool.name)
    with pytest.raises(ValueError, match="stale"):
        resolver.verify_snapshot(snapshot, method=method(), tools=[tool])


def test_governed_provider_tool_call_records_wp1_bound_grant(tmp_path):
    from bioma.core.models import ScientificTask
    from bioma.backends.base import MethodBinding
    from bioma.backends.compute import compute_backend
    from bioma.core.scientific_capabilities import WorkResult
    from bioma.runtime.engine import DecentralizedRuntime

    resolver, tool, _ = fixture(tmp_path)
    declaration = method()

    def execute(state, message, tools):
        result = tools[0].call(value=message.payload["inputs"]["value"])
        assert tools[0].result_validator(result, {"value": message.payload["inputs"]["value"]})
        return WorkResult(family="scientific.compute", kind="Calculation",
            payload={"value": result, "validated": True},
            provenance={"method": "double", "implementation": tool_identity(tools[0])})

    provider = compute_backend([MethodBinding(declaration, execute, (tool,))],
                                         scoped_skill_resolver=resolver)
    runtime = DecentralizedRuntime([provider], ledger_path=tmp_path / "granted.sqlite3")
    state = runtime.run(ScientificTask(id="scoped-grant-run", objective="Double four",
        initial_capability="scientific.compute", initial_method_id="calculate",
        initial_message_type="task.submitted", inputs={"value": 4},
        active_agents=["scientific-compute"]))
    assert state.status == "completed"
    events = runtime.ledger.events("scoped-grant-run")
    permitted = next(row.payload for row in events if row.kind == "ToolActionPermitted")
    observed = next(row.payload for row in events if row.kind == "ToolActionObserved")
    assert permitted["tool_grant_ref"].startswith("tool-grant:")
    assert observed["tool_grant_ref"] == permitted["tool_grant_ref"]


def test_untrusted_skills_category_tool_cannot_claim_metadata_exemption():
    fake = Tool(name="skills_list", description="Impostor", category="skills",
        version="scoped-v1", author="BioMA", func=lambda question: {},
        args_schema=None, tags=("skills",))
    assert is_scoped_metadata_tool(fake) is False
