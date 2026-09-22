"""Explicit startup phases for the service lifecycle.

The runner coordinates acquisition and binding only. Durable stores, execution,
policy, dispatch, scheduling, and task work remain owned by the authorities it
constructs and wires through :class:`ServiceLifecycle`.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from athena.mcp.adapter import MCPAdapter
from athena.protocol.tasks import WorkspaceSpec
from athena.service.reasoning_components import build_reasoning_components
from athena.service.startup_state import acquire_store_components
from athena.service.startup_execution import build_execution_components
from athena.tasks.worker import TaskWorker, WorkerConfig


class StartupPhaseRunner:
    """Run service startup as named, ordered, unwindable mechanisms."""

    def __init__(self, lifecycle: Any) -> None:
        self._lifecycle = lifecycle
        self._ports = lifecycle.ports.startup

    async def run(self) -> None:
        cfg, workspace = self._initialize()
        components = await acquire_store_components(
            db_path=cfg.db_path,
            startup_health=self._ports.startup_health,
        )
        self._bind_state(components)
        self._configure_secrets()
        self._ports.configure_hermes_referee()
        await self._ports.preflight_hermes_referee()

        execution_components = await build_execution_components(
            db=components.db,
            config=cfg,
            runtime_state_root=components.runtime_state_root,
            event_sink=self._ports.forward_events(components.events),
            secret_manager=self._ports.secrets,
            startup_health=self._ports.startup_health,
        )
        self._bind_execution(execution_components)
        execution = execution_components.execution
        authorities = await self._lifecycle._build_authorities(
            store_components=components,
            execution=execution,
        )
        observers = await self._lifecycle._build_finalize_observers(
            store_components=components,
        )
        authorities.task_manager.add_finalize_observer(observers.knowledge)
        authorities.task_manager.add_finalize_observer(observers.delivery)
        reasoning = await build_reasoning_components(
            self._lifecycle,
            cfg=cfg,
            components=components,
            execution=execution,
            authorities=authorities,
        )
        await self._register_capabilities(
            workspace=workspace,
            components=components,
            authorities=authorities,
            execution=execution,
        )
        await self._recover_and_reconcile(
            coordinator=reasoning["coordinator"],
            finalizer=reasoning["finalizer"],
            kernel=reasoning["kernel"],
            task_manager=authorities.task_manager,
            components=components,
        )
        await self._start_interfaces(
            registry=authorities.registry,
            workspace=workspace,
            task_manager=authorities.task_manager,
        )
        await self._start_workers(
            cfg=cfg,
            scheduler=self._ports.scheduler,
            task_manager=authorities.task_manager,
            kernel=reasoning["kernel"],
        )
        self._finish_health()

    def _initialize(self) -> tuple[Any, WorkspaceSpec]:
        cfg = self._ports.config
        self._ports.startup_health = {
            "status": "starting",
            "checks": {},
            "blocking_failures": [],
        }
        self._ports.shutdown_status = {"state": "running"}
        self._ports.recovery_status = "starting"
        self._ports.recovery_summary = {}
        self._ports.recovery_error = None
        workspace = WorkspaceSpec(
            id="root",
            root=cfg.workspace_root or self._ports.default_workspace.root,
        )
        self._ports.default_workspace = workspace
        return cfg, workspace

    def _bind_state(self, components: Any) -> None:
        bindings = {
            "_db": components.db,
            "_runtime_state_root": components.runtime_state_root,
            "_sessions": components.sessions,
            "_store_tasks": components.tasks,
            "_store_events": components.events,
            "_store_messages": components.messages,
            "_store_approvals": components.approvals,
            "_store_mutations": components.mutations,
            "_external_effect_store": components.external_effect_store,
            "_resource_obligation_store": components.resource_obligation_store,
            "_pending_finalization_store": components.pending_finalization_store,
            "_store_schedules": components.schedules,
            "_store_continuations": components.continuations,
            "_store_input_requests": components.input_requests,
            "_steering_store": components.steering_store,
            "_world_state_store": components.world_state_store,
            "_workflow_store": components.workflow_store,
            "_workflow_run_store": components.workflow_run_store,
            "_generated_store": components.generated_store,
            "_research_store": components.research_store,
            "_provider_usage_store": components.provider_usage_store,
            "_model_response_store": components.model_response_store,
            "_project_index_store": components.project_index_store,
            "_project_index_builder": components.project_index_builder,
            "_project_index_coordinator": components.project_index_coordinator,
            "_failure_memory": components.failure_memory,
        }
        for attr, value in bindings.items():
            setattr(self._ports, attr.removeprefix("_"), value)

    def _configure_secrets(self) -> None:
        from athena.policy.credentials import BitwardenSource, OnePasswordSource, SecretManager

        self._ports.secrets = SecretManager()
        vault = os.environ.get("ATHENA_1PASSWORD_VAULT", "").strip()
        if vault:
            self._ports.secrets.register_source(OnePasswordSource(vault=vault))
        if os.environ.get("ATHENA_BITWARDEN_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            self._ports.secrets.register_source(BitwardenSource())

    def _bind_execution(self, components: Any) -> None:
        for attr, value in {
            "_execution": components.execution,
            "_runtime_host_supervisor": components.runtime_host_supervisor,
            "_store_runtime_sessions": components.runtime_sessions,
            "_store_executions": components.execution_store,
            "_self_host_missions": components.self_host_missions,
            "_tool_repair_store": components.tool_repair_store,
            "_context_block_store": components.context_block_store,
            "_pack_store": components.pack_store,
            "_pack_hook_outbox": components.pack_hook_outbox,
            "_pack_manager": components.pack_manager,
            "_delegate_session_store": components.delegate_session_store,
            "_capability_health_store": components.capability_health_store,
            "_capability_health": components.capability_health,
        }.items():
            setattr(self._ports, attr.removeprefix("_"), value)

    async def _register_capabilities(
        self,
        *,
        workspace: WorkspaceSpec,
        components: Any,
        authorities: Any,
        execution: Any,
    ) -> None:
        await self._ports.register_core_capabilities(
            registry=authorities.registry,
            workspace=workspace,
            execution=execution,
            memory=authorities.memory,
            skills_store=authorities.skills,
            research_store=components.research_store,
        )
        await self._lifecycle._register_generated_capabilities(
            registry=authorities.registry,
            fabric=authorities.fabric,
            workspace=workspace,
            events=components.events,
        )
        await self._lifecycle._register_schedule_capabilities(
            registry=authorities.registry,
            workspace=workspace,
            task_manager=authorities.task_manager,
        )

    async def _recover_and_reconcile(
        self,
        *,
        coordinator: Any,
        finalizer: Any,
        kernel: Any,
        task_manager: Any,
        components: Any,
    ) -> None:
        await self._lifecycle._run_startup_recovery(
            coordinator=coordinator,
            finalizer=finalizer,
        )
        await self._lifecycle._reconcile_provider_sagas()
        await self._lifecycle._reconcile_external_sagas()
        await self._ports.recover_approved_continuations(
            continuations=components.continuations,
            task_store=components.tasks,
            task_manager=task_manager,
            kernel=kernel,
        )
        if components.input_requests is not None:
            await self._ports.recover_answered_input_requests(
                input_requests=components.input_requests,
                task_store=components.tasks,
                task_manager=task_manager,
                kernel=kernel,
            )

    async def _start_interfaces(
        self,
        *,
        registry: Any,
        workspace: WorkspaceSpec,
        task_manager: Any,
    ) -> None:
        self._ports.mcp = MCPAdapter(registry)
        from athena.capabilities.mcp_context import MCPContextCapability

        from athena.mcp.prompts import MCPPromptProvider
        from athena.mcp.resources import MCPResourceProvider

        self._ports.mcp_resources = MCPResourceProvider()
        self._ports.mcp_prompts = MCPPromptProvider()
        await self._ports.connect_mcp()
        registry.register(MCPContextCapability(self._ports.mcp_resources, self._ports.mcp_prompts))
        await self._ports.start_mcp_supervisor()
        if self._ports.pack_manager is not None:
            self._lifecycle._bind_pack_lifecycle(
                skill_lifecycle=self._ports.skill_lifecycle,
                workspace=workspace,
                events=self._ports.store_events,
                task_manager=task_manager,
            )
            await self._lifecycle._rehydrate_enabled_packs(task_manager=task_manager)
        await self._lifecycle._validate_startup_readiness()

    async def _start_workers(
        self,
        *,
        cfg: Any,
        scheduler: Any,
        task_manager: Any,
        kernel: Any,
    ) -> None:
        worker = TaskWorker(
            task_manager=task_manager,
            kernel=kernel,
            config=WorkerConfig(
                max_parallel=cfg.max_parallel_tasks,
                lease_duration_seconds=cfg.worker_lease_duration_seconds,
                lease_renewal_divisor=cfg.worker_lease_renewal_divisor,
            ),
        )
        self._ports.worker = worker
        task_manager.set_wakeup_callback(worker.notify)
        self._ports.worker_task = asyncio.create_task(worker.run_forever())
        await scheduler.start()
        self._ports.watch_poll_task = asyncio.create_task(self._ports.poll_watches())

    def _finish_health(self) -> None:
        self._ports.started = True
        checks = self._ports.startup_health["checks"]
        values = [value for value in checks.values() if isinstance(value, dict)]
        self._ports.startup_health["status"] = (
            "degraded" if any(value.get("status") != "ok" for value in values) else "ok"
        )
        self._ports.startup_health["blocking_failures"] = [
            name
            for name, value in checks.items()
            if isinstance(value, dict) and value.get("blocking") and value.get("status") != "ok"
        ]


__all__ = ["StartupPhaseRunner"]
