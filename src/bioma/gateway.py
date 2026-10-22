"""Source-bound Research Contract Intake; no research classification or routing."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import PurePath
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from bioma.core.models import CapabilitySpec, StrictModel
from bioma.core.query_contract import QueryInterpretationError
from bioma.llm import ChatProvider, ModelOutputError, parse_json_object


class SourceFragment(StrictModel):
    ref: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)


class MissingUserInput(StrictModel):
    field: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    verification: Literal["unverified", "verified_missing"] = "unverified"
    scope: Literal["dependent_analysis", "execution"] = "dependent_analysis"
    # A semantic proposal, not a verified resource or permission failure.
    blocking: bool = True

    @model_validator(mode="after")
    def validate_stage(self):
        if self.verification == "verified_missing" and self.scope != "execution":
            raise ValueError("Verified missing input must be scoped to execution")
        return self


class QuerySpec(StrictModel):
    """The sole semantic representation emitted by the gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    goal: str = Field(min_length=1)
    explicit_constraints: tuple[str, ...] = ()
    explicit_exclusions: tuple[str, ...] = ()
    explicit_deliverables: tuple[str, ...] = ()
    authorization_conditions: tuple[str, ...] = ()
    available_inputs: tuple[str, ...] = ()
    missing_required_user_inputs: tuple[MissingUserInput, ...] = ()
    source_refs: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    source_fragments: tuple[SourceFragment, ...] = ()
    ignored_source_fragments: dict[str, str] = Field(default_factory=dict)
    interpretation_method: str = "thin_query_spec_v1"
    model_id: str | None = None

    @model_validator(mode="after")
    def validate_provenance(self):
        known = {item.ref for item in self.source_fragments}
        if any(set(refs) - known for refs in self.source_refs.values()):
            raise ValueError("QuerySpec source_refs must name compiler-created fragments")
        required = {"goal"}
        required.update(f"explicit_constraints:{i}" for i in range(len(self.explicit_constraints)))
        required.update(f"explicit_exclusions:{i}" for i in range(len(self.explicit_exclusions)))
        required.update(f"explicit_deliverables:{i}" for i in range(len(self.explicit_deliverables)))
        required.update(f"authorization_conditions:{i}" for i in range(len(self.authorization_conditions)))
        if required - self.source_refs.keys():
            raise ValueError("Every user-intent statement must retain a source reference")
        fragments = {row.ref: row.text for row in self.source_fragments}
        for field in ("explicit_constraints", "explicit_exclusions",
                      "explicit_deliverables", "authorization_conditions"):
            for index, text in enumerate(getattr(self, field)):
                cited = " ".join(fragments[ref] for ref in self.source_refs[f"{field}:{index}"])
                if not text.strip() or text not in cited:
                    raise ValueError(f"{field} must quote its cited user text verbatim")
        if set(self.ignored_source_fragments) - known or any(
                not reason.strip() for reason in self.ignored_source_fragments.values()):
            raise ValueError("Ignored fragments require compiler-owned refs and reasons")
        return self


class _QuerySpecProposal(StrictModel):
    """LLM output uses fragment indexes; the compiler owns reference symbols."""

    goal: str = Field(min_length=1)
    goal_fragments: list[int] = Field(min_length=1)
    explicit_constraints: list[str] = Field(default_factory=list)
    constraint_fragments: list[list[int]] = Field(default_factory=list)
    explicit_exclusions: list[str] = Field(default_factory=list)
    exclusion_fragments: list[list[int]] = Field(default_factory=list)
    explicit_deliverables: list[str] = Field(default_factory=list)
    deliverable_fragments: list[list[int]] = Field(default_factory=list)
    authorization_conditions: list[str] = Field(default_factory=list)
    authorization_fragments: list[list[int]] = Field(default_factory=list)
    missing_required_user_inputs: list[MissingUserInput] = Field(default_factory=list)


class QueryInterpreter(ABC):
    @abstractmethod
    def interpret(self, objective: str, *, inputs: dict[str, Any],
                  capabilities: tuple[CapabilitySpec, ...],
                  session_context: list[dict[str, Any]]) -> QuerySpec:
        raise NotImplementedError


def source_fragments(text: str) -> tuple[SourceFragment, ...]:
    """Create stable source symbols from verbatim contiguous query spans."""
    rows: list[SourceFragment] = []
    start = 0
    ends = [m.end() for m in re.finditer(r"[。！？；\n]+|(?<=[.!?;])\s+", text)]
    for end in ends + [len(text)]:
        fragment = text[start:end]
        if fragment.strip():
            rows.append(SourceFragment(ref=f"query:fragment:{len(rows)}",
                start=start, end=end, text=fragment))
        start = end
    return tuple(rows)


class LLMQueryInterpreter(QueryInterpreter):
    """One semantic call, plus at most one retry for invalid structured output."""

    def __init__(self, provider: ChatProvider, *, model_id: str,
                 max_attempts: int = 2, **_: Any) -> None:
        if max_attempts not in (1, 2):
            raise ValueError("Thin gateway permits one call and at most one schema retry")
        self.provider = provider
        self.model_id = model_id
        self.max_attempts = max_attempts

    def interpret(self, objective: str, *, inputs: dict[str, Any],
                  capabilities: tuple[CapabilitySpec, ...],
                  session_context: list[dict[str, Any]]) -> QuerySpec:
        del capabilities
        fragments = source_fragments(objective)
        manifest = _available_inputs(inputs)
        messages = [{
            "role": "system",
            "content": (
                "Record the user's goal and explicit constraints, exclusions, deliverables "
                "and authorization conditions. An authorization condition is not a grant. "
                "These are checkable contract interpretations, not a research plan or permission. "
                "Do not classify the task, choose methods/providers, add workflow steps, invent "
                "evidence obligations or attest missing resources. Preserve quoted user requirements "
                "verbatim and cite supporting fragment indexes for every item. Uncertain missing "
                "inputs are questions for subsequent observation, not entry vetoes. "
                "Return exact schema JSON."
            ),
        }, {
            "role": "user",
            "content": json.dumps({
                "query_fragments": [{"index": i, "text": row.text}
                                    for i, row in enumerate(fragments)],
                "available_inputs": manifest,
                "input_contracts": inputs.get("input_contracts", {}),
                "recent_context": session_context[-4:],
                "output_schema": _QuerySpecProposal.model_json_schema(),
            }, ensure_ascii=False, default=str),
        }]
        error: Exception | None = None
        for attempt in range(self.max_attempts):
            # Only a complete returned response is eligible for schema repair.
            # Transport, authorization, timeout and truncation errors retain their
            # original identity and never consume the schema retry allowance.
            response = self.provider.chat(messages, json_mode=True)
            try:
                proposal = _normalize_source_representation(_QuerySpecProposal.model_validate(parse_json_object(response)), fragments)
                return _compile_spec(proposal, fragments, manifest,
                                     method="thin_llm_query_spec_v1", model_id=self.model_id)
            except (ModelOutputError, ValidationError, QueryInterpretationError) as exc:
                error = exc
                if attempt + 1 < self.max_attempts:
                    messages.append({"role": "user", "content": json.dumps({
                        "repair_request": "The previous JSON was schema-invalid.",
                        "validation_error": str(exc),
                        "query_fragments": [{"index": i, "text": row.text}
                                            for i, row in enumerate(fragments)],
                        "verbatim_requirements": (
                            "Every explicit_constraints, explicit_exclusions, "
                            "explicit_deliverables, and authorization_conditions item must be a "
                            "contiguous, character-for-character substring of the text in its "
                            "cited fragment indexes. Copy the exact source text, including case "
                            "and punctuation. Remove an item rather than paraphrasing or inventing "
                            "it. Keep the complete user objective source-bound. Return one "
                            "corrected object only."
                        ),
                    }, ensure_ascii=False)})
        raise QueryInterpretationError(f"query specification failed: {error}") from error


def _normalize_source_representation(proposal, fragments):
    """Lossless source binding repair. Several named deliverables in one
    verbatim span may share that source; verify each filename before expansion.
    The strict compiler remains unchanged for all other malformed mappings.
    """
    updates={}
    groups=proposal.deliverable_fragments
    if len(groups)==1 and len(proposal.explicit_deliverables)>1:
        indexes=groups[0]
        if indexes and all(0<=i<len(fragments) for i in indexes):
            source=' '.join(fragments[i].text for i in indexes)
            names=[re.findall(r'[A-Za-z_][\w.-]*\.(?:json|md|txt|csv|png|pdf)',v) for v in proposal.explicit_deliverables]
            if all(items and all(name in source for name in items) for items in names):
                updates['deliverable_fragments']=[list(indexes) for _ in names]
    return proposal.model_copy(update=updates)


class DeterministicQueryInterpreter(QueryInterpreter):
    """Lossless offline intake. The complete original request stays authoritative."""

    def interpret(self, objective, *, inputs, capabilities, session_context):
        del capabilities, session_context
        fragments = source_fragments(objective)
        proposal = _QuerySpecProposal(goal=objective, goal_fragments=list(range(len(fragments))))
        return _compile_spec(proposal, fragments, _available_inputs(inputs),
                             method="verbatim_contract_intake_v1", model_id=None)


def _compile_spec(proposal: _QuerySpecProposal, fragments: tuple[SourceFragment, ...],
                  available: tuple[str, ...], *, method: str, model_id: str | None) -> QuerySpec:
    def refs(indexes: list[int]) -> tuple[str, ...]:
        if not indexes or any(i < 0 or i >= len(fragments) for i in indexes):
            raise QueryInterpretationError("Every semantic statement needs valid source fragments")
        return tuple(dict.fromkeys(fragments[i].ref for i in indexes))

    paired = ((proposal.explicit_constraints, proposal.constraint_fragments, "explicit_constraints"),
              (proposal.explicit_exclusions, proposal.exclusion_fragments, "explicit_exclusions"),
              (proposal.explicit_deliverables, proposal.deliverable_fragments, "explicit_deliverables"),
              (proposal.authorization_conditions, proposal.authorization_fragments, "authorization_conditions"))
    refs_by_field: dict[str, tuple[str, ...]] = {"goal": refs(proposal.goal_fragments)}
    for values, indexes, name in paired:
        if len(values) != len(indexes):
            raise QueryInterpretationError(f"{name} values and source mappings differ")
        refs_by_field.update({f"{name}:{i}": refs(row) for i, row in enumerate(indexes)})
    missing = tuple(proposal.missing_required_user_inputs)
    if method == "thin_llm_query_spec_v1":
        # The semantic model has not read files or checked execution grants. It
        # may propose a dependency, but it cannot attest resource absence.
        missing = tuple(item.model_copy(update={
            "verification": "unverified", "scope": "dependent_analysis"
        }) for item in missing)
    return QuerySpec(goal=proposal.goal,
        explicit_constraints=tuple(proposal.explicit_constraints),
        explicit_exclusions=tuple(proposal.explicit_exclusions),
        explicit_deliverables=tuple(proposal.explicit_deliverables), available_inputs=available,
        authorization_conditions=tuple(proposal.authorization_conditions),
        missing_required_user_inputs=missing,
        source_refs=refs_by_field, source_fragments=fragments,
        interpretation_method=method, model_id=model_id)


def _available_inputs(inputs: dict[str, Any]) -> tuple[str, ...]:
    rows: list[str] = []
    for key, value in sorted(inputs.items()):
        if key.startswith("_"):
            continue
        if key == "files" and isinstance(value, (list, tuple)):
            rows.extend(str(PurePath(str(path)).name) for path in value)
        elif value is not None:
            rows.append(key)
    return tuple(dict.fromkeys(rows))


from bioma.core.query_contract import verify_query_spec


def interpret_query(objective: str, scope: Any, inputs: dict[str, Any]) -> QuerySpec:
    """Retained public helper; scope no longer injects planning semantics."""
    del scope
    return DeterministicQueryInterpreter().interpret(objective, inputs=inputs,
        capabilities=(), session_context=[])
