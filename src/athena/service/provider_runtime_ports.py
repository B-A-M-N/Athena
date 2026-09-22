"""Explicit configuration/credential boundary for provider construction."""

from __future__ import annotations

from typing import Any

__all__ = ["ProviderRuntimePorts"]


class ProviderRuntimePorts:
    """Expose only provider configuration and credential resolution state."""

    _RESOURCES = {"config": "config", "secrets": "_secrets"}

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        target = self._RESOURCES.get(name)
        if target is None:
            raise AttributeError(f"provider runtime port is not allowed: {name}")
        return getattr(self._owner, target)
