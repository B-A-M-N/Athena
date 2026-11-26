"""Explicit shared mutable runtime state for pack lifecycle mechanisms.

This object replaces owner-tunnelled mutable fields. Composition owns one
state object and passes it to mechanisms; mechanisms mutate exactly these
fields instead of reaching through the whole manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["PackHookRuntimeState", "PackRuntimeState"]


@dataclass
class PackHookRuntimeState:
    """Mutable hook runtime state shared by PackManager and PackHookRuntime."""

    callbacks: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    events_seen: set[str] = field(default_factory=set)
    outbox: Any | None = None
    retry_task: Any | None = None
    health: dict[str, Any] = field(
        default_factory=lambda: {
            "state": "stopped",
            "last_success_at": None,
            "last_error_at": None,
            "last_error": None,
            "iterations": 0,
        }
    )


@dataclass
class PackRuntimeState:
    """All mutable pack runtime state shared across extracted mechanisms."""

    hooks: PackHookRuntimeState = field(default_factory=PackHookRuntimeState)
