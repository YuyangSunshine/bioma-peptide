from __future__ import annotations

import inspect
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from bioma.config import LLMConfig
from bioma.llm import provider_for


DEFAULT_BAIDU_BASE_URL = "https://qianfan.baidubce.com/v2"
DEFAULT_MODEL = "deepseek-v4-flash"
_ACTIVE_LLM_CONFIG: ContextVar[LLMConfig | None] = ContextVar(
    "bioma_comagent_llm_config",
    default=None,
)


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class LLMResponse:
    content: str


class ToolInvoker:
    def __init__(self, func: Callable[..., Any]) -> None:
        self.func = func
        self.__name__ = getattr(func, "__name__", "tool")
        self.__doc__ = getattr(func, "__doc__", None)
        self._signature = inspect.signature(func)

    def invoke(self, values: dict[str, Any]) -> Any:
        if isinstance(values, dict):
            params = [
                p
                for p in self._signature.parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            ]
            if len(params) == 1 and params[0].name not in values:
                return self.func(values)
            return self.func(**values)
        return self.func(values)

    def __call__(self, values: dict[str, Any]) -> Any:
        return self.invoke(values)


class ProviderChatLLM:
    def __init__(self, config: LLMConfig, *, json_mode: bool) -> None:
        self.provider = provider_for(config)
        self.json_mode = json_mode

    def invoke(self, messages: list[Any]) -> LLMResponse:
        return LLMResponse(content=self.provider.chat(_normalize_messages(messages), json_mode=self.json_mode))


def SystemMessage(content: str) -> ChatMessage:
    return ChatMessage(role="system", content=content)


def HumanMessage(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def tool(func: Callable[..., Any]) -> ToolInvoker:
    return ToolInvoker(func)


@contextmanager
def use_llm_config(config: LLMConfig):
    token = _ACTIVE_LLM_CONFIG.set(config)
    try:
        yield
    finally:
        _ACTIVE_LLM_CONFIG.reset(token)


def load_keys_env() -> None:
    """Compatibility no-op: BioMA never reads credentials from local files."""


def _resolve_model(
    model_env_name: str | None, fallback_env_name: str | None = None
) -> str:
    if model_env_name and os.getenv(model_env_name):
        return str(os.getenv(model_env_name))
    if fallback_env_name and os.getenv(fallback_env_name):
        return str(os.getenv(fallback_env_name))
    return DEFAULT_MODEL


def _resolve_baidu_credentials(
    api_env_name: str | None = None,
    base_env_name: str | None = None,
) -> tuple[str, str, str]:
    api_key_env = api_env_name or "BAIDU_API_KEY"
    api_key = os.getenv(api_key_env) or ""
    base_url = (
        (os.getenv(base_env_name) if base_env_name else None)
        or os.getenv("BAIDU_BASE_URL")
        or DEFAULT_BAIDU_BASE_URL
    )
    return api_key_env, str(api_key), str(base_url)


def make_json_llm(
    *,
    model_env_name: str | None = None,
    api_env_name: str | None = None,
    base_env_name: str | None = None,
) -> ProviderChatLLM:
    return _make_llm(model_env_name, api_env_name, base_env_name, json_mode=True)


def make_text_llm(
    *,
    model_env_name: str | None = None,
    api_env_name: str | None = None,
    base_env_name: str | None = None,
) -> ProviderChatLLM:
    return _make_llm(model_env_name, api_env_name, base_env_name, json_mode=False)


def _make_llm(
    model_env_name: str | None,
    api_env_name: str | None,
    base_env_name: str | None,
    *, json_mode: bool,
) -> ProviderChatLLM:
    active_config = _ACTIVE_LLM_CONFIG.get()
    if active_config is not None:
        return ProviderChatLLM(active_config, json_mode=json_mode)
    env_name, api_key, base_url = _resolve_baidu_credentials(
        api_env_name=api_env_name,
        base_env_name=base_env_name,
    )
    if api_key:
        os.environ.setdefault(env_name, api_key)
    return ProviderChatLLM(
        LLMConfig(
            backend="openai",
            model=_resolve_model(model_env_name=model_env_name),
            api_base=base_url,
            api_key_env=env_name,
        ), json_mode=json_mode,
    )


def _normalize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role") or "user")
            content = message.get("content") or ""
        else:
            role = str(getattr(message, "role", "") or "user")
            content = getattr(message, "content", "") or ""
        if not isinstance(content, (str, list)):
            raise TypeError('Chat content must be text or structured multimodal parts.')
        normalized.append({"role": role, "content": content})
    return normalized


__all__ = [
    "DEFAULT_BAIDU_BASE_URL",
    "DEFAULT_MODEL",
    "HumanMessage",
    "SystemMessage",
    "ToolInvoker",
    "load_keys_env",
    "make_json_llm",
    "make_text_llm",
    "tool",
    "use_llm_config",
]
