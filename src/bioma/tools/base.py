from __future__ import annotations

import functools
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Type

from pydantic import BaseModel


class ToolInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True, "extra": "forbid"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    category: str
    version: str
    author: str
    func: Callable[..., Any]
    args_schema: Type[ToolInput] | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    requires_network: bool = False
    result_kind: str = "unclassified"
    result_validator: Callable[[Any, dict], bool] | None = None

    external_effect: bool = False
    replay_safe: bool = False
    resources: tuple[str, ...] = field(default_factory=tuple)
    reconcile_effect: Callable | None = None
    compensate_effect: Callable | None = None
    reconcile_compensation: Callable | None = None

    def call(self, *, action_instance_id: str | None = None, **kwargs: Any) -> Any:
        from bioma.capabilities.scoped import authorize_tool_invocation
        grant = authorize_tool_invocation(self, kwargs)
        from bioma.runtime.admission import check_execution
        check_execution(self)
        from bioma.runtime.deadline import check
        check()
        if self.args_schema is not None:
            kwargs = self.args_schema.model_validate(kwargs).model_dump()
        from bioma.runtime.effects import execute_tool
        from bioma.runtime.deadline import bounded_call
        from bioma.runtime.tool_actions import invoke
        return invoke(self.name, kwargs,
            lambda: bounded_call(lambda: execute_tool(self, kwargs, action_instance_id=action_instance_id)),
            version=self.version, external=self.external_effect, resources=self.resources,
            action_instance_id=action_instance_id,
            tool_grant_ref=grant.identity if grant is not None else None,
            tool_grant_context=grant.receipt_context() if grant is not None else None)

    @property
    def schema_json(self) -> str:
        return json.dumps(self.parameters_schema(), ensure_ascii=False)

    def parameters_schema(self) -> dict[str, Any]:
        schema = (
            self.args_schema.model_json_schema()
            if self.args_schema is not None
            else {"type": "object", "properties": {}}
        )
        return _sanitize_schema(schema)

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            },
        }


_REGISTERED_TOOLS: dict[str, Tool] = {}


def bioma_tool(
    name: str,
    description: str = "",
    category: str = "general",
    version: str = "1.0.0",
    author: str = "unknown",
    args_schema: Type[ToolInput] | None = None,
    tags: list[str] | None = None,
    requires_network: bool = False,
    result_kind: str = "unclassified",
    result_validator: Callable[[Any, dict], bool] | None = None,
    external_effect: bool = False,
    replay_safe: bool = False,
    resources: tuple[str, ...] = (),
    reconcile_effect: Callable | None = None,
    compensate_effect: Callable | None = None,
    reconcile_compensation: Callable | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a typed deterministic tool at import time."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        tool = Tool(
            name=name or func.__name__,
            description=description or (func.__doc__ or "").strip().split("\n")[0],
            category=category,
            version=version,
            author=author,
            func=wrapper,
            args_schema=args_schema,
            tags=tuple(tags or ()),
            requires_network=requires_network,
            result_kind=result_kind,
            result_validator=result_validator,
            external_effect=external_effect,
            replay_safe=replay_safe,
            resources=resources,
            reconcile_effect=reconcile_effect,
            compensate_effect=compensate_effect,
            reconcile_compensation=reconcile_compensation,
        )
        existing = _REGISTERED_TOOLS.get(tool.name)
        if existing is not None and _tool_source(existing.func) != _tool_source(wrapper):
            raise ValueError(
                f"Tool {tool.name!r} is registered by both "
                f"{_tool_source(existing.func)} and {_tool_source(wrapper)}."
            )
        _REGISTERED_TOOLS[tool.name] = tool
        wrapper._bioma_tool = tool  # type: ignore[attr-defined]
        return wrapper

    return decorator


def registered_tools() -> dict[str, Tool]:
    return dict(_REGISTERED_TOOLS)


def sanitize_parameters_schema(schema: Any) -> dict[str, Any]:
    """Return an OpenAI-compatible subset of a Pydantic parameter schema."""
    sanitized = _sanitize_schema(schema)
    if not isinstance(sanitized, dict):
        raise TypeError("A tool parameter schema must be an object.")
    return sanitized


def _tool_source(func: Callable[..., Any]) -> str:
    original = inspect.unwrap(func)
    return f"{inspect.getsourcefile(original) or original.__module__}:{original.__qualname__}"


def _sanitize_schema(node: Any, *, root: Any = None, refs: frozenset[str] = frozenset()) -> Any:
    root = node if root is None else root
    if isinstance(node, list):
        return [_sanitize_schema(item, root=root, refs=refs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in refs:
            raise ValueError("Tool parameter schema references must be local and non-recursive.")
        target = root
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Unknown tool parameter schema reference: {ref}") from exc
        base = _sanitize_schema(target, root=root, refs=refs | {ref})
        siblings = _sanitize_schema({key: value for key, value in node.items() if key != "$ref"},
                                    root=root, refs=refs | {ref})
        if set(siblings) <= {"title", "description", "default", "examples"}:
            return {**base, **siblings}
        return {"allOf": [base, siblings]}
    if "anyOf" in node:
        non_null = [
            item
            for item in node["anyOf"]
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null) == 1:
            merged = {key: value for key, value in node.items() if key != "anyOf"}
            base = _sanitize_schema(non_null[0], root=root, refs=refs)
            if isinstance(base, dict):
                base.update(merged)
                return _sanitize_schema(base, root=root, refs=refs)
    result = {
        key: _sanitize_schema(value, root=root, refs=refs)
        for key, value in node.items()
        if key not in {"$defs", "definitions"}
    }
    if result.get("type") == "object" or "properties" in result:
        result.setdefault("type", "object")
        result.setdefault("properties", {})
    return result
