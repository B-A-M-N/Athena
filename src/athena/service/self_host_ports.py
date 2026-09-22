"""Explicit application/resource ports for self-host mission mechanics."""

from __future__ import annotations

from typing import Any


class SelfHostPorts:
    """Allowlisted resources and application operations for self-host."""

    _RESOURCE_NAMES = {
        "self_host_missions": "_self_host_missions",
        "kernel": "_kernel",
        "store_tasks": "_store_tasks",
        "project_index_coordinator": "_project_index_coordinator",
        "hermes_referee": "_hermes_referee",
        "hermes_supervision_active": "_hermes_supervision_active",
        "hermes_supervision_mode": "_hermes_supervision_mode",
        "acceptance_verifier": "_acceptance_verifier",
    }
    _APPLICATION_OPERATIONS = {
        "require_agent_ready": "require_agent_ready",
        "require_verified_hermes_referee": "_require_verified_hermes_referee",
        "require_task_manager": "_require_task_manager",
        "build_task_spec": "_build_task_spec",
        "record_canonical_user_turn": "_record_canonical_user_turn",
        "_require_verified_hermes_referee": "_require_verified_hermes_referee",
        "_require_task_manager": "_require_task_manager",
        "shadow_engine": "shadow_engine",
        "_build_task_spec": "_build_task_spec",
        "_record_canonical_user_turn": "_record_canonical_user_turn",
        "wait_for": "wait_for",
        "operator_candidate": "operator_candidate",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    @property
    def owner(self) -> Any:
        """Narrow task/review/shadow application dispatch boundary."""
        return self._owner

    def __getattr__(self, name: str) -> Any:
        resource_name = self._RESOURCE_NAMES.get(name)
        if resource_name is not None:
            return getattr(self._owner, resource_name, None)
        operation_name = self._APPLICATION_OPERATIONS.get(name)
        if operation_name is None:
            raise AttributeError(f"self host port is not allowed: {name}")
        return getattr(self._owner, operation_name)


__all__ = ["SelfHostPorts"]
