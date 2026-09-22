"""Explicit read-only resources used by the live health projection."""

from __future__ import annotations

from typing import Any


class HealthPorts:
    """Allowlist health facts and read-only service operations."""

    _RESOURCE_NAMES = frozenset(
        {
            "_browser",
            "_browser_health",
            "_computer",
            "_computer_health",
            "_memory",
            "_model_registry",
            "_optional_capability_health",
            "_provider_recovery_health",
            "_recovery_error",
            "_recovery_status",
            "_recovery_summary",
            "_resource_finalizer",
            "_scheduler",
            "_shutdown_status",
            "_watch_registry",
            "_live_capability_profile_status",
            "mcp_status",
        }
    )

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"health port is not allowed: {name}")
        return getattr(self._owner, target, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None


__all__ = ["HealthPorts"]
