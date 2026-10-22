from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from bioma.llm import ChatProvider, parse_json_object


from bioma.core.protocols import VerificationReport


class AutomationCompiler(ABC):
    @abstractmethod
    def align(self, protocol_text: str, objective: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        aligned_protocol: str,
        objective: str,
        protocol_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def validate(self, machine_code: dict[str, Any]) -> VerificationReport:
        raise NotImplementedError

    @abstractmethod
    def rectify(
        self,
        machine_code: dict[str, Any],
        issues: tuple[str, ...],
        task_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class ConfiguredAutomationCompiler(AutomationCompiler):
    """Automation service backed by configured device registry, K_p validation and repair tools."""

    def __init__(
        self, tools: Any = None, *, provider: ChatProvider | None = None
    ) -> None:
        self._tools = tools
        self.provider = provider

    def _invoke(self, name, values):
        from bioma.backends.automation.provider import use_provider

        with use_provider(self.provider):
            return getattr(self.tools, name).invoke(values)

    @property
    def tools(self) -> Any:
        if self._tools is None:
            from bioma.backends.automation.tools import tool_definitions

            self._tools = tool_definitions
        return self._tools

    def align(self, protocol_text: str, objective: str) -> str:
        return str(
            self._invoke(
                "align_draft_to_automation",
                {"draft": protocol_text, "exp_info": objective},
            )
        )

    def generate(
        self,
        aligned_protocol: str,
        objective: str,
        protocol_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        result = self._invoke(
            "generate_machine_code",
            {
                "aligned_protocol": aligned_protocol,
                "exp_info": objective,
                "session_id": task_id,
            },
        )
        code = _machine_code_object(result)
        code["protocolId"] = protocol_id
        return code

    def validate(self, machine_code: dict[str, Any]) -> VerificationReport:
        result = self._invoke(
            "validate_machine_code",
            {"exp_flow_json": json.dumps(machine_code, ensure_ascii=False)},
        )
        issues = _tool_issues(result)
        return VerificationReport(
            result.get("status") == "success" and not issues, issues
        )

    def rectify(
        self,
        machine_code: dict[str, Any],
        issues: tuple[str, ...],
        task_id: str,
    ) -> dict[str, Any]:
        result = self._invoke(
            "fix_machine_code",
            {
                "machine_code": json.dumps(machine_code, ensure_ascii=False),
                "errors": json.dumps(issues, ensure_ascii=False),
                "session_id": task_id,
            },
        )
        return _machine_code_object(result)


def _machine_code_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return parse_json_object(str(value))


def _tool_issues(result: dict[str, Any]) -> tuple[str, ...]:
    issues: list[str] = []
    for item in [*(result.get("errors") or []), *(result.get("rule_violations") or [])]:
        issues.append(str(item.get("message") if isinstance(item, dict) else item))
    return tuple(issue for issue in issues if issue)
