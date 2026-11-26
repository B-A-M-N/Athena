"""Explicit live-resource boundary for task-resource finalization."""

from __future__ import annotations

from typing import Any


class ResourceFinalizerPorts:
    """Expose only task-owned resources and durable recovery facts."""

    _RESOURCE_NAMES = frozenset(
        {
            "_browser",
            "_checkpoints",
            "_debugger",
            "_execution",
            "_external_delegate_manager",
            "_store_tasks",
            "_synthesis",
            "_task_manager",
            "_terminals",
        }
    )

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"resource finalizer port is not allowed: {name}")
        return getattr(self._owner, target, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None


__all__ = ["ResourceFinalizerPorts"]
