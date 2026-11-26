"""Explicit service boundary for canonical task admission assembly."""

from __future__ import annotations

from typing import Any

__all__ = ["TaskIntakePorts"]


class TaskIntakePorts:
    """Expose only configuration and service admission operations."""

    _RESOURCES = {"default_workspace": "_default_workspace", "config": "config"}
    _OPERATIONS = {
        "validate_request_metadata": "_validate_request_metadata",
        "normalize_acceptance": "_normalize_agent_request_acceptance",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        target = self._RESOURCES.get(name) or self._OPERATIONS.get(name)
        if target is None:
            raise AttributeError(f"task intake port is not allowed: {name}")
        return getattr(self._owner, target)
