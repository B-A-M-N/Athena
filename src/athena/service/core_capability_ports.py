"""Explicit resource boundary for canonical core-capability wiring."""

from __future__ import annotations

from typing import Any


class CoreCapabilityPorts:
    """Expose only resources and application operations used by registration.

    Core capability registration is a wiring mechanism, not a service owner.
    Public names keep the façade's private storage layout out of the wiring
    module while writes still land on the façade that owns the state.
    """

    _RESOURCE_NAMES = frozenset(
        {
            "_artifacts",
            "_browser",
            "_browser_health",
            "_candidate_git_view",
            "_capability_health",
            "_checkpoints",
            "_cleanup_task_affordances",
            "_cleanup_task_watches",
            "_computer",
            "_computer_health",
            "_context_block_store",
            "_database",
            "_debugger",
            "_delegate_registry",
            "_delegate_session_store",
            "_delegation",
            "_device_provider",
            "_dispatcher",
            "_execution",
            "_external_delegate_manager",
            "_external_effect_store",
            "_fabric",
            "_failure_memory",
            "_forward_events",
            "_knowledge",
            "_model_registry",
            "_on_mutation_completed",
            "_optional_capability_health",
            "_pack_manager",
            "_policy",
            "_processes",
            "_project_index_coordinator",
            "_project_index_store",
            "_reality_gate",
            "_require_events",
            "_research_store",
            "_resource_finalizer",
            "_run_watch_observer",
            "_runtime_state_root",
            "_scratch",
            "_secrets",
            "_store_approvals",
            "_store_input_requests",
            "_store_messages",
            "_store_mutations",
            "_synthesis",
            "_task_manager",
            "_terminals",
            "_watch_registry",
            "_watches",
            "_workflow_run_store",
            "_workflow_store",
            "config",
            "mcp_status",
            "runtime_health",
            "register_shutdown_hook",
        }
    )

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    @property
    def owner(self) -> Any:
        """Narrow owner handoff for capabilities that retain compatibility APIs."""
        return self._owner

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        target = self._target(name)
        if target is None:
            raise AttributeError(f"core capability port is not allowed: {name}")
        setattr(self._owner, target, value)

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"core capability port is not allowed: {name}")
        return getattr(self._owner, target, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None


__all__ = ["CoreCapabilityPorts"]
