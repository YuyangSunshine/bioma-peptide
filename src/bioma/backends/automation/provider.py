"""Request-scoped protocol model adapter."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from contextlib import contextmanager
from contextvars import ContextVar

from bioma.config import load_config
from bioma.llm import ChatProvider, provider_for

_provider = ContextVar('automation_provider', default=None)


@contextmanager
def use_provider(provider):
    token = _provider.set(provider)
    try:
        yield
    finally:
        _provider.reset(token)


@dataclass(frozen=True)
class Message:
    content: str


class ConfiguredProtocolModel:
    @cached_property
    def provider(self) -> ChatProvider:
        config = load_config().operagent.llm
        return provider_for(config)

    def invoke(self, prompt: str) -> Message:
        return Message(
            (_provider.get() or self.provider).chat([{"role": "user", "content": prompt}])
        )


protocol_model = ConfiguredProtocolModel()


def invoke(prompt: str) -> Message:
    """Call the request-scoped model, or the configured protocol model."""
    return protocol_model.invoke(prompt)
