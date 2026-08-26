"""Capability-owned operation effect resolution (P0-9).

Security semantics belong to the capability CONTRACT, not to English
operation-name heuristics in the dispatcher. Each deep capability declares
an explicit operation -> frozenset(EffectClass) map; the dispatcher calls
``resolve_operation_effects(descriptor, arguments)`` and unknown operations
fail validation rather than falling through to a generic effect.

Every native capability with operation branching registers its map here.
The security test suite asserts the exact effects PolicyEngine sees per
operation, so a misclassification is caught as a test failure.
"""

from __future__ import annotations

from typing import Any, Mapping

from athena.protocol.capabilities import CapabilityDescriptor, EffectClass

__all__ = ["resolve_operation_effects", "OPERATION_EFFECTS", "CapabilityEffectError"]


class CapabilityEffectError(ValueError):
    """Unknown or unclassifiable operation — fail validation, no fallback."""


# operation -> exact effect set. Absent operation = not classified here.
OPERATION_EFFECTS: dict[str, dict[str, frozenset[EffectClass]]] = {
    "terminal_session": {
        "create": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS,
                             EffectClass.WRITE_LOCAL}),
        "screen": frozenset({EffectClass.READ_LOCAL}),
        "write": frozenset({EffectClass.WRITE_LOCAL, EffectClass.EXECUTE}),
        "send": frozenset({EffectClass.WRITE_LOCAL, EffectClass.EXECUTE}),
        "keys": frozenset({EffectClass.WRITE_LOCAL, EffectClass.EXECUTE}),
        "wait_for": frozenset({EffectClass.READ_LOCAL}),
        "list": frozenset({EffectClass.READ_LOCAL}),
        "resize": frozenset({EffectClass.WRITE_LOCAL}),
        "kill": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
    },
    "process": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "tree": frozenset({EffectClass.READ_LOCAL}),
        "usage": frozenset({EffectClass.READ_LOCAL}),
        "write_stdin": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "signal": frozenset({EffectClass.EXECUTE, EffectClass.PRIVILEGED}),
        "wait": frozenset({EffectClass.READ_LOCAL}),
    },
    "machine": {
        op: frozenset({EffectClass.READ_LOCAL})
        for op in ("overview", "cpu", "memory", "disk", "network", "ports",
                   "toolchain", "services", "gpu")
    },
    "service": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "status": frozenset({EffectClass.READ_LOCAL}),
        "logs": frozenset({EffectClass.READ_LOCAL}),
        "start": frozenset({EffectClass.PRIVILEGED, EffectClass.EXECUTE}),
        "stop": frozenset({EffectClass.PRIVILEGED, EffectClass.EXECUTE}),
        "restart": frozenset({EffectClass.PRIVILEGED, EffectClass.EXECUTE}),
        "reload": frozenset({EffectClass.PRIVILEGED, EffectClass.EXECUTE}),
        "enable": frozenset({EffectClass.PRIVILEGED, EffectClass.WRITE_LOCAL}),
        "disable": frozenset({EffectClass.PRIVILEGED, EffectClass.WRITE_LOCAL}),
    },
    "database": {
        "tables": frozenset({EffectClass.READ_LOCAL}),
        "schema": frozenset({EffectClass.READ_LOCAL}),
        # SQL text can mutate regardless of the verb label; treat as write
        # unless proved read-only by the statement classifier.
        "query": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "explain": frozenset({EffectClass.READ_LOCAL}),
        "execute": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "network": {
        "http": frozenset({EffectClass.NETWORK_READ}),   # GET/HEAD only; unsafe methods escalate below
        "tcp_connect": frozenset({EffectClass.NETWORK_READ}),
        "dns": frozenset({EffectClass.NETWORK_READ}),
        "listeners": frozenset({EffectClass.READ_LOCAL}),
        "connections": frozenset({EffectClass.READ_LOCAL}),
        "ping": frozenset({EffectClass.NETWORK_READ}),
    },
    "workspace": {
        "status": frozenset({EffectClass.READ_LOCAL}),
        "changed_files": frozenset({EffectClass.READ_LOCAL}),
        "snapshot": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "restore": frozenset({EffectClass.WRITE_LOCAL, EffectClass.DELETE}),
    },
}

_UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def resolve_operation_effects(
    descriptor: CapabilityDescriptor,
    arguments: Mapping[str, Any],
) -> tuple[EffectClass, ...]:
    """Resolve the exact effect set from the capability's own contract.

    Returns () when the descriptor has no operation map (legacy/simple
    capabilities keep the existing heuristic path). Raises
    CapabilityEffectError when an operation cannot be classified — the
    caller must reject the call instead of guessing effects.
    """
    op_map = OPERATION_EFFECTS.get(descriptor.id)
    if op_map is None:
        return ()
    op = str(arguments.get("operation") or arguments.get("action") or "").lower()
    if op not in op_map:
        raise CapabilityEffectError(
            f"capability {descriptor.id}: operation {op!r} has no declared "
            f"effect classification")
    effects = set(op_map[op])

    # Contract-level refinements.
    if descriptor.id == "network" and op == "http":
        method = str(arguments.get("method") or "GET").upper()
        if method in _UNSAFE_HTTP_METHODS:
            effects.add(EffectClass.NETWORK_WRITE)

    # Every resolved effect must be within the descriptor's declared ceiling.
    outside = effects - set(descriptor.effects)
    if outside:
        raise CapabilityEffectError(
            f"capability {descriptor.id}: operation {op!r} requires effects "
            f"{sorted(e.value for e in outside)} beyond its declared envelope")

    return tuple(sorted(effects, key=lambda e: e.value))
