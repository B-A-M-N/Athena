"""Explicit resources consumed by provider-outcome recovery."""

from __future__ import annotations

from typing import Any

__all__ = ["ProviderRecoveryPorts"]


class ProviderRecoveryPorts:
    """Late-bound stores and authorities exposed through an allowlist."""

    _RESOURCES = {
        "response_store": "_model_response_store",
        "health": "_provider_recovery_health",
        "budgets": "_budgets",
        "events": "_store_events",
        "task_manager": "_task_manager",
        "tasks": "_store_tasks",
        "kernel": "_kernel",
        "approval_recovery_tasks": "_approval_recovery_tasks",
        "log_background_failure": "_log_background_failure",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        target = self._RESOURCES.get(name)
        if target is None:
            raise AttributeError(f"provider recovery port is not allowed: {name}")
        return lambda: getattr(self._owner, target, None)
