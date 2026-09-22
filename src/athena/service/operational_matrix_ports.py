"""Explicit read-only resources for the operator backend matrix."""

from __future__ import annotations

from typing import Any


class OperationalMatrixPorts:
    """Expose execution readiness and canonical service projections."""

    _RESOURCE_NAMES = frozenset({"_execution", "mcp_status", "runtime_health"})

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"operational matrix port is not allowed: {name}")
        return getattr(self._owner, target, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None


__all__ = ["OperationalMatrixPorts"]
