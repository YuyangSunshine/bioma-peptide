"""Shared scientific analysis execution harness; legacy roles are callers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bioma.harness.budget import HarnessBudget, HarnessBudgetConfig
from bioma.harness.loop import DataAnalysisLoop
from bioma.harness.tools import build_default_tools
from bioma.harness.types import HarnessContext
from bioma.backends.analysis.services.llm_json_service import use_llm_config
from bioma.config import ScientificInvestigationConfig


class DataAnalysisRunner:
    """Reusable code execution, inspection, recovery and delivery primitives."""

    def __init__(self, config: ScientificInvestigationConfig, *, base_dir: Path, tool_registry=None, provider_factory=None) -> None:
        self.config = config
        self.base_dir = base_dir.resolve()
        self.tool_registry = tool_registry
        if config.agent_engine == "mini_swe":
            from bioma.harness.mini_swe_engine import MiniSWEInvestigationLoop
            self.loop = MiniSWEInvestigationLoop(config, provider_factory=provider_factory)
        else:
            self.loop = DataAnalysisLoop(config, provider_factory=provider_factory)
        self.tools = build_default_tools()
        from bioma.harness.shared_tools import shared_tools, discovery_tools
        existing = {tool.name for tool in self.tools}
        self.external_tools = [tool for tool in shared_tools(tool_registry) if tool.name not in existing]
        self.tools.extend(discovery_tools(tool_registry, self.external_tools))

    def tools_for_state(self, state: dict[str, Any]) -> list:
        required_tools = set((state.get('execution_requirements') or {}).get('required_tools') or [])
        tools = list(self.tools)
        tools += [tool for tool in self.external_tools
                  if tool.name in required_tools and tool.name not in {item.name for item in tools}]
        return tools

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        context = HarnessContext(state=dict(state), base_dir=self.base_dir)
        context.state.setdefault("artifact_root", str(self.base_dir / "artifacts"))
        query = str(context.state.get("current_user_request") or "")
        tools = self.tools_for_state(state)
        budget = HarnessBudget(
            HarnessBudgetConfig(
                max_llm_calls=self.config.max_llm_calls,
                max_tool_calls=self.config.max_tool_calls,
            )
        )
        with use_llm_config(self.config.llm):
            loop_result = self.loop.run(query=query, context=context, tools=tools, budget=budget)
        telemetry = budget.telemetry.flush()

        messages = list(context.state.get("messages") or [])
        if loop_result.answer:
            messages.append({"role": "assistant", "content": loop_result.answer})

        status = "completed"
        error_code = context.state.get("error_code")
        error_detail = context.state.get("error_detail")
        if context.state.get("need_clarification"):
            status = "waiting_user"
        elif loop_result.exit_reason == 'paused':
            status = 'paused'
        elif error_code:
            status = "failed"
        elif (loop_result.exit_reason != "completed" or
              (not context.state.get("delivery_complete") if context.state.get("continuous_investigation")
               else not context.state.get("analysis_results"))):
            status = "failed"
            error_code = "INCOMPLETE_TABULAR_ANALYSIS"
            error_detail = f"Harness stopped with {loop_result.exit_reason}."

        context.merge_patch(
            {
                "messages": messages,
                "harness_exit_reason": loop_result.exit_reason,
                "harness_telemetry": telemetry,
                "harness_tool_names": [tool.name for tool in tools],
                "status": status,
                "error_code": error_code,
                "error_detail": error_detail,
            }
        )
        required = (context.state.get('execution_requirements') or {}).get('required_tools') or []
        called = {item['tool'] for item in context.state.get('tool_receipts') or [] if item.get('status') == 'ok'}
        builtin_names = {tool.name for tool in self.tools} - {'find_scientific_tools', 'invoke_scientific_tool'}
        called.update(item['tool_name'] for item in context.state.get('harness_observations') or []
                      if item.get('status') == 'ok' and item.get('tool_name') in builtin_names)
        if status == 'completed' and not set(required) <= called:
            context.merge_patch({'status': 'failed', 'error_code': 'MISSING_REQUIRED_TOOL_RECEIPT',
                'error_detail': 'Required tool executions were not verified: ' + ', '.join(sorted(set(required) - called))})
        return context.state


__all__ = ["DataAnalysisRunner"]
