"""Method-scoped Skill discovery and reviewable Tool grants.

This module adapts existing registries.  It does not register capabilities,
select scientific actions, issue WP1 permits, or commit scientific evidence.
"""
from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bioma.core.methods import ExecutionMethod
from bioma.core.research import fingerprint
from bioma.core.transitions import ExecutionPermit
from bioma.runtime.admission import callable_hash
from bioma.skills.registry import SkillRecord, SkillRegistry, _tokens
from bioma.tools.base import Tool
from bioma.tools.registry import ToolRegistry


_active_tool_grants = ContextVar("bioma_scoped_tool_grants", default=None)


class FrozenModel(BaseModel):
    """Local immutable contract model; capability code does not own control policy."""
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScopedToolRecord(FrozenModel):
    tool_id: str
    version: str
    implementation_identity: str
    implementation_path: str
    input_schema: dict[str, Any]
    result_kind: str
    result_validator_identity: str | None = None
    requires_network: bool = False
    external_effect: bool = False
    replay_safe: bool = False
    resources: tuple[str, ...] = ()
    executable: bool
    restriction_reason: str = ""


class ScopedSkillRecord(FrozenModel):
    stable_skill_id: str
    name: str
    description: str
    source: str
    domain: str
    tags: tuple[str, ...] = ()
    declared_tool_ids: tuple[str, ...] = ()
    available_tools: tuple[ScopedToolRecord, ...] = ()
    unavailable_tool_ids: tuple[str, ...] = ()


class CapabilitySnapshot(FrozenModel):
    provider_id: str
    capability_family: str
    method_id: str
    method_identity: str
    skill_registry_generation: int
    tool_registry_generation: int
    tool_implementation_identities: tuple[str, ...]

    @property
    def identity(self) -> str:
        return "capability-snapshot:" + fingerprint(self.model_dump(mode="json"))


class ToolGrant(FrozenModel):
    schema_version: str = "scoped-tool-grant-v1"
    request_id: str
    parent_scientific_action_ref: str
    contract_ref: str
    contract_version: int = Field(ge=1)
    plan_ref: str
    plan_version: int = Field(ge=1)
    provider_id: str
    capability_family: str
    method_id: str
    method_identity: str
    tool_id: str
    tool_version: str
    tool_implementation_identity: str
    allowed_arguments_schema: dict[str, Any]
    data_access_scope: tuple[str, ...] = ()
    resource_scope: tuple[str, ...] = ()
    result_contract: dict[str, Any]
    capability_snapshot_ref: str
    wp1_mandate_ref: str
    wp1_attempt_ref: str
    budget: float = Field(ge=0)
    issued_at: float
    expires_at: float
    revocable: bool = True

    @model_validator(mode="after")
    def coherent(self):
        Draft202012Validator.check_schema(self.allowed_arguments_schema)
        if self.expires_at <= self.issued_at:
            raise ValueError("Tool grant expiry must be later than issuance")
        return self

    @property
    def identity(self) -> str:
        return "tool-grant:" + fingerprint(self.model_dump(mode="json"))

    def receipt_context(self) -> dict[str, Any]:
        return {"parent_scientific_action_ref": self.parent_scientific_action_ref,
            "contract_ref": self.contract_ref, "contract_version": self.contract_version,
            "plan_ref": self.plan_ref, "plan_version": self.plan_version,
            "provider_id": self.provider_id, "capability_family": self.capability_family,
            "method_id": self.method_id, "capability_snapshot_ref": self.capability_snapshot_ref,
            "wp1_mandate_ref": self.wp1_mandate_ref, "wp1_attempt_ref": self.wp1_attempt_ref}

    def validate_invocation(self, tool: Tool, arguments: dict[str, Any], *,
                            snapshot_ref: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now >= self.expires_at:
            raise ValueError("Tool grant expired")
        if snapshot_ref != self.capability_snapshot_ref:
            raise ValueError("Tool grant is stale")
        if (tool.name, tool.version, tool_identity(tool)) != (
                self.tool_id, self.tool_version, self.tool_implementation_identity):
            raise ValueError("Tool implementation differs from grant")
        error = next(iter(Draft202012Validator(self.allowed_arguments_schema).iter_errors(arguments)), None)
        if error is not None:
            raise ValueError("Tool arguments exceed grant schema: " + error.message)
        if tool.category == "paid-remote-compute":
            requested = arguments.get("max_credits")
            if not isinstance(requested, (int, float)) or isinstance(requested, bool) or requested <= 0:
                raise ValueError("Paid remote compute requires a positive max_credits ceiling")
            estimate = arguments.get("estimated_credits")
            if not isinstance(estimate, (int, float)) or isinstance(estimate, bool) or estimate <= 0:
                raise ValueError("Paid remote compute requires a positive credit estimate")
            if estimate > requested:
                raise ValueError("Paid remote compute estimate exceeds its task ceiling")
            if self.budget <= 0 or float(requested) > self.budget:
                raise ValueError("Paid remote compute exceeds the WP1 ToolGrant budget")


def tool_identity(tool: Tool) -> str:
    return fingerprint({"name": tool.name, "version": tool.version,
        "callable": callable_hash(tool.func), "validator": callable_hash(tool.result_validator),
        "result_kind": tool.result_kind, "network": tool.requires_network,
        "external_effect": tool.external_effect, "replay_safe": tool.replay_safe,
        "resources": list(tool.resources)})


def _tool_record(tool: Tool, *, executable: bool, reason: str = "") -> ScopedToolRecord:
    original = inspect.unwrap(tool.func)
    return ScopedToolRecord(tool_id=tool.name, version=tool.version,
        implementation_identity=tool_identity(tool),
        implementation_path=inspect.getsourcefile(original) or original.__module__,
        input_schema=tool.parameters_schema(), result_kind=tool.result_kind,
        result_validator_identity=(callable_hash(tool.result_validator) or None),
        requires_network=tool.requires_network, external_effect=tool.external_effect,
        replay_safe=tool.replay_safe, resources=tuple(tool.resources),
        executable=executable, restriction_reason=reason)


@dataclass(frozen=True)
class ScopedSkillView:
    """SkillRegistry-compatible progressive view over one Method grant."""
    resolver: "ScopedSkillResolver"
    records: tuple[ScopedSkillRecord, ...]

    @property
    def generation(self) -> int:
        return self.resolver.skill_registry.generation

    def all(self) -> tuple[SkillRecord, ...]:
        allowed = {row.stable_skill_id for row in self.records}
        return tuple(record for record in self.resolver.skill_registry.all()
                     if self.resolver.stable_id(record) in allowed)

    def list(self, question: str, *, limit: int = 8, offset: int = 0) -> dict[str, Any]:
        page = self.resolver._page(self.records, question=question, limit=limit, offset=offset)
        return {"success": True, "returned": len(page), "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(self.records) else None,
                "skills": [{"name": row.name, "stable_skill_id": row.stable_skill_id,
                            "description": row.description[:300], "domain": row.domain,
                            "tags": list(row.tags)} for row in page]}

    def taxonomy_list(self, prefix: str = "", max_depth: int = 1) -> dict[str, Any]:
        del max_depth
        categories = sorted({row.domain for row in self.records
                             if not prefix or row.domain.startswith(prefix)})
        return {"success": True, "categories": categories}

    def taxonomy_skills(self, taxonomy: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows = tuple(row for row in self.records if row.domain == taxonomy)
        page = rows[offset:offset + limit]
        return {"success": True, "returned": len(page), "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
                "skills": [{"name": row.name, "stable_skill_id": row.stable_skill_id,
                            "description": row.description[:300], "domain": row.domain,
                            "tags": list(row.tags)} for row in page]}

    def view(self, name: str, *, file_path: str | None = None,
             max_chars: int = 8000) -> dict[str, Any]:
        row = next((item for item in self.records
                    if name in {item.name, item.stable_skill_id}), None)
        if row is None:
            return {"success": False, "error": "Skill is outside the current Method scope: " + name}
        value = self.resolver.skill_registry.view(row.name, file_path=file_path, max_chars=max_chars)
        if value.get("success"):
            value["stable_skill_id"] = row.stable_skill_id
            value["discovery_grants_execution"] = False
            value["declared_tools"] = list(row.declared_tool_ids)
            value["tools"] = [tool.tool_id for tool in row.available_tools if tool.executable]
            value["unavailable_tools"] = list(row.unavailable_tool_ids) + [
                tool.tool_id for tool in row.available_tools if not tool.executable]
        return value


class ScopedSkillResolver:
    """Intersect discovery metadata with an already-qualified Method tool set."""

    def __init__(self, skill_registry: SkillRegistry, tool_registry: ToolRegistry) -> None:
        self.skill_registry = skill_registry
        self.tool_registry = tool_registry
        self._stable_by_path: dict[str, str] = {}
        for record in skill_registry.all():
            stable = record.name
            for locations in getattr(skill_registry, "_locations", ()):
                candidate = locations.resolve_skill_id(record.path.parent.relative_to(
                    locations.root.parent).as_posix())
                if candidate is not None:
                    stable = candidate
                    break
            self._stable_by_path[str(record.path)] = stable

    def stable_id(self, record: SkillRecord) -> str:
        return self._stable_by_path.get(str(record.path), record.name)

    def snapshot(self, *, provider_id: str, capability_family: str,
                 method: ExecutionMethod, tools: Iterable[Tool]) -> CapabilitySnapshot:
        identities = tuple(sorted(tool_identity(tool) for tool in tools))
        return CapabilitySnapshot(provider_id=provider_id, capability_family=capability_family,
            method_id=method.id, method_identity=fingerprint(method.model_dump(mode="json")),
            skill_registry_generation=self.skill_registry.generation,
            tool_registry_generation=self.tool_registry.generation,
            tool_implementation_identities=identities)

    def discover(self, *, provider_id: str, capability_family: str,
                 method: ExecutionMethod, method_tools: Iterable[Tool], question: str = "",
                 result_kinds: Iterable[str] = (), exact_skill_id: str | None = None,
                 offset: int = 0, limit: int = 20, knowledge_discovery: bool = False) -> tuple[ScopedSkillRecord, ...]:
        if offset < 0 or limit < 1 or limit > 200:
            raise ValueError("Invalid discovery page")
        allowed = {tool.name: tool for tool in method_tools}
        current = {tool.name: tool for tool in self.tool_registry.all()}
        expected = set(result_kinds)
        rows = []
        seen = set()
        for skill in self.skill_registry.all():
            if skill.status not in {"active", "experimental"}:
                continue
            sid = self.stable_id(skill)
            if exact_skill_id and exact_skill_id not in {sid, skill.name}:
                continue
            declared = tuple(dict.fromkeys(skill.tools))
            relevant = [allowed[name] for name in declared if name in allowed]
            if not relevant and not knowledge_discovery:
                continue
            available = []
            unavailable = []
            for tool in relevant:
                same = current.get(tool.name) is tool
                typed = tool.result_kind != "unclassified" and tool.result_validator is not None
                matches = not expected or tool.result_kind in expected
                executable = same and typed and matches
                reason = ""
                if not same:
                    reason = "implementation unavailable or replaced"
                elif not typed:
                    reason = "missing typed result kind or validator"
                elif not matches:
                    reason = "result kind does not match discovery request"
                available.append(_tool_record(tool, executable=executable, reason=reason))
            unavailable.extend(name for name in declared if name not in allowed)
            if sid in seen:
                continue
            seen.add(sid)
            rows.append(ScopedSkillRecord(stable_skill_id=sid, name=skill.name,
                description=skill.description, source=skill.source, domain=skill.domain,
                tags=tuple(skill.tags), declared_tool_ids=declared,
                available_tools=tuple(available), unavailable_tool_ids=tuple(sorted(unavailable))))
        return self._page(tuple(rows), question=question, offset=offset, limit=limit)

    @staticmethod
    def _page(rows: tuple[ScopedSkillRecord, ...], *, question: str,
              offset: int, limit: int) -> tuple[ScopedSkillRecord, ...]:
        query = _tokens(question)
        ranked = sorted(rows, key=lambda row: (-sum((4 if token in _tokens(row.name) else 0) +
            (2 if token in _tokens(" ".join((row.domain, *row.tags))) else 0) +
            (1 if token in _tokens(row.description) else 0) for token in query), row.stable_skill_id))
        return tuple(ranked[offset:offset + limit])

    def view(self, **kwargs) -> ScopedSkillView:
        # A view is the full catalogue; paging happens at each metadata request.
        # Previously the first 20 records permanently hid the remaining skills.
        rows = []
        offset = 0
        kwargs = {k: v for k, v in kwargs.items() if k not in {"offset", "limit"}}
        while True:
            page = self.discover(**kwargs, offset=offset, limit=200)
            rows.extend(page)
            if len(page) < 200:
                break
            offset += len(page)
        return ScopedSkillView(self, tuple(rows))

    def issue_grants(self, *, request_id: str, parent_scientific_action_ref: str,
                     contract_ref: str, plan_ref: str, plan_version: int,
                     provider_id: str, capability_family: str, method: ExecutionMethod,
                     method_tools: Iterable[Tool], permit: ExecutionPermit, budget: float,
                     contract_version: int = 1, issued_at: float | None = None) -> tuple[ToolGrant, ...]:
        if permit.provider_id != provider_id or permit.capability_id != capability_family:
            raise ValueError("WP1 permit does not authorize this provider capability")
        if not permit.live(now=time.time()):
            raise ValueError("WP1 permit is not live")
        tools = tuple(method_tools)
        registered = {tool.name: tool for tool in self.tool_registry.all()}
        changed = [tool.name for tool in tools if registered.get(tool.name) is not tool]
        if changed:
            raise ValueError("Capability snapshot is stale; tools unavailable or replaced: "
                             + ", ".join(sorted(changed)))
        snapshot = self.snapshot(provider_id=provider_id, capability_family=capability_family,
                                 method=method, tools=tools)
        now = time.time() if issued_at is None else issued_at
        # A continuous work package grants a fixed tool set for its admitted
        # lifetime. Ordinary methods retain the shorter 600s grant. Neither
        # case renews a grant or extends the parent mandate.
        expiry = (permit.expires_at if method.id == 'continuous_scientific_investigation'
                  else min(permit.expires_at, now + 600))
        grants = []
        for tool in tools:
            if tool.result_kind == "unclassified" or tool.result_validator is None:
                continue
            scopes = ["network:declared"] if tool.requires_network else ["local:method-inputs"]
            grants.append(ToolGrant(request_id=request_id,
                parent_scientific_action_ref=parent_scientific_action_ref,
                contract_ref=contract_ref, contract_version=contract_version,
                plan_ref=plan_ref, plan_version=plan_version,
                provider_id=provider_id, capability_family=capability_family,
                method_id=method.id, method_identity=snapshot.method_identity,
                tool_id=tool.name, tool_version=tool.version,
                tool_implementation_identity=tool_identity(tool),
                allowed_arguments_schema=tool.parameters_schema(),
                data_access_scope=tuple(scopes), resource_scope=tuple(tool.resources),
                result_contract=method.result.model_dump(mode="json"),
                capability_snapshot_ref=snapshot.identity,
                wp1_mandate_ref=permit.mandate_id, wp1_attempt_ref=permit.attempt_id,
                budget=budget, issued_at=now, expires_at=expiry))
        return tuple(grants)

    def verify_snapshot(self, snapshot: CapabilitySnapshot, *, method: ExecutionMethod,
                        tools: Iterable[Tool]) -> None:
        current = self.snapshot(provider_id=snapshot.provider_id,
            capability_family=snapshot.capability_family, method=method, tools=tools)
        if current.identity != snapshot.identity:
            raise ValueError("Capability snapshot is stale")


@contextmanager
def scoped_tool_grant_scope(grants: Iterable[ToolGrant], snapshot: CapabilitySnapshot):
    """Require an exact live grant for each Tool call in the current Method."""
    by_tool = {grant.tool_id: grant for grant in grants}
    token = _active_tool_grants.set((by_tool, snapshot.identity))
    try:
        yield
    finally:
        _active_tool_grants.reset(token)


SCOPED_METADATA_TOOL_IDS = frozenset({
    "taxonomy_list", "taxonomy_skills", "skills_list", "skill_view",
})


def is_scoped_metadata_tool(tool: Tool) -> bool:
    """Recognize only the resolver's inert metadata adapters."""
    original = inspect.unwrap(tool.func)
    return (tool.name in SCOPED_METADATA_TOOL_IDS and tool.category == "skills"
            and tool.author == "BioMA" and tool.version == "scoped-v1"
            and original.__module__ == __name__)


def authorize_tool_invocation(tool: Tool, arguments: dict[str, Any]) -> ToolGrant | None:
    """Validate the active grant; bounded Skill metadata calls grant no execution."""
    if is_scoped_metadata_tool(tool):
        return None
    active = _active_tool_grants.get()
    if active is None:
        return None
    grants, snapshot_ref = active
    grant = grants.get(tool.name)
    if grant is None:
        raise ValueError("No scoped ToolGrant authorizes " + tool.name)
    grant.validate_invocation(tool, arguments, snapshot_ref=snapshot_ref)
    return grant


def build_scoped_skill_tools(view: ScopedSkillView) -> tuple[Tool, ...]:
    """Build bounded metadata tools over one immutable Method-scoped view."""
    from bioma.skills.tools import (SkillSearchInput, SkillViewInput,
                                    TaxonomyListInput, TaxonomySkillsInput)
    definitions = (
        ("taxonomy_list", "Browse categories inside the current Method scope.",
         view.taxonomy_list, TaxonomyListInput),
        ("taxonomy_skills", "Page Skills inside one scoped category.",
         view.taxonomy_skills, TaxonomySkillsInput),
        ("skills_list", "Search Skills inside the current Method scope.",
         view.list, SkillSearchInput),
        ("skill_view", "Read one discovered scoped Skill; only granted Tools are revealed.",
         view.view, SkillViewInput),
    )
    return tuple(Tool(name=name, description=description, category="skills",
        version="scoped-v1", author="BioMA", func=func, args_schema=schema,
        tags=("skills", "method-scoped"))
        for name, description, func, schema in definitions)
