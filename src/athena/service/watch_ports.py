"""Explicit resources consumed by service watcher mechanics."""

from __future__ import annotations

from typing import Any

__all__ = ["WatchPorts"]


class WatchPorts:
    """Read-only watcher resources exposed through an explicit allowlist."""

    _RESOURCES = {
        "dispatcher": "_dispatcher",
        "require_events": "_require_events",
        "default_workspace": "_default_workspace",
        "world_states": "_world_states",
        "project_index_coordinator": "_project_index_coordinator",
        "world_state_store": "_world_state_store",
        "watch_registry": "_watch_registry",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        target = self._RESOURCES.get(name)
        if target is None:
            raise AttributeError(f"watch port is not allowed: {name}")
        return getattr(self._owner, target, None)
