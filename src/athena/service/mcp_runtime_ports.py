"""Explicit MCP resource and lifecycle callback boundary."""

from __future__ import annotations

from typing import Any

__all__ = ["MCPRuntimePorts"]


class MCPRuntimePorts:
    """Allowlisted MCP resources, connection state, secrets, and callbacks."""

    _RESOURCE_NAMES = frozenset(
        {
            "_mcp_connection_status",
            "config",
            "_mcp_clients",
            "_mcp_resources",
            "_mcp_prompts",
            "_mcp_supervisor",
            "_mcp",
            "_secrets",
            "_mcp_client_factory",
            "_mcp_reconnect_failures",
        }
    )
    _OPERATION_NAMES = frozenset(
        {"_handle_mcp_transport_failure", "mcp_reconnect", "_live_capability_profile_status"}
    )

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        target = self._target(name)
        if target is None:
            raise AttributeError(f"MCP runtime port is not allowed: {name}")
        setattr(self._owner, target, value)

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"MCP runtime port is not allowed: {name}")
        return getattr(self._owner, target)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES or name in cls._OPERATION_NAMES:
            return name
        candidate = f"_{name}"
        return (
            candidate
            if candidate in cls._RESOURCE_NAMES or candidate in cls._OPERATION_NAMES
            else None
        )
