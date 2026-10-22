from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessBudgetConfig:
    max_llm_calls: int
    max_tool_calls: int


@dataclass
class HarnessTelemetry:
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def flush(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class HarnessBudget:
    def __init__(self, config: HarnessBudgetConfig) -> None:
        self.config = config
        self.telemetry = HarnessTelemetry()

    @property
    def remaining_llm_calls(self) -> int:
        local = self.config.max_llm_calls - self.telemetry.llm_calls
        from bioma.llm import remaining_dispatches
        shared = remaining_dispatches()
        return min(local, shared['requests']) if shared is not None else local

    @property
    def remaining_tool_calls(self) -> int:
        from bioma.llm import remaining_dispatches
        local = max(0, self.config.max_tool_calls - self.telemetry.tool_calls)
        shared = remaining_dispatches()
        return min(local, shared['tools']) if shared is not None else local

    def record_llm_call(
        self, *, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        self.telemetry.llm_calls += 1
        self.telemetry.prompt_tokens += prompt_tokens
        self.telemetry.completion_tokens += completion_tokens

    def record_tool_call(self) -> None:
        self.telemetry.tool_calls += 1
