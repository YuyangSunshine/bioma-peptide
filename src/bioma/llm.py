from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bioma.config import LLMConfig


class ModelTransportError(ConnectionError):
    """The model service is temporarily unavailable; generated work must not be repaired for this error."""


class ModelBudgetUnavailable(RuntimeError):
    """No authorized model reservation; not a scientific computation failure."""


class ComputedPendingReview(ModelBudgetUnavailable):
    """Computed files are durable but have no review or Evidence authority."""
    def __init__(self, checkpoint, reason):
        self.checkpoint = str(checkpoint)
        self.execution_status = "COMPUTED_PENDING_REVIEW"
        super().__init__(f"COMPUTED_PENDING_REVIEW: {reason}")


class ModelOutputError(ValueError):
    """A structured model reply is unusable; the existing repair budget applies."""


class ModelOutputBudgetError(ModelOutputError):
    """Truncated completion needs a configuration change, not JSON repair."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def to_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class ChatResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    finish_reason: str | None = None
    reasoning_content: str | None = field(default=None, repr=False)

    def assistant_message(self) -> dict[str, Any]:
        # Native tool calls already carry the action; free-form tool markup is not replayable history.
        message: dict[str, Any] = {"role": "assistant", "content": "" if self.tool_calls else self.content}
        if self.tool_calls:
            message["tool_calls"] = [item.to_openai_dict() for item in self.tool_calls]
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        return message


class ChatProvider(ABC):
    supports_native_tools = True

    @abstractmethod
    def chat(self, messages: list[dict[str, Any]], *, json_mode: bool = False) -> str:
        raise NotImplementedError

    @abstractmethod
    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *, json_mode: bool = False, tool_choice: dict[str, Any] | str | None = None,
    ) -> ChatResponse:
        raise NotImplementedError



# The binding is owned by trusted dispatch, never by generated analysis code.
from contextvars import ContextVar
from contextlib import contextmanager
_dispatch_budget = ContextVar('bioma_dispatch_budget', default=None)
_delegated_budget = ContextVar('bioma_delegated_budget', default=None)


@contextmanager
def delegated_budget(budget, *, persist=None):
    """Professional Method calls charge the live enclosing Harness budget."""
    token = _delegated_budget.set((budget, persist))
    try:
        yield
    finally:
        _delegated_budget.reset(token)


@contextmanager
def dispatch_budget(ledger, pair, arm, policy, input_bound=None):
    import copy
    policy = copy.deepcopy(policy)
    ledger.configure_budget(pair, policy)
    token = _dispatch_budget.set((ledger, pair, arm, policy, input_bound))
    try:
        yield
    finally:
        _dispatch_budget.reset(token)


def remaining_dispatches():
    active = _dispatch_budget.get()
    if active:
        ledger, pair, arm, _, _ = active
        return ledger.remaining_dispatches(pair, arm)
    return None


def reserve_tool_dispatch(name):
    delegated = _delegated_budget.get()
    if delegated is not None:
        budget, persist = delegated
        if budget.remaining_tool_calls <= 0:
            raise ModelBudgetUnavailable('Enclosing tool budget exhausted')
        budget.record_tool_call()
        if persist:
            persist()
    active = _dispatch_budget.get()
    if active:
        ledger, pair, arm, policy, _ = active
        ledger.reserve_dispatch(pair, arm, 'tool', 0, 0, {'name': name})


def _reserve_model(config, packet):
    delegated = _delegated_budget.get()
    if delegated is not None:
        budget, persist = delegated
        if budget.remaining_llm_calls <= 0:
            raise ModelBudgetUnavailable('Enclosing model budget exhausted')
        budget.record_llm_call()
        if persist:
            persist()
    active = _dispatch_budget.get()
    if not active:
        return None
    ledger, pair, arm, policy, bound = active
    if str(config.api_base).rstrip('/') != policy['endpoint'].rstrip('/') or packet.get('model') != policy['model']:
        raise ModelBudgetUnavailable('Endpoint/model outside frozen budget policy')
    if policy.get('metering_mode') == 'usage_only_authorized':
        from bioma.memory.context import current_model_role
        identity = ledger.reserve_dispatch(pair, arm, 'model', 0, 0,
            {'role': current_model_role(), 'metering_mode': 'usage_only_authorized', 'usage': 'unknown_until_response'})
        return ledger, identity, policy
    if bound is None or not policy.get('metering_source') or not policy.get('price_source'):
        raise ModelBudgetUnavailable('No trusted final-request token bound or price source; not dispatched')
    import copy
    if set(packet.get('extra_body') or {}) - {'thinking'}:
        raise ModelBudgetUnavailable('Unmetered extra_body overrides; not dispatched')
    prompt = bound(copy.deepcopy(packet))
    output = packet.get('max_completion_tokens', packet.get('max_tokens'))
    n = packet.get('n', 1)
    if any(type(v) is not int or v < 1 for v in (prompt, output, n)) or packet.get('stream'):
        raise ModelBudgetUnavailable('Unsupported request metering; not dispatched')
    # Reserve input for each choice conservatively; output is per choice.
    tokens = n * (prompt + output)
    cost = n * (prompt * policy['input_nanocny_per_token'] + output * policy['output_nanocny_per_token'])
    import hashlib
    from bioma.memory.context import current_model_role
    identity = ledger.reserve_dispatch(pair, arm, 'model', tokens, cost,
        {'request_sha256': hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest(), 'n': n, 'role': current_model_role(), 'reserved_tokens': tokens, 'reserved_nanocny': cost, 'input_bound': prompt, 'output_per_choice': output})
    return ledger, identity, policy


def _settle_model(reservation, usage):
    delegated = _delegated_budget.get()
    if delegated is not None and usage is not None and all(
            type(v) is int and v >= 0 for v in (usage.prompt_tokens, usage.completion_tokens)):
        budget, persist = delegated
        budget.telemetry.prompt_tokens += usage.prompt_tokens
        budget.telemetry.completion_tokens += usage.completion_tokens
        if persist:
            persist()
    if not reservation or usage is None:
        return
    ledger, identity, policy = reservation
    prompt, output, total = usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
    if any(type(v) is not int or v < 0 for v in (prompt, output, total)) or total != prompt + output:
        return  # Unknown/untrustworthy usage retains the entire reservation.
    ledger.settle_dispatch(identity, total, prompt * policy['input_nanocny_per_token'] + output * policy['output_nanocny_per_token'])


class OpenAICompatibleProvider(ChatProvider):
    supports_forced_tool_choice = True
    """One transport with trusted per-instance visibility and observation hooks.

    Preparation runs after context augmentation and before budget reservation;
    only messages may change. Observations carry detached packet/response copies.
    Hooks neither dispatch HTTP nor grant tool/scientific authority. Their failures
    propagate; reservations already made are not reset by instrumentation failure.
    """
    def __init__(
        self, config: LLMConfig, *,
        prepare_request: Callable[[LLMConfig, dict[str, Any]], dict[str, Any]] | None = None,
        observe_request: Callable[[dict[str, Any]], None] | None = None,
        review_input_material: Callable[[Any, str], dict[str, Any]] | None = None,
    ) -> None:
        from openai import OpenAI

        if not config.api_key:
            raise RuntimeError(
                f"Missing LLM API key in environment variable {config.api_key_env}."
            )
        self.config = config
        self.prepare_request = prepare_request
        self.observe_request = observe_request
        self.review_input_material = review_input_material
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base,
            timeout=config.timeout_seconds,
            max_retries=2,
        )

    def _request(self, *, json_mode: bool = False, **kwargs):
        from bioma.runtime.admission import check_execution
        check_execution()
        from bioma.memory.context import augment_messages
        kwargs['messages'] = augment_messages(kwargs.get('messages', []))
        from bioma.runtime.deadline import bounded_call, remaining
        from bioma.runtime.telemetry import emit
        import time
        started = time.monotonic()
        self.last_prompt_tokens = self.last_completion_tokens = 0
        if json_mode:
            if kwargs.get('tools'):
                raise ValueError('JSON responses cannot be combined with tool selection.')
            kwargs['response_format'] = {'type': 'json_object'}
        if self.config.temperature is not None:
            kwargs['temperature'] = self.config.temperature
        if self.config.thinking_mode is not None:
            kwargs['extra_body'] = {'thinking': {'type': self.config.thinking_mode}}
        # Trusted, instance-scoped visibility policy transforms the final packet.
        # It cannot replace dispatch, budget reservation or evidence admission.
        prepare = getattr(self, 'prepare_request', None)
        if prepare is not None:
            from copy import deepcopy
            original = deepcopy(kwargs)
            kwargs = prepare(deepcopy(self.config), deepcopy(kwargs))
            if not isinstance(kwargs, dict):
                raise TypeError('Request preparation must return a packet dictionary')
            if ({key: value for key, value in kwargs.items() if key != 'messages'} !=
                    {key: value for key, value in original.items() if key != 'messages'}):
                raise ValueError('Visibility policy may only transform messages, not model or tool configuration')
            if not isinstance(kwargs.get('messages'), list):
                raise TypeError('Prepared messages must be a list')
        emit('llm.started', model=self.config.model, detail='Awaiting model response')
        left = remaining()
        client = self.client.with_options(timeout=min(self.config.timeout_seconds, left) if left is not None else self.config.timeout_seconds, max_retries=0)
        reservation = _reserve_model(self.config, kwargs)
        from uuid import uuid4
        from copy import deepcopy
        request_id = reservation[1] if reservation else str(uuid4())
        observer = getattr(self, 'observe_request', None)
        if observer is not None:
            observer({'phase': 'dispatch', 'request_id': request_id,
                      'model': self.config.model, 'packet': deepcopy(kwargs)})
        try:
            result = bounded_call(lambda: client.chat.completions.create(**kwargs))
        except Exception as exc:
            if observer is not None:
                observer({'phase': 'failed', 'request_id': request_id,
                          'error_type': type(exc).__name__, 'error': str(exc),
                          'elapsed': time.monotonic()-started})
            emit('llm.failed', model=self.config.model, error_type=type(exc).__name__, elapsed_seconds=time.monotonic()-started)
            from openai import APIConnectionError, APIStatusError
            transient_status = isinstance(exc, APIStatusError) and (
                exc.status_code in {408, 429} or exc.status_code >= 500)
            if isinstance(exc, APIConnectionError) or transient_status:
                raise ModelTransportError(str(exc)) from exc
            raise
        _settle_model(reservation, result.usage)
        if result.usage is not None:
            self.last_prompt_tokens = result.usage.prompt_tokens
            self.last_completion_tokens = result.usage.completion_tokens
        if observer is not None:
            observer({'phase': 'completed', 'request_id': request_id,
                      'response': deepcopy(result), 'elapsed': time.monotonic()-started})
        emit('llm.completed', model=self.config.model, elapsed_seconds=time.monotonic()-started,
             finish_reason=result.choices[0].finish_reason,
             usage=result.usage.model_dump() if result.usage else None)
        if result.choices[0].finish_reason == 'length':
            raise ModelOutputBudgetError('Model output budget exhausted (finish_reason=length); partial text/tool calls cannot authorize execution. Adjust the explicit token budget or supported thinking mode before retrying.')
        return result

    def chat(self, messages: list[dict[str, Any]], *, json_mode: bool = False) -> str:
        response = self._request(
            json_mode=json_mode,
            model=self.config.model,
            messages=messages,
            **{self.config.max_output_parameter: self.config.max_output_tokens},
        )
        return response.choices[0].message.content or ""

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *, json_mode: bool = False, tool_choice: dict[str, Any] | str | None = None,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            self.config.max_output_parameter: self.config.max_output_tokens,
        }
        if tools:
            kwargs.update(
                {
                    "tools": tools,
                    "tool_choice": tool_choice if tool_choice is not None else "auto",
                    "parallel_tool_calls": False,
                }
            )
        response = self._request(json_mode=json_mode, **kwargs)
        choice = response.choices[0]
        calls = tuple(
            ToolCall(
                id=call.id or f"call-{index}",
                name=call.function.name,
                arguments=call.function.arguments or "{}",
            )
            for index, call in enumerate(choice.message.tool_calls or (), start=1)
        )
        return ChatResponse(
            content=choice.message.content or "",
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            reasoning_content=getattr(choice.message, "reasoning_content", None),
        )


def provider_for(config: LLMConfig) -> ChatProvider:
    return OpenAICompatibleProvider(config)


def parse_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ModelOutputError("LLM response did not contain a JSON object.")
    # Providers sometimes emit literal newlines/tabs inside quoted JSON strings.
    # Escape only those characters, preserving the proposed data and retaining
    # strict parsing for every other syntax error (including truncated output).
    candidate = text[start : end + 1]
    quoted = escaped = False
    normalized = []
    for char in candidate:
        if quoted and not escaped and char in "\n\r\t":
            normalized.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            continue
        normalized.append(char)
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
    try:
        payload = json.loads("".join(normalized))
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"Invalid model JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelOutputError("LLM response must be a JSON object.")
    return payload
