"""Project-sensitivity and opaque-execution classification for RealityGate.

Subordinate to :class:`athena.reality.gate.RealityGate`. This module owns
static allowlists and request-level risk predicates; the gate owns routing,
checkpoint management, and recovery decisions.
"""

from __future__ import annotations

__all__ = [
    "ISOLATABLE_OPERATIONS",
    "PROCESS_CAPABILITIES",
    "PROJECT_CAPABILITIES",
    "READ_ONLY_OPERATIONS",
]


READ_ONLY_OPERATIONS = frozenset(
    {
        "read",
        "list",
        "stat",
        "screen",
        "wait_for",
        "status",
        "inspect",
        "tree",
        "usage",
        "tables",
        "schema",
        "explain",
        "search",
        "recall",
        "sources",
        "evidence",
        "gaps",
        "describe",
        "dependencies",
        "provenance",
        "history",
        "created_this_task",
        "workflows",
        "skills",
        "runtimes",
        "permissions",
        "devices",
        "changed_files",
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
    }
)

PROCESS_CAPABILITIES = frozenset(
    {
        "execute",
        "terminal_session",
        "debugger",
        "process",
        "shell",
        "bash",
    }
)

PROJECT_CAPABILITIES = frozenset(
    {
        "database",
        "dependency",
        "fs",
        "workspace",
        "workflow",
        "scratch",
    }
)

# Ops whose failure mode is a single, reversible local change are the
# cheapest safe thing to isolate: one ephemeral shadow, discarded alone.
ISOLATABLE_OPERATIONS = frozenset(
    {
        "write",
        "create",
        "mkdir",
        "touch",
        "delete",
        "remove",
        "rename",
        "move",
        "copy",
        "patch",
        "append",
        "update",
        "install",
        "add",
    }
)
