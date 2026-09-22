"""AthenaService — the application composition root and interface-neutral API.

Per BUILDSPEC §94 and §7, ``AthenaService`` sits ABOVE the core services
(``TaskManager`` / kernel / stores / ...) and BELOW the interfaces (CLI / ACP /
HTTP). It is the single application entrypoint that wires the whole runtime
together and exposes the application API surfaces know.

The service is the composition root:
    * it constructs every subsystem in dependency order;
    * it does NOT run an agent loop (INV-001) — it turns work into Tasks that
      run through :class:`TaskManager` / :class:`TaskWorker` / the kernel;
    * it does NOT keep an independent session store (INV-003) — it uses
      :class:`SessionRepository`;
    * it exposes a clean, interface-neutral API observable by any client.

``AthenaService.start()`` opens the database, applies migrations, constructs all
subsystems, registers the core capabilities, registers model providers, loads
skills, connects configured MCP servers (best-effort) and starts the scheduler
background loop. ``AthenaService.stop()`` shuts down in reverse order.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from athena.artifacts.store import ArtifactStore
from athena.affordances import (
    CapabilityFabric,
    GeneratedCapabilityStore,
    ScratchManager,
)
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.context.compiler import ContextCompiler
from athena.execution.manager import ExecutionManager
from athena.state.external_effects import ExternalEffectStore
from athena.execution.environment import VerificationEnvironment
from athena.interpreter import InterpreterExtension
from athena.hermes import (
    HermesReferee,
)
from athena.kernel.kernel import AgentKernel
from athena.kernel.dispatch import CapabilityDispatchShim
from athena.mcp.adapter import MCPAdapter
from athena.mcp.client import MCPClient
from athena.mcp.prompts import MCPPromptProvider
from athena.mcp.resources import MCPResourceProvider
from athena.mcp.supervisor import MCPConnectionSupervisor
from athena.memory.store import MemoryStore
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.policy.credentials import SecretManager
from athena.policy.engine import PolicyEngine
from athena.scheduler.scheduler import Scheduler
from athena.skills.lifecycle import SkillLifecycle, SkillStore
from athena.kernel.continuations import ContinuationStore
from athena.state.approvals import ApprovalStore
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.executions import ExecutionStore
from athena.state.messages import MessageStore
from athena.state.mutations import MutationStore
from athena.state.runtime_sessions import RuntimeSessionStore
from athena.state.schedules import ScheduleStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.state.tool_repairs import ToolRepairStore
from athena.state.context_blocks import ContextBlockStore
from athena.state.self_host import SelfHostMissionStore
from athena.packs.store import PackStore
from athena.service.operational_matrix import build_operational_matrix
from athena.service.health import build_runtime_health
from athena.self_host.gates import SelfHostGateBundle
from athena.state.delegate_sessions import DelegateSessionStore
from athena.project.index.store import ProjectIndexStore
from athena.project.index.builder import ProjectIndexBuilder
from athena.project.index.coordinator import ProjectIndexCoordinator
from athena.state.failure_memory import FailureMemory
from athena.delegates.registry import DelegateRegistry
from athena.tasks.budgets import BudgetTracker
from athena.tasks.cancellation import CancellationManager
from athena.tasks.delegation import DelegationManager
from athena.tasks.manager import TaskManager
from athena.tasks.worker import TaskWorker
from athena.voice import VoiceManager

if TYPE_CHECKING:
    from athena.state.input_requests import InputRequestStore

from athena.protocol.events import Event
from athena.protocol.errors import ModelProviderUnconfigured, ProviderError, ServiceNotReady
from athena.protocol.policy import ApprovalScope
from athena.protocol.tasks import (
    AgentRequest,
    TaskSpec,
    TrustedTaskMetadata,
    TaskStatus,
    WorkspaceSpec,
)
from athena.protocol.task_codec import decode_criteria, encode_criteria

from athena.service.candidates import CandidateService
from athena.service.direct_execution import DirectExecutionPorts, DirectExecutionService
from athena.service.interaction import OperatorInteractionService
from athena.service.task_api import TaskAPI
from athena.service.task_intake import TaskIntake
from athena.service.operator_query import OperatorQueryService
from athena.service.pack_api import PackAPI
from athena.service.recovery import RecoveryCoordinator
from athena.service.fusion_composition import FusionComposition
from athena.service.hermes_runtime import HermesRuntime, HermesRuntimePorts
from athena.service.lifecycle import ServiceLifecycle
from athena.service.self_host import SelfHostService
from athena.service.provider_runtime import ProviderRuntime
from athena.service.provider_recovery import ProviderOutcomeRecoveryAPI
from athena.service.readiness import ServiceReadinessAPI
from athena.service.watchers import ServiceWatchAPI
from athena.service.watch_ports import WatchPorts
from athena.service.mcp_runtime import MCPRuntime
from athena.service.config import (
    AthenaConfig,
    HermesSupervisionMode,
    MCPConfig,
    ProviderConfig,
)

__all__ = ["AthenaService"]

_DEFAULT_ANSWER_SCRIPTS = (
    {"match": {"user_contains": "2+2"}, "respond": {"text": "4", "done": True}},
)

_logger = logging.getLogger("athena.service")


_RESERVED_REQUEST_METADATA = frozenset(
    {
        "self_host",
        "review_before_commit",
        "_verification_environment",
        "_athena_verification_environment",
        "verification_writable_paths",
        "_athena_verification_writable_paths",
        "_athena_self_host",
        "_athena_review_before_commit",
        "_athena_required_gates",
        "cache_namespace",
    }
)


class AthenaService(ServiceReadinessAPI, ProviderOutcomeRecoveryAPI, ServiceWatchAPI):
    """The application API — composition root above the core services."""

    def __init__(
        self,
        *,
        config: AthenaConfig | None = None,
        device_provider=None,
        hermes_referee: HermesReferee | None = None,
    ) -> None:
        ServiceReadinessAPI.__init__(self, self)
        ServiceWatchAPI.__init__(self, WatchPorts(self))
        self.config = config or AthenaConfig()
        self._background_tasks: set[asyncio.Task] = set()
        self._observation_callbacks: list = []
        # Optional OI/device adapter surface.  Reflection must receive the
        # configured provider at registration time instead of silently
        # reporting "unsupported" for a provider owned by the host.
        self._device_provider = device_provider
        self._computer_health: dict[str, Any] = {
            "state": "not_started",
            "backend": "unknown",
        }
        self._terminals: Any = None
        self._debugger: Any = None
        self._browser: Any = None
        self._browser_health: dict[str, Any] = {
            "state": "not_started",
            "configured": False,
            "active_sessions": 0,
        }
        self._optional_capability_health: dict[str, dict[str, Any]] = {}
        self._capability_profile_status: dict[str, Any] = {
            "profile": getattr(self.config, "capability_profile", None),
            "required": [],
            "resolved": [],
            "missing": [],
            "status": "not_started",
            "blocking": False,
        }
        self._provider_recovery_runtime = ProviderOutcomeRecoveryAPI.compose(self)
        self._started = False
        self._recovery_status = "not_started"
        self._recovery_summary: dict[str, int] = {}
        self._recovery_error: str | None = None
        self._provider_recovery_health: dict[str, Any] = {
            "state": "not_started",
            "unresolved_count": 0,
            "error": None,
        }
        self._startup_health: dict[str, Any] = {
            "status": "not_started",
            "checks": {},
            "blocking_failures": [],
        }
        self._hermes_runtime = HermesRuntime(
            ports=HermesRuntimePorts(self),
            referee=hermes_referee,
        )
        cfg_ws = config.workspace_root if config is not None else None
        self._default_workspace: WorkspaceSpec = WorkspaceSpec(
            id="root",
            root=cfg_ws or os.getcwd(),
        )

        # State (created eagerly; the DB is opened on start()).
        self._db: Database | None = None
        self._store_tasks: TaskStore | None = None
        self._store_events: EventStore | None = None
        self._store_messages: MessageStore | None = None
        self._store_approvals: ApprovalStore | None = None
        self._store_mutations: MutationStore | None = None
        self._store_schedules: ScheduleStore | None = None
        self._store_runtime_sessions: RuntimeSessionStore | None = None
        self._store_executions: ExecutionStore | None = None
        self._self_host_missions: SelfHostMissionStore | None = None
        self._tool_repair_store: ToolRepairStore | None = None
        self._context_block_store: ContextBlockStore | None = None
        self._pack_store: PackStore | None = None
        self._pack_manager: Any = None
        self._pack_hook_outbox: Any = None
        self._delegate_session_store: DelegateSessionStore | None = None
        self._delegate_registry = DelegateRegistry()
        self._external_delegate_manager: Any = None
        self._capability_health_store: Any = None
        self._capability_health: Any = None
        self._world_state_store: Any = None
        self._project_index_store: ProjectIndexStore | None = None
        self._project_index_builder: ProjectIndexBuilder | None = None
        self._project_index_coordinator: ProjectIndexCoordinator | None = None
        self._failure_memory: FailureMemory | None = None
        self._generated_store: GeneratedCapabilityStore | None = None
        self._research_store: Any = None
        self._world_states: dict[str, Any] = {}
        self._sessions: SessionRepository | None = None

        # Subsystem handles (assigned in start()).
        self._secrets: SecretManager | None = None
        self._execution: ExecutionManager | None = None
        self._policy: PolicyEngine | None = None
        self._registry: CapabilityRegistry | None = None
        self._fabric: CapabilityFabric | None = None
        self._dispatcher: CapabilityDispatcher | None = None
        self._reality_gate: Any = None
        self._reality_coordinator: Any = None
        self._acceptance_verifier: Any = None
        self._compiler: ContextCompiler | None = None
        self._model_registry: ProviderRegistry | None = None
        self._voice: VoiceManager | None = None
        self._kernel: AgentKernel | None = None
        self._task_manager: TaskManager | None = None
        self._worker: TaskWorker | None = None
        self._worker_task: asyncio.Task | None = None
        self._approval_recovery_tasks: set[asyncio.Task] = set()
        self._watch_poll_task: asyncio.Task | None = None
        self._shutdown_hooks: list[tuple[str, Any]] = []
        self._resource_finalizer: Any = None
        self._resource_obligation_store: Any = None
        self._pending_finalization_store: Any = None
        self._shutdown_status: dict[str, Any] = {"state": "not_started"}
        self._budgets: BudgetTracker | None = None
        self._cancellations: CancellationManager | None = None
        self._delegation: DelegationManager | None = None
        self._memory: MemoryStore | None = None
        self._skills: SkillStore | None = None
        self._skill_lifecycle: SkillLifecycle | None = None
        self._scheduler: Scheduler | None = None
        self._schedule_api: Any = None
        self._artifacts: ArtifactStore | None = None
        self._mcp: MCPAdapter | None = None
        self._workflow_store: Any = None
        self._workflow_run_store: Any = None
        self._synthesis: Any = None
        self._synthesis_event_observer: Any = None
        self._scratch = ScratchManager()
        # Set by ServiceLifecycle during startup (P1-10 extraction); declared
        # here so the facade's type surface stays complete.
        self._store_input_requests: InputRequestStore | None = None
        self._steering_store: Any = None
        self._external_effect_store: ExternalEffectStore | None = None
        self._knowledge: Any = None
        self._provider_usage_store: Any = None
        self._model_response_store: Any = None
        self._runtime_state_root: Path | None = None
        self._runtime_host_supervisor: Any = None
        self._router: ModelRouter | None = None
        # Self-host orchestration mechanism (P1-10): constructed against
        # the facade; every authority seam still resolves through self.
        self._self_host = SelfHostService(self)
        self._candidates = CandidateService(self)
        self._provider_runtime = ProviderRuntime(self)
        self._mcp_runtime = MCPRuntime(self)
        # Facades are stable mechanism objects; constructing them per API call
        # obscures ownership and needlessly recreates the same service binding.
        self._task_api = TaskAPI(self)
        self._task_intake = TaskIntake(self)
        self._direct_execution = DirectExecutionService(ports=DirectExecutionPorts(self))
        self._interaction = OperatorInteractionService(self)
        self._operator_query = OperatorQueryService(self)
        self._recovery = RecoveryCoordinator(self)
        self._pack_api = PackAPI(
            manager=lambda: self._pack_manager,
            workspace_root=lambda: self.config.workspace_root,
        )

        self._mcp_clients: list[MCPClient] = []
        # Injection seam retained for transport fixtures and host adapters;
        # MCPRuntime owns lifecycle, while the service owns the client type.
        self._mcp_client_factory = MCPClient
        self._mcp_resources: MCPResourceProvider | None = None
        self._mcp_prompts: MCPPromptProvider | None = None
        self._mcp_connection_status: dict[str, dict[str, Any]] = {}
        self._mcp_reconnect_failures: dict[str, int] = {}
        self._mcp_supervisor: MCPConnectionSupervisor | None = None

    # ------------------------------------------------------------------ #
    # Factories for tests / smoke
    # ------------------------------------------------------------------ #
    @classmethod
    def in_memory(
        cls,
        *,
        config: AthenaConfig | None = None,
        extra_scripts: list[dict] | None = None,
    ) -> "AthenaService":
        """A fully-wired, isolated service (in-memory DB, temp workspace, Fake
        model). No network, no on-disk state, no real toolchain required."""
        if config is None:
            tmp = tempfile.mkdtemp(prefix="athena-ws-")
            scripts = list(_DEFAULT_ANSWER_SCRIPTS)
            if extra_scripts:
                scripts.extend(extra_scripts)
            config = AthenaConfig(
                db_path=":memory:",
                workspace_root=tmp,
                artifact_root=os.path.join(tmp, "artifacts"),
                providers=(ProviderConfig(kind="fake", name="fake", extra={"scripts": scripts}),),
            )
        return cls(config=config)

    # ------------------------------------------------------------------ #
    # Lifecycle: start / stop
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        return await ServiceLifecycle(self).start()

    async def _start_impl(self) -> None:
        return await ServiceLifecycle(self)._start_impl()

    async def _quarantine_tasks_for_packs(
        self,
        *,
        task_store: TaskStore,
        task_manager: TaskManager,
        unavailable: set[str],
    ) -> list[str]:
        return await self._recovery.quarantine_tasks_for_packs(
            task_store=task_store, task_manager=task_manager, unavailable=unavailable
        )

    async def stop(self) -> None:
        return await ServiceLifecycle(self).stop()

    async def _mark_execution_uncertain(self, task_id: str, marker: dict[str, Any]) -> None:
        """Park a task when an executed effect outlives its finish journal."""
        tasks = self._store_tasks
        if tasks is None:
            raise RuntimeError("task store unavailable while recording execution uncertainty")
        await tasks.record_recovery_marker(task_id, marker)
        row = await tasks.get(task_id)
        current = TaskStatus(row["status"]) if row else None
        if (
            current
            in {
                TaskStatus.CREATED,
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.INTERRUPTED,
            }
            and self._task_manager is not None
        ):
            await self._task_manager.transition(
                task_id,
                TaskStatus.RECOVERY_REQUIRED,
                reason="execution effect occurred but finish journal persistence failed",
            )

    def startup_health(self) -> dict[str, Any]:
        """Return startup checks for readiness and operator diagnostics."""
        checks = {
            str(name): dict(value)
            for name, value in (self._startup_health.get("checks") or {}).items()
            if isinstance(value, dict)
        }
        return {
            "status": self._startup_health.get("status", "not_started"),
            "checks": checks,
            "blocking_failures": list(self._startup_health.get("blocking_failures") or ()),
        }

    def runtime_health(self) -> dict[str, Any]:
        """Return live subsystem health for reflection and operator APIs."""
        return build_runtime_health(self)

    def operational_matrix(self) -> dict[str, Any]:
        """Return the concrete backend/runtime readiness matrix for operators."""
        return build_operational_matrix(self)

    async def retry_resource_cleanup(self, task_id: str) -> dict[str, Any]:
        """Run the operator-visible retry path for durable resource obligations."""
        finalizer = self._resource_finalizer
        manager = self._task_manager
        if finalizer is None or manager is None:
            raise ServiceNotReady("resource finalization is not initialized")
        task = await manager.get(str(task_id))
        pending_store = self._pending_finalization_store
        pending = await pending_store.get(str(task_id)) if pending_store is not None else None
        result = pending.result if pending is not None else await manager.get_result(str(task_id))
        if result is None:
            from athena.protocol.tasks import TaskResult

            result = TaskResult(
                task_id=str(task_id),
                status=TaskStatus.RECOVERY_REQUIRED,
                summary="resource cleanup recovery",
            )
        await finalizer.retry(task, result)
        health = finalizer.health()
        task_unresolved = finalizer.unresolved_for_task(str(task_id))
        if pending is not None and not task_unresolved and not health.get("durability_error"):
            recovered = await manager.commit_pending_finalization(str(task_id))
            if recovered is not None:
                health = {
                    **health,
                    "pending_finalization": {
                        "status": "committed",
                        "result_status": recovered.status.value,
                    },
                }
        check = self._startup_health.get("checks", {}).get("resource_obligations")
        if isinstance(check, dict):
            check.update(
                {
                    "status": "ok" if health["unresolved_count"] == 0 else "degraded",
                    "blocking": health["unresolved_count"] > 0,
                    "unresolved_count": health["unresolved_count"],
                }
            )
        return health

    def _live_capability_profile_status(
        self, mcp: Mapping[str, Mapping[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Refresh cheap live profile state without performing async probes."""
        configured = tuple(self.config.effective_required_capabilities)
        if not configured:
            status: dict[str, Any] = {
                "profile": self.config.capability_profile,
                "required": [],
                "resolved": [],
                "missing": [],
                "status": "ok",
                "blocking": False,
            }
            self._capability_profile_status = status
            return dict(status)

        mcp_status = dict(mcp or {})
        previous_missing = {
            str(item.get("id")): dict(item)
            for item in self._capability_profile_status.get("missing", ())
            if isinstance(item, Mapping)
        }
        resolved: list[str] = []
        missing: list[dict[str, Any]] = []
        aliases = {
            "terminal": "terminal_session",
            "external_delegate": "external_delegate",
            "external-delegate": "external_delegate",
        }
        from athena.protocol.capabilities import Availability

        for configured_id in configured:
            requirement = str(configured_id).strip()
            kind, separator, identifier = requirement.partition(":")
            kind = kind.casefold() if separator else "capability"
            identifier = identifier.strip() if separator else requirement
            reason: str | None = None
            if kind == "mcp":
                state = mcp_status.get(identifier)
                if state is None:
                    reason = "MCP server is not configured"
                elif state.get("state") != "connected":
                    reason = str(state.get("last_error") or state.get("state") or "not connected")
            elif kind == "capability":
                if self._registry is None:
                    reason = "capability registry is not initialized"
                else:
                    try:
                        descriptor = self._registry.resolve(
                            aliases.get(identifier.casefold(), identifier)
                        )
                        if descriptor.availability is not Availability.AVAILABLE:
                            reason = f"capability is {descriptor.availability.value}"
                    except Exception:
                        reason = "capability is not registered"
            elif kind == "delegate":
                try:
                    preflight = self._delegate_registry.preflight(identifier)
                    if not preflight.get("available"):
                        reason = str(preflight.get("reason") or "delegate is unavailable")
                except KeyError:
                    reason = "delegate is not configured"
            elif kind in {"skill", "pack"}:
                # Skill/pack checks require an async store read; preserve their
                # last verified state here and refresh it at admission.
                if requirement in previous_missing:
                    reason = str(previous_missing[requirement].get("reason") or "not ready")
                elif self._capability_profile_status.get("status") != "ok":
                    reason = "capability profile has not been verified"
            else:
                reason = f"unknown capability requirement kind: {kind}"
            if reason is None:
                resolved.append(requirement)
            else:
                missing.append({"id": requirement, "reason": reason})
        status = {
            "profile": self.config.capability_profile,
            "required": list(configured),
            "resolved": resolved,
            "missing": missing,
            "status": "ok" if not missing else "failed",
            "blocking": True,
        }
        self._capability_profile_status = status
        return dict(status)

    async def _validate_required_capabilities(self) -> dict[str, Any]:
        """Resolve the configured deployment capability contract.

        Required capability ids are host configuration, never model input. The
        contract accepts native capability ids plus explicit ``mcp:``,
        ``skill:``, ``pack:``, and ``delegate:`` references. A missing or
        unhealthy required surface fails startup before workers can claim work.
        """
        configured = tuple(self.config.effective_required_capabilities)
        status: dict[str, Any] = {
            "profile": self.config.capability_profile,
            "required": list(configured),
            "resolved": [],
            "missing": [],
            "blocking": bool(configured),
        }
        if not configured:
            status["status"] = "ok"
            self._capability_profile_status = dict(status)
            return status

        registry = self._registry
        aliases = {
            "terminal": "terminal_session",
            "external_delegate": "external_delegate",
            "external-delegate": "external_delegate",
            "external delegates": "external_delegate",
        }
        from athena.protocol.capabilities import Availability

        for requested in configured:
            requirement = str(requested).strip()
            kind, separator, identifier = requirement.partition(":")
            kind = kind.casefold() if separator else "capability"
            identifier = identifier.strip() if separator else requirement
            reason: str | None = None

            if kind == "mcp":
                mcp = self.mcp_status().get(identifier)
                if mcp is None:
                    reason = "MCP server is not configured"
                elif mcp.get("state") != "connected":
                    reason = str(mcp.get("last_error") or mcp.get("state") or "not connected")
            elif kind == "skill":
                lifecycle = self._skill_lifecycle
                skill = await lifecycle.get(identifier) if lifecycle is not None else None
                if skill is None:
                    reason = "skill is not installed"
                elif not bool(getattr(skill, "enabled", False)):
                    reason = "skill is disabled"
            elif kind == "pack":
                manager = self._pack_manager
                if manager is None:
                    reason = "pack manager is not initialized"
                else:
                    try:
                        detail = await manager.inspect_installed(identifier)
                    except KeyError:
                        detail = None
                    if detail is None:
                        reason = "pack is not installed"
                    elif not bool(detail.get("enabled")):
                        reason = "pack is disabled"
                    elif (detail.get("health_detail") or {}).get("status") != "healthy":
                        health = detail.get("health_detail") or {}
                        reason = str(health.get("reason") or health.get("status"))
            elif kind == "delegate":
                try:
                    preflight = self._delegate_registry.preflight(identifier)
                    if not preflight.get("available"):
                        reason = str(preflight.get("reason") or "delegate is unavailable")
                except KeyError:
                    reason = "delegate is not configured"
            elif kind == "capability":
                resolved_name = aliases.get(identifier.casefold(), identifier)
                if registry is None:
                    reason = "capability registry is not initialized"
                else:
                    try:
                        descriptor = registry.resolve(resolved_name)
                    except Exception:
                        reason = "capability is not registered"
                    else:
                        if descriptor.availability is not Availability.AVAILABLE:
                            reason = f"capability is {descriptor.availability.value}"
            else:
                reason = f"unknown capability requirement kind: {kind}"

            if reason is None:
                status["resolved"].append(requirement)
            else:
                status["missing"].append({"id": requirement, "reason": reason})

        status["status"] = "ok" if not status["missing"] else "failed"
        self._capability_profile_status = dict(status)
        return status

    def mcp_status(self) -> dict[str, dict[str, Any]]:
        return self._mcp_runtime.status()

    def mcp_resources(self) -> list[dict[str, Any]]:
        return self._mcp_runtime.resources()

    async def read_mcp_resource(self, uri: str, *, connection_id: str | None = None) -> list[Any]:
        return await self._mcp_runtime.read_resource(uri, connection_id=connection_id)

    async def mcp_prompts(self) -> list[dict[str, Any]]:
        return await self._mcp_runtime.prompts()

    async def render_mcp_prompt(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
        *,
        connection_id: str | None = None,
    ) -> list[Any]:
        return await self._mcp_runtime.render_prompt(name, arguments, connection_id=connection_id)

    @property
    def _hermes_referee(self) -> HermesReferee | None:
        """Compatibility projection for the Hermes runtime owner."""
        return self._hermes_runtime.referee

    @_hermes_referee.setter
    def _hermes_referee(self, value: HermesReferee | None) -> None:
        self._hermes_runtime.referee = value

    @property
    def _hermes_adapter(self) -> Any:
        """Compatibility projection for the Hermes runtime adapter."""
        return self._hermes_runtime.adapter

    @_hermes_adapter.setter
    def _hermes_adapter(self, value: Any) -> None:
        self._hermes_runtime.adapter = value

    @property
    def _hermes_referee_owned(self) -> bool:
        """Compatibility projection for owned-referee lifecycle state."""
        return self._hermes_runtime.referee_owned

    @_hermes_referee_owned.setter
    def _hermes_referee_owned(self, value: bool) -> None:
        self._hermes_runtime.referee_owned = value

    @property
    def _hermes_status_error(self) -> str | None:
        """Compatibility projection for Hermes safety error evidence."""
        return self._hermes_runtime.status_error

    @_hermes_status_error.setter
    def _hermes_status_error(self, value: str | None) -> None:
        self._hermes_runtime.status_error = value

    @property
    def _hermes_supervision_mode(self) -> HermesSupervisionMode:
        """Return the explicit operator policy for self-host supervision."""
        return self._hermes_runtime.supervision_mode

    @property
    def _hermes_supervision_active(self) -> bool:
        """Whether Hermes may contribute evidence at self-host checkpoints."""
        return self._hermes_runtime.supervision_active

    async def hermes_referee_status(self) -> dict[str, Any]:
        """Return operator-safe Hermes configuration and connectivity status."""
        return await self._hermes_runtime.status()

    async def _recover_approved_continuations(
        self,
        *,
        continuations: ContinuationStore,
        task_store: TaskStore,
        task_manager: TaskManager,
        kernel: AgentKernel,
    ) -> None:
        return await self._recovery.recover_approved_continuations(
            continuations=continuations,
            task_store=task_store,
            task_manager=task_manager,
            kernel=kernel,
        )

    def _track_approval_recovery(self, task_id: str, recovery: asyncio.Task):
        return self._recovery.track_approval_recovery(task_id, recovery)

    async def _recover_answered_input_requests(
        self,
        *,
        input_requests: InputRequestStore,
        task_store: TaskStore,
        task_manager: TaskManager,
        kernel: AgentKernel,
    ) -> None:
        return await self._recovery.recover_answered_input_requests(
            input_requests=input_requests,
            task_store=task_store,
            task_manager=task_manager,
            kernel=kernel,
        )

    # ------------------------------------------------------------------ #
    # Application API
    # ------------------------------------------------------------------ #
    async def submit(self, request: AgentRequest, *, wait: bool = True) -> TaskSpec:
        return await self._task_api.submit(request, wait=wait)

    def voice_health(self) -> dict[str, Any]:
        """Return bounded voice readiness without exposing credentials."""
        if self._voice is None:
            return {"enabled": False, "state": "not_started"}
        return self._voice.health()

    async def transcribe_voice(
        self,
        data: bytes,
        *,
        mime_type: str,
        filename: str = "voice-input",
        language: str | None = None,
        task_id: str | None = None,
    ):
        """Transcribe audio through the configured voice route."""
        if self._voice is None:
            from athena.protocol.errors import VoiceUnavailable

            raise VoiceUnavailable("voice subsystem is not started")
        return await self._voice.transcribe(
            data,
            mime_type=mime_type,
            filename=filename,
            language=language,
            task_id=task_id,
        )

    async def synthesize_voice(
        self,
        text: str,
        *,
        task_id: str | None = None,
        voice: str | None = None,
        response_format: str | None = None,
    ):
        """Synthesize bounded spoken output into a task-owned artifact."""
        if self._voice is None:
            from athena.protocol.errors import VoiceUnavailable

            raise VoiceUnavailable("voice subsystem is not started")
        return await self._voice.synthesize(
            text,
            task_id=task_id,
            voice=voice,
            response_format=response_format,
        )

    async def synthesize_task_result(
        self,
        task_id: str,
        *,
        voice: str | None = None,
        response_format: str | None = None,
    ):
        """Speak the durable task summary once the task has finalized."""
        from athena.protocol.errors import VoiceResultNotReady

        result = await self.get_result(task_id)
        if result is None:
            raise VoiceResultNotReady(f"result not ready for task {task_id!r}")
        summary = str(getattr(result, "summary", "") or "").strip()
        if not summary:
            raise VoiceResultNotReady(f"task {task_id!r} has no speakable summary")
        return await self.synthesize_voice(
            summary,
            task_id=task_id,
            voice=voice,
            response_format=response_format,
        )

    async def submit_spec(
        self,
        spec: TaskSpec,
        *,
        wait: bool = False,
        user_request: Any | None = None,
        trusted: bool = False,
        enqueue: bool = True,
    ) -> TaskSpec:
        return await self._task_api.submit_spec(
            spec,
            wait=wait,
            user_request=user_request,
            trusted=trusted,
            enqueue=enqueue,
        )

    async def submit_self_host(
        self,
        objective: str,
        *,
        workspace_root: str | None = None,
        additional_criteria: tuple[str, ...] = (),
        task_id: str | None = None,
        wait: bool = True,
        mission_id: str | None = None,
        plan: Mapping[str, Any] | None = None,
        _allow_known_dirty: bool = False,
    ) -> TaskSpec:
        return await self._self_host.submit_self_host(
            objective,
            workspace_root=workspace_root,
            additional_criteria=additional_criteria,
            task_id=task_id,
            wait=wait,
            mission_id=mission_id,
            plan=plan,
            _allow_known_dirty=_allow_known_dirty,
        )

    def self_host_preflight(self, *, workspace_root: str | None = None) -> dict[str, Any]:
        return self._self_host.preflight(workspace_root=workspace_root)

    async def _plan_next_self_host_item(
        self,
        mission: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        current_index: Any,
        bundle: SelfHostGateBundle,
        task_id: str | None,
        initial: bool = False,
        current_release_evidence: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        return await self._self_host.plan_next_self_host_item(
            mission,
            plan=plan,
            current_index=current_index,
            bundle=bundle,
            task_id=task_id,
            initial=initial,
            current_release_evidence=current_release_evidence,
        )

    async def _verify_self_host_performance(
        self,
        *,
        bundle: SelfHostGateBundle,
        task_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        return await self._self_host.verify_self_host_performance(
            bundle=bundle,
            task_id=task_id,
        )

    async def _verify_self_host_completion(
        self,
        mission: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        current_index: Any,
        bundle: SelfHostGateBundle,
        current_fingerprint: str,
        release_evidence: Mapping[str, Any],
        task_id: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return await self._self_host.verify_self_host_completion(
            mission,
            plan=plan,
            current_index=current_index,
            bundle=bundle,
            current_fingerprint=current_fingerprint,
            release_evidence=release_evidence,
            task_id=task_id,
        )

    async def _run_hermes_mission_referee(
        self,
        mission: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        current_index: Any,
        bundle: SelfHostGateBundle,
        current_fingerprint: str,
        release_evidence: Mapping[str, Any],
        task_id: str | None,
    ) -> dict[str, Any]:
        return await self._self_host.run_hermes_mission_referee(
            mission,
            plan=plan,
            current_index=current_index,
            bundle=bundle,
            current_fingerprint=current_fingerprint,
            release_evidence=release_evidence,
            task_id=task_id,
        )

    async def self_host_status(self, *, workspace_root: str | None = None) -> list[dict[str, Any]]:
        return await self._self_host.self_host_status(workspace_root=workspace_root)

    async def continue_self_host(
        self,
        *,
        workspace_root: str | None = None,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._self_host.continue_self_host(
            workspace_root=workspace_root,
            mission_id=mission_id,
        )

    async def _enqueue_spec(
        self,
        task_manager: TaskManager,
        spec: TaskSpec,
        *,
        wait: bool,
        user_request: AgentRequest | None = None,
        enqueue: bool = True,
    ):
        return await self._task_api.enqueue_spec(
            task_manager, spec, wait=wait, user_request=user_request, enqueue=enqueue
        )

    async def _reconcile_created_intake(self) -> dict[str, int]:
        return await self._task_api.reconcile_created_intake()

    def _spawn_static_prefetch(self, task: TaskSpec) -> None:
        compiler = self._compiler
        if compiler is None or not hasattr(compiler, "precompute_static"):
            return

        async def _prefetch() -> None:
            try:
                await compiler.precompute_static(task)
            except Exception:  # pragma: no cover - defensive
                pass

        try:
            worker = asyncio.create_task(_prefetch())
        except Exception:  # pragma: no cover - no running loop
            return
        # Keep a strong ref until the task finishes (asyncio would otherwise
        # GC it mid-await), then drop it so completed workers never
        # accumulate.
        self._background_tasks.add(worker)
        worker.add_done_callback(self._background_tasks.discard)

    async def _record_canonical_user_turn(self, request: Any, task: TaskSpec) -> None:
        """Append the service-owned user turn exactly once before enqueueing."""
        if self._store_messages is None or not task.session_id:
            raise RuntimeError(
                f"task {task.id!r} cannot enter the queue without a durable session/message store"
            )
        from athena.protocol.messages import (
            ArtifactRefBlock,
            FileRefBlock,
            Message,
            Provenance,
            Role,
            SourceType,
            TextBlock,
            TrustClass,
            utcnow,
        )

        blocks: list[Any] = [
            TextBlock(
                text=str(getattr(request, "prompt", None) or getattr(request, "objective", "")),
                provenance=Provenance(
                    source_type=SourceType.USER,
                    source_id=task.id,
                    trust=TrustClass.USER_CONTENT,
                    scope="session",
                ),
            )
        ]
        for attachment in getattr(request, "attachments", ()) or ():
            if hasattr(attachment, "uri"):
                blocks.append(
                    ArtifactRefBlock(
                        uri=str(attachment.uri),
                        ref=attachment,
                    )
                )
            elif isinstance(attachment, Mapping):
                uri = str(attachment.get("uri") or attachment.get("ref") or "")
                if uri:
                    blocks.append(
                        FileRefBlock(
                            uri=uri,
                            mime_type=attachment.get("mime_type"),
                        )
                    )
        message = Message(
            # Stable association makes retries idempotent without making the
            # task/message identity part of normal opaque ID generation.
            id=f"msg_user_{task.id}",
            role=Role.USER,
            blocks=tuple(blocks),
            created_at=utcnow(),
            provenance=Provenance(
                source_type=SourceType.USER,
                source_id=task.id,
                trust=TrustClass.USER_CONTENT,
                scope="session",
            ),
            metadata={
                "session_id": task.session_id,
                "task_id": task.id,
                "message_kind": "user_turn",
                "canonical_user_turn": True,
            },
        )
        append_user_turn = getattr(self._store_messages, "append_user_turn", None)
        if append_user_turn is not None:
            await append_user_turn(task.session_id, message)
        else:
            await self._store_messages.append_to_session(task.session_id, message)

    def register_external_delegate(self, spec, *, connector=None) -> None:
        """Register a host-configured ACP/A2A/OpenAI delegate.

        Registration is host-side configuration; model input can select only
        specialists already present in this registry.
        """
        self._delegate_registry.register(spec, connector=connector)

    async def execute_direct(
        self,
        source: str,
        *,
        language: str = "shell",
        cwd: str | None = None,
        session_id: str | None = None,
        inject_into_context: bool = True,
        on_approval=None,
    ) -> dict:
        """Execute code directly WITHOUT routing through the model loop.

        Used by the CLI ``!``/``!!`` shell escapes. The execution still flows
        through the canonical registry -> policy -> capability path and can
        request approval, but bypasses AgentKernel inference. ``on_approval``
        is an optional async callback receiving ``(approval_id, scopes)``;
        interfaces use it to collect the human decision without making the
        service own presentation concerns.

        Args:
            source: The code/shell command to execute.
            language: Runtime language (``shell``, ``python``, ``node``, ...).
            cwd: Working directory (must be within workspace root).
            session_id: Optional session to record the execution against.
            inject_into_context: If True, the result is recorded as a capability
                result that future model turns may use (the ``!`` form). If
                False, the result is recorded for audit but excluded from future
                model context (the ``!!`` form).
            on_approval: Optional async ``(approval_id, scopes)`` decision hook.

        Returns:
            A result dict with ``exit_code``, ``stdout``, ``stderr``, ``status``.
        """
        return await self._direct_execution.execute_direct(
            source,
            language=language,
            cwd=cwd,
            session_id=session_id,
            inject_into_context=inject_into_context,
            on_approval=on_approval,
        )

    async def run_task(self, task_id: str) -> TaskSpec:
        return await self._task_api.run_task(task_id)

    async def wait_for(self, task_id: str, *, timeout: float | None = None) -> TaskSpec:
        return await self._task_api.wait_for(task_id, timeout=timeout)

    async def get_task(self, task_id: str) -> TaskSpec:
        return await self._task_api.get_task(task_id)

    async def list_tasks(self, status: TaskStatus | None = None) -> list[dict]:
        """Return durable tasks for operator/CLI inspection."""
        if self._store_tasks is None:
            return []
        if status is not None:
            return await self._store_tasks.list_by_status(status)
        rows: list[dict] = []
        for task_status in TaskStatus:
            rows.extend(await self._store_tasks.list_by_status(task_status))
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""))

    async def list_jobs(self, *, enabled_only: bool = False) -> list[dict]:
        """Return scheduled jobs with their latest durable run receipt."""
        if self._store_schedules is None:
            return []
        jobs = await self._store_schedules.list_jobs(enabled_only=enabled_only)
        for job in jobs:
            job["last_run_receipt"] = await self._store_schedules.last_run(job["id"])
        return jobs

    async def list_workflows(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return workflow definitions visible to the operator.

        The workflow store remains the authority for scope filtering.  Task
        candidates are included only when their owning task id is supplied;
        project and user workflows remain visible without one.
        """
        store = self._workflow_store
        if store is None:
            return []
        workflows = await store.list(
            task_id=task_id,
            project_id=getattr(self._default_workspace, "id", None),
            user_id=self.config.cache_namespace,
        )
        return [workflow.to_record() for workflow in workflows]

    async def inspect_workflow(
        self,
        workflow_id: str,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Inspect one visible workflow definition through the durable store."""
        store = self._workflow_store
        if store is None:
            return None
        workflow = await store.get(
            workflow_id,
            task_id=task_id,
            project_id=getattr(self._default_workspace, "id", None),
            user_id=self.config.cache_namespace,
        )
        return workflow.to_record() if workflow is not None else None

    async def job_set_enabled(self, job_id: str, enabled: bool) -> bool:
        if self._store_schedules is None:
            return False
        return await self._store_schedules.set_enabled(job_id, enabled)

    async def job_run_now(self, job_id: str) -> str | None:
        if self._scheduler is None:
            return None
        return await self._scheduler.run_now(job_id)

    async def grant_job_control(
        self,
        job_id: str,
        task_id: str,
        *,
        principal_id: str | None = None,
        project_id: str | None = None,
        operations: tuple[str, ...] = ("inspect", "update", "enable", "disable", "run"),
        expires_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Grant a schedule lease to a task through the operator API.

        The lease is bound to the task identity; no bearer token is returned
        to model-visible context.
        """
        if self._schedule_api is None:
            return None
        from athena.capabilities.schedule import ScheduleControl

        return await self._schedule_api.grant_control(
            job_id,
            control=ScheduleControl(origin="user_direct"),
            principal_id=principal_id,
            project_id=project_id,
            task_id=task_id,
            operations=operations,
            expires_at=expires_at,
        )

    async def revoke_job_control(self, job_id: str) -> bool:
        """Revoke a task-bound schedule lease through the operator API."""
        if self._schedule_api is None:
            return False
        from athena.capabilities.schedule import ScheduleControl

        return await self._schedule_api.revoke_control(
            job_id,
            control=ScheduleControl(origin="user_direct"),
        )

    async def list_packs(self, query: str | None = None) -> list[dict[str, Any]]:
        return await self._pack_api.list(query)

    async def inspect_pack(self, pack_id: str) -> dict[str, Any] | None:
        return await self._pack_api.inspect(pack_id)

    async def install_pack(self, source_path: str, *, enable: bool = True) -> dict[str, Any]:
        return await self._pack_api.install(source_path, enable=enable)

    async def enable_pack(self, pack_id: str) -> dict[str, Any]:
        return await self._pack_api.enable(pack_id)

    async def disable_pack(self, pack_id: str) -> dict[str, Any]:
        return await self._pack_api.disable(pack_id)

    async def remove_pack(self, pack_id: str) -> bool:
        return await self._pack_api.remove(pack_id)

    async def get_result(self, task_id: str):
        return await self._task_api.get_result(task_id)

    async def stream_events(self, task_id: str, after_sequence: int = 0):
        async for ev in self._task_api.stream_events(task_id, after_sequence=after_sequence):
            yield ev

    async def stream_all(self, after_rowid: int = 0, limit: int = 200):
        async for ev in self._task_api.stream_all(after_rowid=after_rowid, limit=limit):
            yield ev

    async def get_task_status(self, task_id: str) -> str | None:
        return await self._task_api.get_task_status(task_id)

    async def cancel(self, task_id: str, reason: str = "cancelled by user") -> TaskStatus:
        return await self._interaction.cancel(task_id, reason)

    async def interrupt(self, task_id: str, reason: str = "externally interrupted") -> TaskStatus:
        return await self._interaction.interrupt(task_id, reason)

    async def steer_task(
        self,
        task_id: str,
        text: str,
        *,
        principal_id: str | None = None,
        source_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue operator/parent steering for the next safe model boundary."""
        if self._steering_store is None or self._task_manager is None:
            raise ServiceNotReady("task steering is not initialized")
        task = await self._task_manager.get(str(task_id))
        if source_task_id is not None:
            source_id = str(source_task_id)
            try:
                source = await self._task_manager.get(source_id)
            except KeyError as exc:
                raise ValueError(f"source task {source_id!r} does not exist") from exc
            # A parent may steer any descendant, but unrelated tasks must not
            # be able to inject authority-bearing content into one another.
            cursor = task
            related = False
            visited: set[str] = set()
            while cursor.parent_task_id and cursor.parent_task_id not in visited:
                visited.add(cursor.id)
                if cursor.parent_task_id == source.id:
                    related = True
                    break
                try:
                    cursor = await self._task_manager.get(cursor.parent_task_id)
                except KeyError:
                    break
            if not related:
                raise ValueError(
                    f"source task {source_id!r} is not an ancestor of task {task_id!r}"
                )
        status = await self.get_task_status(str(task_id))
        if status not in {
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
            TaskStatus.INTERRUPTED.value,
        }:
            raise ValueError(f"task {task_id!r} is not steerable in status {status}")
        record = await self._steering_store.enqueue(
            str(task_id),
            text,
            principal_id=str(principal_id or self.config.cache_namespace),
            source_task_id=source_task_id,
            source="operator",
        )
        worker = self._worker
        if worker is not None:
            worker.notify()
        return record

    async def pending_input(self, task_id: str) -> dict | None:
        return await self._interaction.pending_input(task_id)

    async def provide_input(self, task_id: str, answer: str) -> None:
        return await self._interaction.provide_input(task_id, answer)

    async def approve(self, approval_id: str, *, granted: bool, scope: str | None = None) -> None:
        return await self._interaction.approve(approval_id, granted=granted, scope=scope)

    async def _mark_approval_recovery(
        self, task_id: str | None, approval_id: str, error: BaseException
    ) -> None:
        return await self._interaction.mark_approval_recovery(task_id, approval_id, error)

    def _install_grant(
        self,
        approval_id: str,
        task_id: str | None,
        metadata: dict,
        scope: str | None = None,
        first: ApprovalScope | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        return self._interaction.install_grant(
            approval_id,
            task_id,
            metadata,
            scope=scope,
            first=first,
            expires_at=expires_at,
        )

    async def _rehydrate_approval_grants(
        self, approvals: ApprovalStore, continuations: ContinuationStore
    ) -> None:
        return await self._interaction.rehydrate_approval_grants(approvals, continuations)

    def _clamp_approval_scope(self, choice: str | None, metadata: dict) -> ApprovalScope | None:
        return self._interaction.clamp_approval_scope(choice, metadata)

    async def pending_approval_id(self, task_id: str) -> str | None:
        return await self._interaction.pending_approval_id(task_id)

    async def list_sessions(self) -> list[dict]:
        return await self._interaction.list_sessions()

    async def close_session(self, session_id: str) -> bool:
        """Close one durable conversation and its session-scoped resources."""
        sessions = self._sessions
        if sessions is None:
            raise RuntimeError("AthenaService not started")
        if await sessions.get(session_id) is None:
            return False
        browser = self._browser
        if browser is not None:
            close_session = getattr(browser, "close_session", None)
            if callable(close_session):
                await close_session(session_id)
        closed = await sessions.close(session_id)
        if closed and self._store_events is not None:
            await self._store_events.append_event(
                "SessionClosed",
                {"session_id": session_id},
                session_id=session_id,
            )
        return closed

    async def resume(self, session_id: str, *, prompt: str = "") -> TaskSpec:
        return await self._interaction.resume(session_id, prompt=prompt)

    async def list_interrupted(self) -> list[dict]:
        return await self._interaction.list_interrupted()

    async def resume_task(self, task_id: str) -> TaskSpec:
        return await self._interaction.resume_task(task_id)

    async def inspect(self, task_id: str) -> dict:
        """Return the structured forensic view of one task's lifecycle.

        The event payloads remain authoritative; the projection groups them
        into the evidence categories used by the CLI/API so each interface
        does not have to reverse-engineer the audit trail independently.
        """
        task = await self.get_task(task_id)
        result = await self.get_result(task_id)
        gathered = [ev async for ev in self.stream_events(task_id, after_sequence=0)]
        details = [
            {
                "sequence": getattr(event, "sequence", None),
                "type": event.type,
                "timestamp": getattr(event, "timestamp", None),
                "payload": dict(event.payload or {}),
                "causal_id": getattr(event, "causal_id", None),
            }
            for event in gathered
        ]
        categories = {
            "models": [
                e
                for e in details
                if str(e["type"]).startswith("Model") or str(e["type"]).startswith("Inference")
            ],
            "capabilities": [
                e
                for e in details
                if "Capability" in str(e["type"]) or str(e["type"]).startswith("Tool")
            ],
            "policy": [
                e for e in details if "Policy" in str(e["type"]) or "Approval" in str(e["type"])
            ],
            "execution": [
                e
                for e in details
                if "Execution" in str(e["type"]) or str(e["type"]).startswith("Std")
            ],
            "mutations": [e for e in details if "Mutation" in str(e["type"])],
            "artifacts": [e for e in details if "Artifact" in str(e["type"])],
            "children": [
                e for e in details if "Child" in str(e["type"]) or "Delegat" in str(e["type"])
            ],
        }
        return {
            "task_id": task_id,
            "status": (task.metadata or {}).get("status"),
            "objective": task.objective,
            "result": result,
            "events": [e.type for e in gathered],
            "event_details": details,
            "forensics": categories,
        }

    # ------------------------------------------------------------------ #
    # Candidate lifecycle mechanism; the facade keeps compatibility entrypoints.
    # ------------------------------------------------------------------ #

    def _candidate_branch(self, task_id: str):
        return self._candidates.candidate_branch(task_id)

    async def operator_candidate(self, task_id: str) -> dict | None:
        return await self._candidates.operator_candidate(task_id)

    async def review_candidate(self, task_id: str) -> dict[str, Any] | None:
        return await self._candidates.review_candidate(task_id)

    async def _run_hermes_candidate_referee(
        self,
        mission: Mapping[str, Any],
        task_row: Mapping[str, Any],
        candidate: Mapping[str, Any],
        review: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._candidates.run_hermes_candidate_referee(
            mission, task_row, candidate, review
        )

    async def _run_self_host_reviewer(
        self,
        task_row: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._candidates.run_self_host_reviewer(task_row, candidate)

    async def _candidate_diff_text(self, task_id: str) -> str:
        return await self._candidates.candidate_diff_text(task_id)

    @staticmethod
    def _self_host_risk(changed_resources: list[Any]) -> dict[str, Any]:
        return CandidateService._self_host_risk(changed_resources)

    async def request_candidate_apply_approval(
        self,
        branch,
        *,
        plan_digest: str,
    ) -> str | None:
        return await self._candidates.request_candidate_apply_approval(
            branch, plan_digest=plan_digest
        )

    async def _candidate_apply_approval_matches(self, approval_id: str, branch) -> bool:
        return await self._candidates.candidate_apply_approval_matches(approval_id, branch)

    async def apply_candidate(self, task_id: str, approval_id: str | None = None) -> dict:
        return await self._candidates.apply_candidate(task_id, approval_id)

    async def discard_candidate(self, task_id: str) -> dict:
        return await self._candidates.discard_candidate(task_id)

    async def _candidate_review_event(
        self, event_key: str, task_id: str, review: Mapping[str, Any], outcome: Mapping[str, Any]
    ) -> None:
        return await self._candidates.persist_candidate_review_event(
            event_key, task_id, review, outcome
        )

    # ------------------------------------------------------------------ #
    # Operator projections (stable views over canonical durable state)
    # ------------------------------------------------------------------ #
    # Each method projects ONE slice of the same canonical stores the kernel
    # reads.  They never mutate state and never become a second execution
    # path; the CLI renders them verbatim.

    async def operator_permissions(self) -> dict:
        return await self._operator_query.operator_permissions()

    async def operator_diff(self, *, limit: int = 25) -> list[dict]:
        return await self._operator_query.operator_diff(limit=limit)

    async def undo_mutation(self, mutation_id: str) -> dict:
        return await self._operator_query.undo_mutation(mutation_id)

    async def operator_context_summary(self, session_id: str | None = None) -> dict:
        return await self._operator_query.operator_context_summary(session_id)

    async def operator_artifacts(self, *, limit: int = 50) -> list[dict]:
        return await self._operator_query.operator_artifacts(limit=limit)

    async def operator_generated_capabilities(self, task_id: str | None = None) -> list[dict]:
        return await self._operator_query.operator_generated_capabilities(task_id)

    async def operator_memory_candidates(self, *, limit: int = 100) -> list[dict]:
        return await self._operator_query.operator_memory_candidates(limit=limit)

    async def operator_candidates(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return the shared operator review queue for learned candidates."""
        return await self._operator_query.operator_candidates(task_id)

    async def operator_candidate_item(
        self, candidate_id: str, task_id: str | None = None
    ) -> dict[str, Any] | None:
        return await self._operator_query.operator_candidate_item(candidate_id, task_id)

    async def operator_promote_candidate(
        self,
        candidate_id: str,
        *,
        target_scope: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._operator_query.operator_promote_candidate(
            candidate_id,
            target_scope=target_scope,
            task_id=task_id,
        )

    async def operator_deprecate_candidate(
        self, candidate_id: str, *, task_id: str | None = None
    ) -> dict[str, Any]:
        return await self._operator_query.operator_deprecate_candidate(
            candidate_id, task_id=task_id
        )

    async def operator_memory_candidate(self, memory_id: str) -> dict | None:
        return await self._operator_query.operator_memory_candidate(memory_id)

    async def operator_promote_memory_candidate(
        self, memory_id: str, scope: str, scope_id: str | None = None
    ) -> dict:
        return await self._operator_query.operator_promote_memory_candidate(
            memory_id, scope, scope_id
        )

    async def operator_discard_memory_candidate(self, memory_id: str) -> dict:
        return await self._operator_query.operator_discard_memory_candidate(memory_id)

    async def operator_generated_capability(
        self, capability_id: str, task_id: str | None = None
    ) -> dict:
        return await self._operator_query.operator_generated_capability(capability_id, task_id)

    async def operator_promote_generated_capability(
        self, capability_id: str, scope: str, task_id: str | None = None
    ) -> dict:
        return await self._operator_query.operator_promote_generated_capability(
            capability_id, scope, task_id
        )

    async def operator_deprecate_generated_capability(
        self, capability_id: str, task_id: str | None = None
    ) -> dict:
        return await self._operator_query.operator_deprecate_generated_capability(
            capability_id, task_id
        )

    async def _invoke_synthesis(self, arguments: dict, *, task_id: str | None) -> dict:
        return await self._operator_query.invoke_synthesis(arguments, task_id=task_id)

    # ------------------------------------------------------------------ #
    def normalize_spec(self, spec: TaskSpec, *, trusted: bool = False) -> TaskSpec:
        """Canonical admission normalization for pre-built protocol tasks."""
        normalized = self._task_intake.normalize_spec(spec, trusted=trusted)
        if trusted:
            return normalized
        # The classifier just wrote this service-owned authority field. Mark
        # the derived record as intake-owned so subsequent metadata validation
        # rejects a caller-owned `_athena_*` key without rejecting the service
        # normalization itself.
        return replace(
            normalized,
            metadata=TrustedTaskMetadata(dict(normalized.metadata), _intake_owner="athena"),
        )

    def _build_task_spec(
        self,
        request: AgentRequest,
        session_id: str,
        *,
        trusted_verification: VerificationEnvironment | None = None,
        trusted_self_host: bool = False,
        trusted_gate_criteria: tuple[str, ...] = (),
        trusted_gate_bundle: Mapping[str, Any] | None = None,
        trusted_mission_plan: Mapping[str, Any] | None = None,
    ) -> TaskSpec:
        return self._task_intake.build_spec(
            request,
            session_id,
            trusted_verification=trusted_verification,
            trusted_self_host=trusted_self_host,
            trusted_gate_criteria=trusted_gate_criteria,
            trusted_gate_bundle=trusted_gate_bundle,
            trusted_mission_plan=trusted_mission_plan,
        )

    @staticmethod
    def _validate_request_metadata(metadata: Mapping[str, Any] | None) -> None:
        if getattr(metadata, "_athena_trusted", False):
            return
        for key in metadata or {}:
            name = str(key)
            if name.startswith("_") or name in _RESERVED_REQUEST_METADATA:
                raise ValueError(f"reserved Athena metadata: {name}")

    @staticmethod
    def _normalize_agent_request_acceptance(request: AgentRequest) -> tuple:
        """Resolve typed and legacy acceptance criteria once at admission."""
        typed = tuple(request.acceptance_criteria or ())
        metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        raw_legacy = metadata.get("acceptance_criteria")
        legacy = decode_criteria(raw_legacy) if raw_legacy is not None else ()
        if typed and legacy and encode_criteria(typed) != encode_criteria(legacy):
            raise ValueError(
                "typed acceptance_criteria conflicts with legacy metadata acceptance_criteria"
            )
        return typed or legacy

    def _workspace_reader(self):
        """Return a workspace instruction reader bound to the current workspace.

        Used by the context compiler to load the hierarchical AGENTS.md chain.
        Returns None when no workspace is bound so the compiler degrades gracefully.
        """
        from athena.context.instructions import hierarchical_agents_md

        root_value = (
            getattr(self._default_workspace, "root", None) if self._default_workspace else None
        )
        if not isinstance(root_value, str) or not root_value:
            return None
        root = root_value

        class _Reader:
            def list_agents_md(_self):
                try:
                    return hierarchical_agents_md(root)
                except Exception:
                    return []

            def snapshot(_self):
                files = _self.list_agents_md()
                revision = tuple(
                    (
                        str(path),
                        int(Path(root, str(path)).stat().st_mtime_ns)
                        if Path(root, str(path)).exists()
                        else 0,
                        len(text),
                    )
                    for path, text in files
                )
                return revision, files

        return _Reader()

    def _build_verifier(
        self,
        *,
        execution,
        dispatcher,
        artifact_store,
        capability_registry,
        model_registry,
        evidence_provider=None,
        inference_broker=None,
    ):
        """Build the acceptance verifier for the kernel."""
        from athena.kernel.verifiers import CompositeVerifier

        return CompositeVerifier(
            execution=execution,
            dispatcher=dispatcher,
            artifact_store=artifact_store,
            capability_registry=capability_registry,
            model_registry=model_registry,
            evidence_provider=evidence_provider,
            inference_broker=inference_broker,
            verification_environment_resolver=self._resolve_verification_environment,
        )

    def _resolve_verification_environment(self, task: TaskSpec):
        """Resolve a task's proof environment from service-owned authority.

        Task metadata contains a durable identity so a proof can survive a
        restart.  It is not a capability grant.  Rebuild the environment from
        the current host checkout and compare the persisted identity and the
        frozen gate bundle before returning it to the verifier.
        """
        metadata = task.metadata or {}
        if not bool(metadata.get("_athena_self_host")):
            return None
        record = metadata.get("_athena_verification_environment")
        bundle_record = metadata.get("_athena_gate_bundle")
        if not isinstance(record, Mapping) or not isinstance(bundle_record, Mapping):
            raise ValueError("self-host proof is missing its trusted authority bundle")
        project_root = str(record.get("project_root") or "")
        bundle_root = str(bundle_record.get("project_root") or "")
        if not project_root or project_root != bundle_root:
            raise ValueError("self-host proof authority roots do not match")

        # This also validates that the persisted root is the Athena checkout,
        # that the host toolchain is still bootstrapped, and that the base
        # checkout has not changed since the proof authority was captured.
        current_bundle = SelfHostGateBundle.capture(project_root, allow_dirty=True)
        if current_bundle.gate_bundle_hash != str(bundle_record.get("gate_bundle_hash") or ""):
            raise ValueError("self-host proof gate bundle is stale")
        expected = VerificationEnvironment.from_project(
            project_root,
            include_project_root=True,
            include_rust=True,
            task_id=task.id,
        )
        return VerificationEnvironment.from_record(record, expected=expected)

    def _make_judge_broker(self):
        """Return a late-bound broker for task-scoped judge inference."""

        async def _broker(*, task, system_prompt, user_prompt):
            kernel = self._kernel
            if kernel is None:
                raise RuntimeError("judge broker: kernel not constructed")
            return await kernel.judge_subturn(
                task=task,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

        return _broker

    async def _project_index_for_completion(self, root: str):
        """Read a source-verified index for completion planning."""
        coordinator = self._project_index_coordinator
        if coordinator is None:
            return None
        return await coordinator.current(root, freshness="source_verified")

    async def _project_profile_for_completion(self, task: TaskSpec):
        """Use the service-owned index as the completion profile source."""
        workspace = getattr(task, "workspace", None)
        root = getattr(workspace, "root", None)
        if not root:
            return None
        index = await self._project_index_for_completion(root)
        if index is None:
            return None
        return dict(getattr(index, "profile", {}) or {})

    async def _verification_evidence(self, task: TaskSpec) -> dict[str, Any]:
        """Collect a bounded projection of durable task observations for a judge."""
        evidence: dict[str, Any] = {"objective": task.objective, "task_id": task.id}
        if self._store_events is not None:
            events = await self._store_events.list_for_task(task.id)
            evidence["events"] = [
                {
                    "sequence": event.sequence,
                    "type": event.type,
                    "payload": dict(event.payload or {}),
                }
                for event in events[-100:]
            ]
        if self._store_executions is not None:
            evidence["executions"] = [
                dict(row) for row in (await self._store_executions.list_for_task(task.id))[-25:]
            ]
        if self._store_mutations is not None:
            evidence["mutations"] = [
                dict(row) for row in (await self._store_mutations.list_for_task(task.id))[-25:]
            ]
        result = await self.get_result(task.id)
        if result is not None:
            evidence["result"] = {
                "status": result.status.value,
                "summary": result.summary,
                "unresolved": list(result.unresolved),
                "artifacts": [getattr(ref, "uri", str(ref)) for ref in result.artifacts],
            }
        result_data = evidence.get("result", {})
        if self._research_store is not None:
            try:
                workspace_id = task.workspace.id if task.workspace else None
                sources = await self._research_store.list_sources(
                    task_id=task.id,
                    project_id=workspace_id,
                    limit=50,
                )
                research_evidence = await self._research_store.list_evidence(
                    task_id=task.id,
                    project_id=workspace_id,
                    limit=75,
                )
                gaps = await self._research_store.list_gaps(
                    task_id=task.id,
                    limit=100,
                )
                evidence["research"] = {
                    "sources": [source.to_record() for source in sources],
                    "evidence": [item.to_record() for item in research_evidence],
                    "gaps": [gap.to_record() for gap in gaps],
                }
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning("research evidence lookup failed: %s", exc)
        try:
            world_state = await self.world_state(task.id).snapshot(
                workspace_root=task.workspace.root if task.workspace else None,
            )
            evidence["world_state"] = world_state
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning("world-state evidence lookup failed: %s", exc)
        return {
            "evidence": evidence,
            "world_state": evidence.get("world_state", {}),
            "artifacts": result_data.get("artifacts", []),
            "unresolved_failures": result_data.get("unresolved", []),
        }

    def _dispatch_factory(self, task: TaskSpec):
        if self._dispatcher is None:
            raise RuntimeError("AthenaService not started")
        ws = task.workspace or self._default_workspace
        profile = (task.metadata or {}).get("autonomy") or self.config.autonomy_level.value
        return CapabilityDispatchShim(self._dispatcher, ws, profile=profile)

    @staticmethod
    def _log_background_failure(label: str):
        """Consume detached task exceptions instead of losing recovery truth."""

        def _done(task: asyncio.Task) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _logger.warning("%s failed: %s", label, exc, exc_info=True)

        return _done

    @staticmethod
    def _forward_events(events: EventStore):
        async def sink(event: Event) -> None:
            await events.append(event)

        return sink

    def _build_model_router(self, model_registry, cfg) -> ModelRouter:
        """Construct the task model router (the single sanctioned site).

        ServiceLifecycle wires this in during startup; construction stays on
        the facade so the router-authority boundary remains mechanical.
        """
        return ModelRouter(
            model_registry,
            role_policies=self._role_policies(cfg.model_roles),
            usage_provider=self._provider_usage_store,
        )

    def _role_policies(self, raw: Any) -> dict:
        """Normalize config ``model_roles`` into router role policies.

        Accepts ``{"summarizer": {"allowed": ["x/y"], "privacy": "...",
        "max_cost_usd": 0.01}}``. Invalid entries are logged and skipped.
        """
        from decimal import Decimal, InvalidOperation

        from athena.protocol.tasks import ModelPolicy

        out: dict[str, Any] = {}
        for role, spec in dict(raw or {}).items():
            if not isinstance(spec, Mapping):
                _logger.warning("model_roles[%r] ignored: not a table", role)
                continue
            allowed = tuple(str(a) for a in (spec.get("allowed") or ()) if a)
            max_cost = None
            raw_cost = spec.get("max_cost_usd")
            if raw_cost is not None:
                try:
                    max_cost = Decimal(str(raw_cost))
                except (InvalidOperation, ValueError):
                    _logger.warning("model_roles[%r].max_cost_usd invalid: %r", role, raw_cost)
            out[str(role)] = ModelPolicy(
                role=str(role),
                allowed=allowed,
                privacy=str(spec.get("privacy") or "local-preferred"),
                require_tools=bool(spec.get("require_tools", False)),
                max_cost_usd=max_cost,
                routing_preference=str(spec.get("routing_preference") or "balanced"),
                min_quality_tier=(
                    str(spec["min_quality_tier"]).strip()
                    if spec.get("min_quality_tier") is not None
                    else None
                ),
                require_declared_quality=bool(spec.get("require_declared_quality", False)),
                max_model_attempts=int(spec.get("max_model_attempts", 2)),
            )
        return out

    def _make_model_summarizer(self, model_registry: Any):
        """Build the compression summarizer used by the context compiler.

        Delegates to the kernel's ``utility_inference`` (the single inference
        path for task-less auxiliary model work): same router, same metering,
        same usage store. On any failure it returns None, and
        ContextCompressor falls back to deterministic truncation — so
        offline/test operation is unaffected.
        """
        if model_registry is None:
            return None

        async def _summarize(text: str, *, task=None, max_tokens: int | None = None) -> str | None:
            kernel = self._kernel
            if kernel is None:
                return None
            prompt = (
                "Summarize the following complete agent-work transcript excerpt "
                "into at most 6 sentences, preserving decisions, file changes, "
                "and unresolved issues. Output ONLY the summary.\n\n" + text
            )
            if max_tokens is not None:
                prompt += f"\n\nKeep the response within {max_tokens} estimated tokens."
            if task is not None:
                return await kernel.task_utility_inference(
                    task=task,
                    system_prompt="",
                    user_prompt=prompt,
                    role="summarizer",
                )
            return await kernel.utility_inference(
                system_prompt="", user_prompt=prompt, role="summarizer"
            )

        return _summarize

    def _make_interpreter(self):
        """Build the interpreter fusion extension bound to this service's kernel.

        The broker closes over ``self._kernel`` late: the extension is passed
        INTO the kernel constructor, so the kernel does not exist yet when
        this method runs. The closure resolves the kernel at subturn time —
        if the kernel never lands (construction failure), the broker raises
        and fusion is skipped (the primary loop is unaffected).
        """

        async def _broker(*, context, system_prompt, user_prompt):
            kernel = self._kernel
            if kernel is None:
                raise RuntimeError("interpreter broker: kernel not constructed")
            return await kernel.interpreter_subturn(
                context=context,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

        return InterpreterExtension(inference_broker=_broker)

    @staticmethod
    async def _sync_skills(lifecycle: SkillLifecycle, discovered) -> None:
        """Synchronize discovered skills into the lifecycle catalog (P1-32).

        Install skills that aren't already present; match on (name, version) so
        known skills are not reimported as duplicates.
        """
        try:
            existing = await lifecycle.list()
        except Exception as exc:
            _logger.warning("skill lifecycle list failed: %s", exc)
            existing = []
        known = {(s.name, s.version) for s in existing}
        for skill in discovered:
            key = (skill.name, skill.version)
            if key in known:
                continue
            try:
                await lifecycle.install(skill)
                known.add(key)
            except Exception as exc:
                _logger.warning("skill install failed for %s: %s", skill.name, exc)
                continue

    async def _register_core_capabilities(
        self, *, registry, workspace, execution, memory, skills_store, research_store=None
    ) -> None:
        """Compose core capabilities through the extracted registration owner."""
        from athena.service.core_capabilities import register_core_capabilities

        await register_core_capabilities(
            self,
            registry=registry,
            workspace=workspace,
            execution=execution,
            memory=memory,
            skills_store=skills_store,
            research_store=research_store,
        )

    def register_shutdown_hook(self, name: str, hook) -> None:
        """Register a capability-owned resource teardown (P1-32)."""
        self._shutdown_hooks.append((name, hook))

    async def _run_shutdown_hooks(self) -> dict[str, Any]:
        outcome: dict[str, Any] = {"failures": [], "hooks": []}
        for name, hook in reversed(self._shutdown_hooks):
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook()
                else:
                    result = hook()
                    if inspect.isawaitable(result):
                        await result
                outcome["hooks"].append({"name": name, "status": "ok"})
            except Exception as exc:
                _logger.warning("shutdown hook %s failed: %s", name, exc)
                failure = {"name": name, "error": str(exc)}
                outcome["hooks"].append({"name": name, "status": "failed", **failure})
                outcome["failures"].append(failure)
        self._shutdown_hooks.clear()
        return outcome

    # ------------------------------------------------------------------ #
    # Fusion engines: shadow execution + execution-grounded world state
    # ------------------------------------------------------------------ #
    async def _cleanup_task_affordances(self, task, result) -> None:
        task_id = getattr(task, "id", None)
        if task_id and self._fabric is not None:
            self._fabric.unregister_task(task_id)
        if task_id:
            self._scratch.discard_task(task_id)
            if self._workflow_store is not None:
                try:
                    await self._workflow_store.delete_for_task(task_id)
                except Exception as exc:
                    _logger.warning("task workflow cleanup failed for %s: %s", task_id, exc)

    async def _on_mutation_completed(
        self,
        task_id: str | None,
        resource: str,
        mutation_id: str | None = None,
        mutation_event_sequence: int | None = None,
        mutation_sequence: int | None = None,
    ) -> None:
        """Invalidate claims from the canonical mutation path.

        Without a workspace/project binding on the claim, the safe boundary
        available here is the owning task. Never mark every task's claims
        stale merely because one task mutated a path with the same spelling.
        Claims created by the fusion path also carry the event sequence at
        which their evidence was established, so later mutations can be
        measured precisely by ``TaskWorldState``.
        """
        if not resource:
            return
        coordinator = getattr(self, "_project_index_coordinator", None)
        if coordinator is not None:
            coordinator.mark_stale_for_paths([resource])
        cache = getattr(self, "_world_states", {})
        for world_state in list(cache.values()):
            if task_id is None or world_state.task_id == task_id:
                world_state.claims.invalidate_for_paths(
                    [resource],
                    mutation_id=mutation_id,
                    mutation_sequence=mutation_sequence,
                    mutation_event_sequence=mutation_event_sequence,
                )
        store = getattr(self, "_world_state_store", None)
        if store is not None and task_id is not None:
            try:
                await store.invalidate_for_paths(
                    task_id,
                    [resource],
                    mutation_id=mutation_id,
                    mutation_sequence=mutation_sequence,
                    mutation_event_sequence=mutation_event_sequence,
                )
            except Exception as exc:
                _logger.warning("durable claim invalidation failed: %s", exc)

    def shadow_engine(self):
        """Speculative-execution engine bound to this service's dispatcher."""
        from athena.shadow.engine import ShadowEngine

        if getattr(self, "_shadow", None) is None:
            state_root = getattr(self, "_runtime_state_root", None)
            roots_parent = os.path.join(state_root, "shadows") if state_root else None
            self._shadow = ShadowEngine(
                roots_parent=roots_parent,
                state_root=state_root,
            )
        if self._shadow.dispatcher is None and self._dispatcher is not None:
            self._shadow.bind(self._dispatcher)
        self._shadow.bind_service(self)
        return self._shadow

    def _candidate_git_view(self, task_id: str | None) -> dict[str, str] | None:
        """Resolve Git's base metadata and candidate work tree for a task."""
        if not task_id:
            return None
        shadow = self.shadow_engine()
        branches = getattr(shadow, "list_branches", lambda: ())()
        for branch in reversed(branches):
            if getattr(branch, "task_id", None) != task_id:
                continue
            if getattr(branch, "status", None) not in {"PROPOSED", "EXECUTING", "VERIFIED"}:
                continue
            base_root = getattr(getattr(branch, "base_workspace", None), "root", None)
            candidate_root = getattr(getattr(branch, "shadow_workspace", None), "root", None)
            if base_root and candidate_root:
                return {
                    "base_root": str(base_root),
                    "candidate_root": str(candidate_root),
                    "branch_id": str(getattr(branch, "id", "")),
                }
        return None

    def fusion_orchestrator(self):
        """Return the service-owned, single-agent fusion orchestrator.

        Production composition uses typed ports; the whole-service form is
        only a compatibility path for external construction.
        """
        if getattr(self, "_fusion", None) is None:
            shadow = self.shadow_engine()
            self._fusion = FusionComposition(
                shadow=shadow,
                checkpoints=getattr(self, "_checkpoints", None)
                or getattr(shadow, "_checkpoints", None),
                task_store=self._store_tasks,
                event_store=self._store_events,
                runtime_session_store=self._store_runtime_sessions,
                context_block_store=self._context_block_store,
                fabric=self._fabric,
                workflow_store=self._workflow_store,
                synthesis=getattr(self, "_synthesis", None),
                dispatcher=self._dispatcher,
                world_state_store=getattr(self, "_world_state_store", None),
                world_state_provider=self.world_state,
                reality_coordinator=getattr(self, "_reality_coordinator", None),
                reality_gate=self._reality_gate,
                task_manager=self._task_manager,
                session_store=getattr(self, "_sessions", None),
                message_store=getattr(self, "_store_messages", None),
                principal_id=getattr(getattr(self, "config", None), "cache_namespace", None),
            ).build()
        return self._fusion

    def world_state(self, task_id: str | None = None):
        """Execution-grounded structured reality for one task."""
        from athena.worldstate import TaskWorldState

        cache = getattr(self, "_world_states", None)
        if cache is None:
            cache = {}
            self._world_states = cache
        ws = cache.get(task_id or "")
        if ws is None:
            ws = TaskWorldState(service=self, task_id=task_id)
            if task_id:
                cache[task_id] = ws
        return ws

    def _register_providers(self, registry: ProviderRegistry) -> None:
        self._provider_runtime.register(registry)

    def _build_provider(self, pc: ProviderConfig, credential_id: str | None = None) -> Any:
        return self._provider_runtime.build(pc, credential_id)

    async def _connect_mcp(self) -> None:
        await self._mcp_runtime.connect()

    async def start_mcp_supervisor(self) -> None:
        await self._mcp_runtime.start_supervisor()

    async def _handle_mcp_transport_failure(self, connection_id: str, error: BaseException) -> None:
        await self._mcp_runtime.handle_transport_failure(connection_id, error)

    async def _connect_mcp_server(self, server: MCPConfig) -> dict[str, Any]:
        return await self._mcp_runtime.connect_server(server)

    async def mcp_reconnect(self, name: str) -> dict[str, Any]:
        return await self._mcp_runtime.reconnect(name)

    def _resolve_api_key(self, pc: ProviderConfig, *, credential_id: str | None = None) -> str:
        return self._provider_runtime.resolve_api_key(pc, credential_id=credential_id)

    async def _preflight_hermes_referee(self) -> None:
        """Run the optional safety probe without blocking normal startup."""
        await self._hermes_runtime.preflight()

    async def _require_verified_hermes_referee(self) -> None:
        """Refuse self-host work only under required Hermes supervision."""
        await self._hermes_runtime.require_verified()

    def _configure_hermes_referee(self) -> None:
        """Build the optional transport after the secret boundary exists."""
        self._hermes_runtime.configure()

    async def _close_hermes_referee(self) -> None:
        """Close Hermes transport resources through their owning runtime."""
        await self._hermes_runtime.close()

    # ------------------------------------------------------------------ #
    # Internal accessors
    # ------------------------------------------------------------------ #
    async def _require_provider_ready(self) -> None:
        registry = self._model_registry
        if registry is None:
            readiness = {"state": "unconfigured"}
        else:
            probe = getattr(registry, "readiness", None)
            if callable(probe):
                readiness = probe()
            else:
                # Keep small host/test registries fail-closed without making
                # them implement the richer diagnostic surface immediately.
                names = getattr(registry, "names", None)
                readiness = {"state": "ready" if callable(names) and names() else "unconfigured"}
        if not isinstance(readiness, Mapping):
            readiness = {"state": "degraded"}
        if readiness.get("state") != "ready":
            raise ModelProviderUnconfigured(
                "No usable model provider is ready. Configure credentials and provider readiness before submitting agent work.",
                provider_state=readiness.get("state", "unconfigured"),
            )

    async def _admit_model_roles(self, base_policy, roles: set[str]) -> None:
        router = self._router
        if router is None:
            raise ModelProviderUnconfigured(
                "Model routing is not initialized; agent work cannot be admitted.",
                provider_state="unconfigured",
            )
        for role in sorted(roles, key=lambda value: (value != "primary", value)):
            policy = base_policy if role == base_policy.role else replace(base_policy, role=role)
            try:
                await router.select(policy=policy)
            except ProviderError as exc:
                raise ModelProviderUnconfigured(
                    "No ready model satisfies this task's provider, role, capability, or policy constraints.",
                    provider_state="request_unavailable",
                    allowed_models=list(policy.allowed or ()),
                    role=role,
                ) from exc

    @staticmethod
    def _required_model_roles(
        policy,
        metadata: Mapping[str, Any] | None = None,
        *,
        criteria: Any = None,
    ) -> set[str]:
        roles = {"primary"}
        role = str(getattr(policy, "role", "primary") or "primary")
        if role:
            roles.add(role)
        for item in criteria or ():
            if isinstance(item, str):
                if not item.strip().lower().startswith("command:"):
                    roles.add("judge")
                continue
            verification = getattr(item, "verification", None)
            raw_type = getattr(getattr(verification, "type", None), "value", None)
            raw_type = raw_type or getattr(verification, "type", None)
            if str(raw_type or "").casefold() == "model_judgment":
                roles.add("judge")
        data = metadata or {}
        for key in ("mandatory_model_roles", "required_model_roles", "mandatory_roles"):
            raw_roles = data.get(key) if isinstance(data, Mapping) else None
            if isinstance(raw_roles, str):
                raw_roles = (raw_roles,)
            if isinstance(raw_roles, (list, tuple, set, frozenset)):
                roles.update(str(value).strip() for value in raw_roles if str(value).strip())
        return roles

    def _require_task_manager(self) -> TaskManager:
        if self._task_manager is None:
            raise RuntimeError("AthenaService not started")
        return self._task_manager

    def _require_worker(self) -> TaskWorker:
        if self._worker is None:
            raise RuntimeError("AthenaService not started")
        return self._worker

    def _require_cancellations(self) -> CancellationManager:
        if self._cancellations is None:
            raise RuntimeError("AthenaService not started")
        return self._cancellations

    def _require_execution(self):
        if self._execution is None:
            raise RuntimeError("AthenaService not started")
        return self._execution

    def _require_events(self) -> EventStore:
        if self._store_events is None:
            raise RuntimeError("AthenaService not started")
        return self._store_events


# Privacy values ModelRouter treats as a hard LOCAL-only gate. OFFLINE
# autonomy and network-DENY workspaces narrow task model policy into this
# set; "local-preferred" (the default) is deliberately NOT in it because the
# router only biases, never hard-gates, under that value.
_OFFLINE_MODEL_PRIVACY = frozenset({"offline", "local"})
