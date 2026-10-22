"""WP2 scope: a *view* over an existing Evidence field, not a new field.

WP2 introduces no container and no field. A scientific claim's scope is read
out of the existing ``Evidence.value`` payload, so the committed evidence
channel stays the single source of truth::

    value has no ``scope`` key     -> UNDECLARED
    value["scope"] == []           -> DECLARED(frozenset())   explicitly unconditional
    value["scope"] == ["a", "b"]   -> DECLARED({"a", "b"})    scoped

``UNDECLARED != DECLARED(frozenset())``: "nothing was declared" and "declared
to be unconditional" are different facts and are never conflated.

Matching is exact-string only. There is no units ontology and no synonym
reasoning: ``"human"`` and ``"Human"`` are distinct scopes, and ``"10mg"`` is
never normalised to ``"0.01g"``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Union

from bioma.core.protocol_invariants import ProtocolViolation

SCOPE_KEY = "scope"


class ScopeShapeViolation(ProtocolViolation):
    """E5a: a declared scope is malformed and cannot be interpreted at all."""

    code, invariant = "scope_shape", "E5a"


@dataclass(frozen=True)
class UndeclaredScope:
    """No scope was declared: the claim does not say where it applies."""

    def __repr__(self) -> str:
        return "UNDECLARED"


UNDECLARED = UndeclaredScope()


@dataclass(frozen=True)
class DeclaredScope:
    """An explicitly declared scope; an empty set means *unconditional*."""

    values: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        normalized = frozenset(self.values)
        for value in normalized:
            if not isinstance(value, str) or not value.strip():
                raise ScopeShapeViolation(
                    "scope entries must be non-empty exact strings"
                )
        object.__setattr__(self, "values", normalized)

    @property
    def unconditional(self) -> bool:
        return not self.values

    def __repr__(self) -> str:
        return "DECLARED(" + ",".join(sorted(self.values)) + ")"


Scope = Union[UndeclaredScope, DeclaredScope]


def covers(outer: Scope, inner: Scope) -> bool:
    """Exact, frozen scope compatibility.

    The two contract rows are implemented verbatim::

        covers(UNDECLARED, DECLARED(frozenset())) is True
        covers(UNDECLARED, DECLARED(non-empty))   is False

    Two declared scopes compare by exact-string subset. The remaining two
    combinations are left unspecified by the contract, so they fail closed
    instead of silently succeeding.
    """
    if isinstance(outer, DeclaredScope):
        if isinstance(inner, DeclaredScope):
            return inner.values <= outer.values
        return False
    if isinstance(inner, DeclaredScope):
        return inner.unconditional
    return True


def parse_scope(value: object) -> Scope:
    """Read a scope from an ``Evidence.value`` payload (``None`` or mapping).

    A non-mapping payload is not a scope-shape problem; the caller that owns
    the reserved-role schema raises E1a for it. Here the payload is assumed to
    be ``None`` or a mapping already.
    """
    if value is None:
        return UNDECLARED
    if not isinstance(value, Mapping):
        raise ScopeShapeViolation("scope payload must be a mapping")
    if SCOPE_KEY not in value:
        return UNDECLARED
    declared = value[SCOPE_KEY]
    if isinstance(declared, (str, bytes)) or not isinstance(declared, Sequence):
        raise ScopeShapeViolation("scope must be a list of exact strings")
    return DeclaredScope(frozenset(declared))


__all__ = [
    "DeclaredScope",
    "SCOPE_KEY",
    "Scope",
    "ScopeShapeViolation",
    "UNDECLARED",
    "UndeclaredScope",
    "covers",
    "parse_scope",
]
