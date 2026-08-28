"""Host-owned external specialist registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from athena.delegates.models import DelegateSpec


class DelegateRegistry:
    """Registry populated by trusted host configuration, never by the model."""

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

    def list(self) -> list[dict[str, Any]]:
        return [self._specs[key].to_record() for key in sorted(self._specs)]


__all__ = ["DelegateRegistry"]
