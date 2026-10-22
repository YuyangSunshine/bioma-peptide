"""Experimental-only protocol policy injection, for ablations.

Production behaviour is ``PRODUCTION_POLICY``: every protocol component enabled.
Nothing here is reachable unless a caller explicitly activates an experimental
policy inside a ``use_policy(...)`` block, and ``use_policy`` refuses any policy
that is not marked experimental. That makes it impossible to change production
semantics through this interface by accident.

The four switches map one-to-one onto the four components the protocol claims
are necessary, so each can be ablated independently:

===============  =========================================================
admission        pre-execution admission checks (risk ceiling, contract
                 preconditions, initial invariants, resource availability)
evidence_gate    the evidence/effect gate that refuses to let a transition
                 commit when its promised evidence and effects are absent
failure_resilience  retry/repair recovery instead of fail-fast termination
decentralized_proposal  offer/commitment routing instead of the runtime
                 collapsing a request onto one directly dispatched provider
===============  =========================================================

The four WP2 semantic controls are carried by the same policy object and are
equally experimental-only. Production always applies the full WP2 semantics:

==================  =====================================================
scope_checking      a claim/evidence link whose scope is incompatible with
                    the claim's scope is excluded from derived status
contradiction_preservation  contradicting claims are all kept and all take
                    part in derivation, instead of being collapsed
derived_epistemic_status  epistemic status is recomputed from the committed
                    record and is never read back from a stored field
epistemic_role_conflation  reserved roles stay distinct, so the reserved
                    role schema (E1a) is enforced
==================  =====================================================
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

COMPONENTS = (
    "admission",
    "evidence_gate",
    "failure_resilience",
    "decentralized_proposal",
)

# WP2 controls. The first three are enabled in production; the gating ablation
# is stated affirmatively, so "conflation" is off in production and only an
# experimental policy turns it on.
WP2_CONTROLS = (
    "scope_checking",
    "contradiction_preservation",
    "derived_epistemic_status",
    "epistemic_role_conflation",
)


@dataclass(frozen=True)
class ProtocolPolicy:
    """Which protocol components are enforced. Defaults reproduce production."""

    admission: bool = True
    evidence_gate: bool = True
    failure_resilience: bool = True
    decentralized_proposal: bool = True
    # WP2 semantic controls. Production applies every one of them.
    scope_checking: bool = True
    contradiction_preservation: bool = True
    derived_epistemic_status: bool = True
    epistemic_role_conflation: bool = False
    experimental: bool = False

    def disabled(self) -> tuple[str, ...]:
        return tuple(name for name in COMPONENTS if not getattr(self, name))

    def wp2_ablated(self) -> tuple[str, ...]:
        """WP2 controls whose semantics is not being applied."""
        return tuple(name for name in WP2_CONTROLS if _wp2_off(self, name))

    def as_dict(self) -> dict[str, object]:
        return {**{name: getattr(self, name) for name in COMPONENTS},
                "experimental": self.experimental}


PRODUCTION_POLICY = ProtocolPolicy()

_active: ContextVar[ProtocolPolicy] = ContextVar(
    "bioma_protocol_policy", default=PRODUCTION_POLICY
)


def active_policy() -> ProtocolPolicy:
    """The policy in force for the current context. Production by default."""
    return _active.get()


def _wp2_off(policy: ProtocolPolicy, control: str) -> bool:
    """True when the named WP2 control is ablated in this policy."""
    if control == "epistemic_role_conflation":
        return policy.epistemic_role_conflation
    return not getattr(policy, control)


def experimental_policy(**switches: bool) -> ProtocolPolicy:
    """Build an ablation policy that disables at least one component."""
    unknown = sorted(set(switches) - set(COMPONENTS) - set(WP2_CONTROLS))
    if unknown:
        raise ValueError("Unknown protocol policy switches: " + ", ".join(unknown))
    invalid = sorted(name for name, value in switches.items() if not isinstance(value, bool))
    if invalid:
        raise ValueError("Protocol policy switches must be booleans: " + ", ".join(invalid))
    policy = replace(PRODUCTION_POLICY, experimental=True, **switches)
    if not policy.disabled() and not policy.wp2_ablated():
        raise ValueError("An experimental policy must disable at least one component")
    return policy


def ablations() -> dict[str, ProtocolPolicy]:
    """One single-component ablation policy per protocol component."""
    return {
        name: replace(PRODUCTION_POLICY, experimental=True, **{name: False})
        for name in COMPONENTS
    }


def wp2_ablations() -> dict[str, ProtocolPolicy]:
    """One single-control ablation policy per WP2 semantic control."""
    return {
        name: replace(
            PRODUCTION_POLICY,
            experimental=True,
            **({name: True} if name == "epistemic_role_conflation" else {name: False}),
        )
        for name in WP2_CONTROLS
    }


@contextmanager
def use_policy(policy: ProtocolPolicy):
    """Activate an experimental policy for the duration of the block."""
    if not isinstance(policy, ProtocolPolicy):
        raise TypeError("use_policy expects a ProtocolPolicy")
    if not policy.experimental:
        raise ValueError("Only experimental protocol policies may be injected")
    token = _active.set(policy)
    try:
        yield policy
    finally:
        _active.reset(token)


def ablated(component: str) -> bool:
    """True when the named component is switched off in the active policy."""
    if component not in COMPONENTS:
        raise ValueError("Unknown protocol component: " + component)
    return not getattr(active_policy(), component)


def wp2_ablation_active(control: str) -> bool:
    """True when the named WP2 control is ablated in the active policy.

    Production always applies the full WP2 semantics, so this is ``False``
    unless an experimental policy has been explicitly activated.
    """
    if control not in WP2_CONTROLS:
        raise ValueError("Unknown WP2 control: " + control)
    return _wp2_off(active_policy(), control)
