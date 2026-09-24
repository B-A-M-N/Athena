"""Explicit resource boundary used by service lifecycle phases."""

from __future__ import annotations

from typing import Any


class LifecyclePorts:
    """Explicit service-resource allowlist consumed by the lifecycle.

    This is the ownership contract: lifecycle may access only these named
    resources, not browse unrelated facade attributes. Values resolve lazily
    so lifecycle resources remain authoritative at call time. Callers use the
    public name (for example ``ports.started``); the leading underscore is an
    owner implementation detail retained only in this mapping.
    """

    _RESOURCE_NAMES = frozenset(
        {
            "_startup_health",
            "_synthesis",
            "_secrets",
            "_store_events",
            "_research_store",
            "_reality_gate",
            "_pack_manager",
            "_fabric",
            "_workflow_store",
            "_worker",
            "_watch_registry",
            "_task_manager",
            "_synthesis_event_observer",
            "_store_tasks",
            "_store_input_requests",
            "_started",
            "_skill_lifecycle",
            "_skill_discovery_status",
            "_scheduler",
            "_runtime_state_root",
            "_recovery_summary",
            "_recovery_status",
            "_recovery_error",
            "_memory",
            "_mcp_resources",
            "_mcp_prompts",
            "_mcp",
            "_knowledge",
            "_generated_store",
            "_forward_events",
            "_external_effect_store",
            "_execution",
            "_dispatcher",
            "_delivery",
            "_default_workspace",
            "_worker_task",
            "_watch_poll_task",
            "_tool_repair_store",
            "_steering_store",
            "_store_schedules",
            "_store_continuations",
            "_provider_usage_store",
            "_model_response_store",
            "_config",
            "_pack_hook_outbox",
            "_store_mutations",
            "_store_runtime_sessions",
            "_store_executions",
            "_pending_finalization_store",
            "_hermes_referee",
            "_hermes_adapter",
            "_scratch",
            "_db",
            "_configuration",
            "_register_core_capabilities",
            "_configure_hermes_referee",
            "_preflight_hermes_referee",
            "_close_hermes_referee",
            "_connect_mcp",
            "_validate_required_capabilities",
            "_reconcile_created_intake",
            "_sync_skills",
            "_refresh_skill_event",
            "_poll_watches",
            "_mark_execution_uncertain",
            "_run_watch_observer",
            "_on_mutation_completed",
            "_register_schedule_capabilities",
        }
    )
    _RESOURCE_NAMES |= frozenset(
        {
            "_artifacts",
            "_budgets",
            "_cancellations",
            "_capability_health",
            "_config",
            "_delivery",
            "_provider_recovery_health",
            "_quarantine_tasks_for_packs",
            "_recover_answered_input_requests",
            "_recover_approved_continuations",
            "_reconcile_created_intake",
            "_recovery_error",
            "_recovery_status",
            "_recovery_summary",
            "_registry",
            "_rehydrate_approval_grants",
            "config",
            "reconcile_provider_outcomes",
            "require_task_ready",
            "shadow_engine",
            "stop",
            "submit",
            "submit_spec",
            "_mcp_clients",
            "_policy",
            "_runtime_host_supervisor",
            "_schedule_api",
            "_skills",
            "_start_impl",
            "start_mcp_supervisor",
            # Startup composition and reasoning wiring consume these explicit
            # facade-owned resources through the same port. Keep this list
            # enumerated: adding a phase helper must make its dependency
            # visible in the lifecycle contract rather than silently restoring
            # unrestricted facade reach-through.
            "_acceptance_verifier",
            "_approval_recovery_tasks",
            "_background_tasks",
            "_browser",
            "_browser_health",
            "_build_model_router",
            "_build_verifier",
            "_capability_health_store",
            "_compiler",
            "_computer",
            "_computer_health",
            "_context_block_store",
            "_debugger",
            "_delegation",
            "_delegate_session_store",
            "_dispatch_factory",
            "_external_delegate_manager",
            "_failure_memory",
            "_hermes_referee_owned",
            "_kernel",
            "_make_interpreter",
            "_make_judge_broker",
            "_make_model_summarizer",
            "_mcp_connection_status",
            "_mcp_supervisor",
            "_model_registry",
            "_observation_callbacks",
            "_project_index_for_completion",
            "_project_profile_for_completion",
            "_reality_coordinator",
            "_register_providers",
            "_resource_finalizer",
            "_resource_obligation_store",
            "_router",
            "_run_shutdown_hooks",
            "_self_host_missions",
            "_shutdown_status",
            "_sessions",
            "_steering_store",
            "_store_approvals",
            "_task_manager",
            "_terminals",
            "_verification_evidence",
            "_voice",
            "_workflow_run_store",
            "_workspace_reader",
            "_world_state_store",
            "_world_states",
            "_project_index_store",
            "_project_index_builder",
            "_project_index_coordinator",
            "_store_executions",
            "_store_messages",
            "_store_mutations",
            "_store_runtime_sessions",
            "_pack_store",
        }
    )

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    def __setattr__(self, name: str, value: Any) -> None:
        """Write allowlisted lifecycle resources back to their owner.

        Startup phases mutate the façade as they acquire and bind resources.
        A read-only proxy would make those assignments land on the proxy and
        silently split ownership, so writes use the same explicit allowlist as
        reads while preserving the façade as the sole state owner.
        """
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        target = self._target(name)
        if target is None:
            raise AttributeError(f"lifecycle port is not allowed: {name}")
        setattr(self._owner, target, value)

    @property
    def owner(self) -> Any:
        """Narrow application dispatch boundary for startup coordination."""
        return self._owner

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"lifecycle port is not allowed: {name}")
        return getattr(self._owner, target, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None

    @property
    def startup(self) -> "StartupPorts":
        """Narrow acquisition/binding boundary used by startup phases."""
        return StartupPorts(self._owner)


class StartupPorts:
    """Explicit startup-only resource set with owner-preserving writes."""

    _RESOURCE_NAMES = frozenset(
        {
            "_startup_health",
            "_shutdown_status",
            "_recovery_status",
            "_recovery_summary",
            "_recovery_error",
            "_default_workspace",
            "_db",
            "_runtime_state_root",
            "_sessions",
            "_store_tasks",
            "_store_events",
            "_store_messages",
            "_store_approvals",
            "_store_mutations",
            "_external_effect_store",
            "_resource_obligation_store",
            "_pending_finalization_store",
            "_store_schedules",
            "_store_continuations",
            "_store_input_requests",
            "_steering_store",
            "_world_state_store",
            "_workflow_store",
            "_workflow_run_store",
            "_generated_store",
            "_research_store",
            "_provider_usage_store",
            "_model_response_store",
            "_project_index_store",
            "_project_index_builder",
            "_project_index_coordinator",
            "_failure_memory",
            "_execution",
            "_runtime_host_supervisor",
            "_store_runtime_sessions",
            "_store_executions",
            "_self_host_missions",
            "_tool_repair_store",
            "_context_block_store",
            "_pack_store",
            "_pack_hook_outbox",
            "_pack_manager",
            "_delegate_session_store",
            "_capability_health_store",
            "_capability_health",
            "_secrets",
            "_scheduler",
            "_skills",
            "_skill_lifecycle",
            "_skill_discovery_status",
            "_mcp",
            "_mcp_resources",
            "_mcp_prompts",
            "_worker",
            "_worker_task",
            "_watch_poll_task",
            "_register_core_capabilities",
            "_recover_approved_continuations",
            "_recover_answered_input_requests",
            "_configure_hermes_referee",
            "_preflight_hermes_referee",
            "_forward_events",
            "_connect_mcp",
            "start_mcp_supervisor",
            "_poll_watches",
            "_started",
            "config",
        }
    )

    def __init__(self, owner: Any) -> None:
        object.__setattr__(self, "_owner", owner)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        target = self._target(name)
        if target is None:
            raise AttributeError(f"startup port is not allowed: {name}")
        setattr(self._owner, target, value)

    def __getattr__(self, name: str) -> Any:
        target = self._target(name)
        if target is None:
            raise AttributeError(f"startup port is not allowed: {name}")
        return getattr(self._owner, target, None)

    @classmethod
    def _target(cls, name: str) -> str | None:
        if name in cls._RESOURCE_NAMES:
            return name
        candidate = f"_{name}"
        return candidate if candidate in cls._RESOURCE_NAMES else None
