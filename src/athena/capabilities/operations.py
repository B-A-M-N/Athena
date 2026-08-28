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

from collections.abc import Mapping
from typing import Any

from athena.protocol.capabilities import CapabilityDescriptor, EffectClass

__all__ = ["OPERATION_EFFECTS", "CapabilityEffectError", "resolve_operation_effects"]


class CapabilityEffectError(ValueError):
    """Unknown or unclassifiable operation — fail validation, no fallback."""


# operation -> exact effect set. Absent operation = not classified here.
OPERATION_EFFECTS: dict[str, dict[str, frozenset[EffectClass]]] = {
    "terminal_session": {
        "create": frozenset(
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS, EffectClass.WRITE_LOCAL}
        ),
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
        for op in (
            "overview",
            "cpu",
            "memory",
            "disk",
            "network",
            "ports",
            "toolchain",
            "services",
            "gpu",
            "env",
        )
    },
    "service": {
        "list": frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "status": frozenset(
            {EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
        ),
        "logs": frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "start": frozenset(
            {EffectClass.PRIVILEGED, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
        ),
        "stop": frozenset({EffectClass.PRIVILEGED, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "restart": frozenset(
            {EffectClass.PRIVILEGED, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
        ),
        "reload": frozenset(
            {EffectClass.PRIVILEGED, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
        ),
        "enable": frozenset(
            {
                EffectClass.PRIVILEGED,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
        "disable": frozenset(
            {
                EffectClass.PRIVILEGED,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
        "mask": frozenset(
            {
                EffectClass.PRIVILEGED,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
        "unmask": frozenset(
            {
                EffectClass.PRIVILEGED,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
            }
        ),
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
        "http": frozenset(
            {EffectClass.NETWORK_READ}
        ),  # GET/HEAD only; unsafe methods escalate below
        "tcp_connect": frozenset({EffectClass.NETWORK_READ}),
        "dns": frozenset({EffectClass.NETWORK_READ}),
        "listeners": frozenset({EffectClass.READ_LOCAL}),
        "connections": frozenset({EffectClass.READ_LOCAL}),
        "ping": frozenset({EffectClass.NETWORK_READ}),
    },
    "workspace": {
        "status": frozenset({EffectClass.READ_LOCAL}),
        "changed_files": frozenset({EffectClass.READ_LOCAL}),
        "profile": frozenset({EffectClass.READ_LOCAL}),
        "index": frozenset({EffectClass.READ_LOCAL}),
        "impact": frozenset({EffectClass.READ_LOCAL}),
        "snapshot": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "restore": frozenset({EffectClass.WRITE_LOCAL, EffectClass.DELETE}),
    },
    "diagnostics": {
        "normalize": frozenset({EffectClass.READ_LOCAL}),
        "failures": frozenset({EffectClass.READ_LOCAL}),
        "remember": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "git": {
        "status": frozenset({EffectClass.READ_LOCAL}),
        "diff": frozenset({EffectClass.READ_LOCAL}),
        "log": frozenset({EffectClass.READ_LOCAL}),
        "show": frozenset({EffectClass.READ_LOCAL}),
        "blame": frozenset({EffectClass.READ_LOCAL}),
        "branch": frozenset({EffectClass.READ_LOCAL}),
        "merge_base": frozenset({EffectClass.READ_LOCAL}),
        "baseline": frozenset({EffectClass.READ_LOCAL}),
    },
    "fs": {
        "read": frozenset({EffectClass.READ_LOCAL}),
        "list": frozenset({EffectClass.READ_LOCAL}),
        "stat": frozenset({EffectClass.READ_LOCAL}),
        "write": frozenset({EffectClass.WRITE_LOCAL}),
        "patch": frozenset({EffectClass.WRITE_LOCAL}),
        "mkdir": frozenset({EffectClass.WRITE_LOCAL}),
        "copy": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "move": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "delete": frozenset({EffectClass.DELETE}),
    },
    "memory": {
        "recall": frozenset({EffectClass.READ_LOCAL}),
        "search": frozenset({EffectClass.READ_LOCAL}),
        "save": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "context_blocks": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "read": frozenset({EffectClass.READ_LOCAL}),
        "history": frozenset({EffectClass.READ_LOCAL}),
        "create": frozenset({EffectClass.WRITE_LOCAL}),
        "update": frozenset({EffectClass.WRITE_LOCAL}),
        "attach": frozenset({EffectClass.WRITE_LOCAL}),
        "detach": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "capability_health": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "reset": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "packs": {
        "search": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "health": frozenset({EffectClass.READ_LOCAL}),
        "install": frozenset({EffectClass.WRITE_LOCAL}),
        "upgrade": frozenset({EffectClass.WRITE_LOCAL}),
        "enable": frozenset({EffectClass.WRITE_LOCAL}),
        "disable": frozenset({EffectClass.WRITE_LOCAL}),
        "uninstall": frozenset({EffectClass.DELETE}),
    },
    "delegate": {
        "spawn": frozenset({EffectClass.SPAWN_PROCESS}),
        "status": frozenset({EffectClass.READ_LOCAL}),
        "collect": frozenset({EffectClass.READ_LOCAL}),
        "cancel": frozenset({EffectClass.SPAWN_PROCESS}),
    },
    "delegate.external": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "start": frozenset({EffectClass.SPAWN_PROCESS, EffectClass.EXTERNAL_MESSAGE}),
        "send": frozenset({EffectClass.EXTERNAL_MESSAGE}),
        "status": frozenset({EffectClass.READ_LOCAL}),
        "close": frozenset({EffectClass.EXTERNAL_MESSAGE}),
    },
    "schedule": {
        "create": frozenset({EffectClass.WRITE_LOCAL}),
        "list": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "enable": frozenset({EffectClass.WRITE_LOCAL}),
        "disable": frozenset({EffectClass.WRITE_LOCAL}),
        "delete": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "maintain": {
        "create": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "list": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "enable": frozenset({EffectClass.WRITE_LOCAL}),
        "disable": frozenset({EffectClass.WRITE_LOCAL}),
        "delete": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "skills": {
        "search": frozenset({EffectClass.READ_LOCAL}),
        "trigger": frozenset({EffectClass.EXECUTE}),
    },
    "watch": {
        "file": frozenset({EffectClass.READ_LOCAL}),
        "process": frozenset({EffectClass.READ_LOCAL}),
        "list": frozenset({EffectClass.READ_LOCAL}),
        "stop": frozenset({EffectClass.READ_LOCAL}),
    },
    "capabilities": {
        "search": frozenset({EffectClass.READ_LOCAL}),
        "describe": frozenset({EffectClass.READ_LOCAL}),
        "dependencies": frozenset({EffectClass.READ_LOCAL}),
        "provenance": frozenset({EffectClass.READ_LOCAL}),
        "history": frozenset({EffectClass.READ_LOCAL}),
        "created_this_task": frozenset({EffectClass.READ_LOCAL}),
        "workflows": frozenset({EffectClass.READ_LOCAL}),
        "skills": frozenset({EffectClass.READ_LOCAL}),
        "runtimes": frozenset({EffectClass.READ_LOCAL}),
        "permissions": frozenset({EffectClass.READ_LOCAL}),
        "devices": frozenset({EffectClass.READ_LOCAL}),
        "availability": frozenset({EffectClass.READ_LOCAL}),
    },
    "dependency": {
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "resolve": frozenset({EffectClass.READ_LOCAL}),
        "install": frozenset(
            {
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_WRITE,
            }
        ),
    },
    "synthesis": {
        "create": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "repair": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "promote_scratch": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "candidates": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "promote": frozenset({EffectClass.WRITE_LOCAL}),
        "deprecate": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "scratch": {
        "run": frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
    },
    "research": {
        "record_source": frozenset({EffectClass.WRITE_LOCAL}),
        "fetch": frozenset({EffectClass.WRITE_LOCAL, EffectClass.NETWORK_READ}),
        "discover": frozenset({EffectClass.READ_LOCAL, EffectClass.NETWORK_READ}),
        "sources": frozenset({EffectClass.READ_LOCAL}),
        "search": frozenset({EffectClass.READ_LOCAL}),
        "record_evidence": frozenset({EffectClass.WRITE_LOCAL}),
        "evidence": frozenset({EffectClass.READ_LOCAL}),
        "record_gap": frozenset({EffectClass.WRITE_LOCAL}),
        "gaps": frozenset({EffectClass.READ_LOCAL}),
        "close_gap": frozenset({EffectClass.WRITE_LOCAL}),
        "verify": frozenset({EffectClass.READ_LOCAL}),
        "plan": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "assess": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "bundle": frozenset({EffectClass.READ_LOCAL}),
        # The bounded workflow may capture allowlisted sources, persist
        # evidence/gaps, and verify a task-scoped packet in one dispatch.
        "run": frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.NETWORK_READ,
            }
        ),
    },
    "artifacts": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "read": frozenset({EffectClass.READ_LOCAL}),
        "slice": frozenset({EffectClass.READ_LOCAL}),
        "search": frozenset({EffectClass.READ_LOCAL}),
    },
    "workflow": {
        "list": frozenset({EffectClass.READ_LOCAL}),
        "describe": frozenset({EffectClass.READ_LOCAL}),
        "create": frozenset({EffectClass.WRITE_LOCAL}),
        "promote": frozenset({EffectClass.WRITE_LOCAL}),
        "recover": frozenset({EffectClass.WRITE_LOCAL}),
        "run": frozenset(
            {
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
                EffectClass.NETWORK_WRITE,
            }
        ),
        "trial": frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.WRITE_LOCAL,
                EffectClass.DELETE,
            }
        ),
    },
    "debugger": {
        "launch": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "breakpoint": frozenset({EffectClass.EXECUTE}),
        "continue": frozenset({EffectClass.EXECUTE}),
        "pause": frozenset({EffectClass.EXECUTE}),
        "step": frozenset({EffectClass.EXECUTE}),
        "stack": frozenset({EffectClass.READ_LOCAL}),
        "variables": frozenset({EffectClass.READ_LOCAL}),
        "evaluate": frozenset({EffectClass.EXECUTE}),
        "detach": frozenset({EffectClass.EXECUTE}),
        "status": frozenset({EffectClass.READ_LOCAL}),
    },
    "execute": {
        "": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "execute": frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
    },
    "fusion": {
        "run": frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.DELETE,
            }
        ),
        "compare": frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.DELETE,
            }
        ),
        "status": frozenset({EffectClass.READ_LOCAL}),
        "commit": frozenset({EffectClass.WRITE_LOCAL, EffectClass.DELETE}),
        "discard": frozenset({EffectClass.WRITE_LOCAL, EffectClass.DELETE}),
        "fork": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "checkpoint": frozenset({EffectClass.READ_LOCAL, EffectClass.WRITE_LOCAL}),
        "inspect_checkpoint": frozenset({EffectClass.READ_LOCAL}),
        "release_checkpoint": frozenset({EffectClass.WRITE_LOCAL}),
    },
    "truth": {
        "status": frozenset({EffectClass.READ_LOCAL}),
        "explain": frozenset({EffectClass.READ_LOCAL}),
        "dependencies": frozenset({EffectClass.READ_LOCAL}),
        "stale": frozenset({EffectClass.READ_LOCAL}),
    },
    "observer": {
        "create": frozenset(
            {EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}
        ),
        "run": frozenset({EffectClass.READ_LOCAL, EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        "list": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
    },
    "capsule": {
        "export": frozenset({EffectClass.READ_LOCAL}),
        "inspect": frozenset({EffectClass.READ_LOCAL}),
        "import": frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
            }
        ),
        "run": frozenset(
            {
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.NETWORK_READ,
                EffectClass.NETWORK_WRITE,
                EffectClass.DELETE,
                EffectClass.PRIVILEGED,
            }
        ),
    },
}

_SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


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
    if descriptor.operation_effects is None and descriptor.effect_resolver is None:
        return ()
    try:
        effects = descriptor.resolve_effects(arguments)
    except ValueError as exc:
        raise CapabilityEffectError(f"capability {descriptor.id}: {exc}") from None
    return tuple(sorted(effects or (), key=lambda e: e.value))
