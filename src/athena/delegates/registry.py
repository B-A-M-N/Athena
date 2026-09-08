"""Host-owned external specialist registry."""

from __future__ import annotations

from collections.abc import Callable
import os
import shutil
from typing import Any

from athena.delegates.models import DelegateSpec


class DelegateRegistry:
    """Registry populated by trusted host configuration, never by the model."""

    BUILTIN_PROTOCOLS = frozenset({"json_lines"})

    def __init__(self) -> None:
        self._specs: dict[str, DelegateSpec] = {}
        self._connectors: dict[str, Callable[..., Any]] = {}

    def register(self, spec: DelegateSpec, *, connector: Callable[..., Any] | None = None) -> None:
        if spec.id in self._specs:
            raise ValueError(f"delegate already registered: {spec.id}")
        if connector is None and not spec.command:
            raise ValueError("endpoint delegates require a trusted connector")
        self._specs[spec.id] = spec
        if connector is not None:
            self._connectors[spec.id] = connector

    def get(self, delegate_id: str) -> DelegateSpec:
        try:
            return self._specs[delegate_id]
        except KeyError as exc:
            raise KeyError(f"unknown external delegate: {delegate_id}") from exc

    def connector_for(self, delegate_id: str):
        return self._connectors.get(delegate_id)

    def preflight(self, delegate_id: str) -> dict[str, Any]:
        """Check that a configured delegate can actually be launched.

        Registration proves only that host configuration is syntactically
        present. Admission needs a live, cheap check of the executable or
        trusted connector so a required delegate cannot disappear behind a
        successful startup configuration check.
        """
        spec = self.get(delegate_id)
        connector = self.connector_for(delegate_id)
        protocol = spec.protocol_value
        built_in = protocol in self.BUILTIN_PROTOCOLS
        connector_protocols = _declared_connector_protocols(spec, connector)
        connector_ready = (
            connector is not None and callable(connector) and protocol in connector_protocols
        )
        protocol_supported = built_in or connector_ready
        executable: str | None = None
        if spec.command:
            command = str(spec.command[0])
            if os.path.isabs(command) or os.sep in command:
                executable = command if os.access(command, os.X_OK) else None
            else:
                executable = shutil.which(command)
        executable_ready = executable is not None
        available = connector_ready or (built_in and executable_ready)
        return {
            "id": spec.id,
            "protocol": protocol,
            "configured": True,
            "connector_ready": connector_ready,
            "built_in": built_in,
            "connector_protocols": sorted(connector_protocols),
            "executable": executable,
            "executable_ready": executable_ready,
            "protocol_supported": protocol_supported,
            "available": available and protocol_supported,
            "reason": (
                None
                if available and protocol_supported
                else (
                    "unsupported delegate protocol"
                    if not protocol_supported
                    else "delegate executable is unavailable"
                    if spec.command
                    else "delegate connector must explicitly declare this protocol"
                )
            ),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._specs[key].to_record() for key in sorted(self._specs)]


__all__ = ["DelegateRegistry"]


def _declared_connector_protocols(
    spec: DelegateSpec, connector: Callable[..., Any] | None
) -> set[str]:
    """Read an explicit host declaration; never infer protocol from a name."""
    values: Any = spec.metadata.get("connector_protocols")
    if values is None:
        values = spec.metadata.get("supported_protocols")
    if values is None:
        values = getattr(connector, "protocols", None)
    if values is None:
        value = getattr(connector, "protocol", None)
        values = (value,) if value is not None else ()
    if isinstance(values, str):
        values = (values,)
    return {str(getattr(value, "value", value)) for value in values or ()}
