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
import json
import logging
import os
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import httpx

from athena.artifacts.store import ArtifactStore
from athena.affordances import (
    CapabilityFabric,
    GeneratedCapabilityStore,
    ScratchManager,
)
from athena.capabilities.delegate import DelegateCapability
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.execute import ExecuteCapability
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.memory import MemoryCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.skills import SkillsCapability
from athena.context.compiler import ContextCompiler
from athena.execution.manager import ExecutionManager
from athena.state.external_effects import ExternalEffectStore
from athena.execution.environment import VerificationEnvironment
from athena.interpreter import InterpreterExtension
from athena.models.compat.profiles import ModelProfile, resolve_profile
from athena.hermes import (
    HermesAgentEvaluator,
    HermesReferee,
    ReviewPacket,
)
from athena.kernel.kernel import AgentKernel
from athena.kernel.dispatch import CapabilityDispatchShim
from athena.mcp.adapter import MCPAdapter
from athena.mcp.client import MCPClient
from athena.memory.store import MemoryStore
from athena.models.providers.anthropic import AnthropicProvider
from athena.models.providers.fake import FakeModelProvider
from athena.models.providers.openai_compat import OpenAICompatProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.policy.credentials import SecretError, SecretManager
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

if TYPE_CHECKING:
    from athena.state.input_requests import InputRequestStore

from athena.protocol.events import Event
from athena.protocol.ids import new_id
from athena.protocol.errors import ModelProviderUnconfigured, ProviderError
from athena.protocol.policy import ApprovalScope
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    CapabilityPolicy,
    ContextRef,
    ResourceBudget,
    MutationMode,
    NetworkPolicy,
    TaskSpec,
    TaskStatus,
    TERMINAL_STATUSES,
    WorkspaceSpec,
)
from athena.protocol.task_codec import decode_criteria, encode_criteria

from athena.service.candidates import CandidateService
from athena.service.interaction import OperatorInteractionService
from athena.service.task_api import TaskAPI
from athena.service.operator_query import OperatorQueryService
from athena.service.recovery import RecoveryCoordinator
from athena.service.lifecycle import ServiceLifecycle
from athena.service.self_host import SelfHostService
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


class AthenaService:
    """The application API — composition root above the core services."""

    # ------------------------------------------------------------------ #
    # Construction / factories
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        *,
        config: AthenaConfig | None = None,
        device_provider=None,
        hermes_referee: HermesReferee | None = None,
    ) -> None:
        self.config = config or AthenaConfig()
        self._background_tasks: set[asyncio.Task] = set()
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
        self._hermes_referee = hermes_referee
        self._hermes_adapter: HermesAgentEvaluator | None = None
        self._hermes_referee_owned = False
        self._hermes_status_error: str | None = None
        self._started = False
        self._recovery_status = "not_started"
        self._recovery_summary: dict[str, int] = {}
        self._recovery_error: str | None = None
        self._startup_health: dict[str, Any] = {
            "status": "not_started",
            "checks": {},
            "blocking_failures": [],
        }
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
        self._kernel: AgentKernel | None = None
        self._task_manager: TaskManager | None = None
        self._worker: TaskWorker | None = None
        self._worker_task: asyncio.Task | None = None
        self._approval_recovery_tasks: set[asyncio.Task] = set()
        self._watch_poll_task: asyncio.Task | None = None
        self._shutdown_hooks: list[tuple[str, Any]] = []
        self._resource_finalizer: Any = None
        self._shutdown_status: dict[str, Any] = {"state": "not_started"}
        self._budgets: BudgetTracker | None = None
        self._cancellations: CancellationManager | None = None
        self._delegation: DelegationManager | None = None
        self._memory: MemoryStore | None = None
        self._skills: SkillStore | None = None
        self._skill_lifecycle: SkillLifecycle | None = None
        self._scheduler: Scheduler | None = None
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
        self._external_effect_store: ExternalEffectStore | None = None
        self._knowledge: Any = None
        self._provider_usage_store: Any = None
        self._runtime_state_root: Path | None = None
        self._router: ModelRouter | None = None
        # Self-host orchestration mechanism (P1-10): constructed against
        # the facade; every authority seam still resolves through self.
        self._self_host = SelfHostService(self)
        self._candidates = CandidateService(self)

        self._mcp_clients: list[MCPClient] = []
        self._mcp_connection_status: dict[str, dict[str, Any]] = {}
        self._mcp_reconnect_failures: dict[str, int] = {}

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
        return await RecoveryCoordinator(self)._quarantine_tasks_for_packs(
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
        scheduler = getattr(self, "_scheduler", None)
        watches = getattr(self, "_watch_registry", None)
        computer = getattr(self, "_computer", None)
        browser = getattr(self, "_browser", None)
        memory = getattr(self, "_memory", None)
        model_registry = getattr(self, "_model_registry", None)
        model_readiness = (
            model_registry.readiness()
            if model_registry is not None and callable(getattr(model_registry, "readiness", None))
            else {"state": "unconfigured", "providers": {}}
        )
        embedding_provider = (
            getattr(memory, "embedding_provider", None) if memory is not None else None
        )
        embedding_health = (
            embedding_provider.health()
            if embedding_provider is not None
            and callable(getattr(embedding_provider, "health", None))
            else {
                "configured": False,
                "available": False,
                "state": "unconfigured",
                "reason": "no embedding provider configured",
            }
        )
        return {
            "scheduler": scheduler.health() if scheduler is not None else {"health": "stopped"},
            "watch": watches.health() if watches is not None else {"health": "stopped"},
            "computer": computer.health() if computer is not None else dict(self._computer_health),
            "browser": browser.health() if browser is not None else dict(self._browser_health),
            "memory_embeddings": embedding_health,
            "model": {
                "state": model_readiness.get("state", "unknown"),
                "configured": bool(model_readiness.get("providers")),
                "providers": model_readiness.get("providers", {}),
                "reason": (
                    None if model_readiness.get("state") == "ready" else "no ready model provider"
                ),
            },
            "mcp": self.mcp_status(),
            "optional_capabilities": {
                name: dict(value)
                for name, value in sorted(self._optional_capability_health.items())
            },
            "resources": (
                self._resource_finalizer.health()
                if self._resource_finalizer is not None
                else {"state": "not_started"}
            ),
            "shutdown": dict(self._shutdown_status),
        }

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
                    spec = self._delegate_registry.get(identifier)
                    if (
                        not spec.command
                        and self._delegate_registry.connector_for(identifier) is None
                    ):
                        reason = "delegate has no trusted connector"
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
        """Return configured MCP transport/discovery state for operators."""
        status = dict(self._mcp_connection_status)
        configured = {server.name: server for server in self.config.mcp_servers}
        for name, server in configured.items():
            status.setdefault(
                name,
                {
                    "id": name,
                    "configured": True,
                    "required": bool(server.required),
                    "state": "configured",
                    "transport": "http" if server.url else "stdio",
                    "tool_count": 0,
                    "last_successful_connection": None,
                    "last_error": None,
                },
            )
        for client in self._mcp_clients:
            status[client.connection_id] = client.health()
        return {name: dict(status[name]) for name in sorted(status)}

    @property
    def _hermes_supervision_mode(self) -> HermesSupervisionMode:
        """Return the explicit operator policy for self-host supervision."""
        return self.config.hermes_referee.supervision_mode

    @property
    def _hermes_supervision_active(self) -> bool:
        """Whether Hermes may contribute evidence at self-host checkpoints."""
        return (
            self.config.hermes_referee.transport_enabled
            and self._hermes_supervision_mode is not HermesSupervisionMode.OFF
            and self._hermes_referee is not None
        )

    async def hermes_referee_status(self) -> dict[str, Any]:
        """Return operator-safe Hermes configuration and connectivity status."""
        settings = self.config.hermes_referee
        if not settings.transport_enabled:
            return {
                "enabled": False,
                "self_host_supervision": settings.supervision_mode.value,
                "state": "disabled",
                "profile": settings.profile,
                "endpoint": settings.endpoint,
            }
        if self._hermes_adapter is None:
            return {
                "enabled": settings.transport_enabled,
                "self_host_supervision": settings.supervision_mode.value,
                "state": (
                    "configured_unverified"
                    if self._hermes_referee is not None and self._hermes_status_error is None
                    else "error"
                ),
                "profile": settings.profile,
                "endpoint": settings.endpoint,
                "error": self._hermes_status_error,
            }
        try:
            preflight = await self._hermes_adapter.preflight()
        except httpx.HTTPError as exc:
            return {
                "enabled": settings.transport_enabled,
                "self_host_supervision": settings.supervision_mode.value,
                "state": "disconnected",
                "profile": settings.profile,
                "endpoint": settings.endpoint,
                "safety_verified": False,
                "error": str(exc),
            }
        except Exception as exc:
            from athena.hermes.agent_adapter import HermesRefereeSafetyError

            return {
                "enabled": settings.transport_enabled,
                "self_host_supervision": settings.supervision_mode.value,
                "state": ("unsafe" if isinstance(exc, HermesRefereeSafetyError) else "error"),
                "safety_verified": False,
                "profile": settings.profile,
                "endpoint": settings.endpoint,
                "error": str(exc),
            }
        return {
            "enabled": settings.transport_enabled,
            "self_host_supervision": settings.supervision_mode.value,
            "state": "safety_verified",
            "safety_verified": True,
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "policy_fingerprint": preflight.policy_fingerprint,
            "runtime": dict(preflight.capabilities.get("runtime") or {}),
            "referee": dict(preflight.capabilities.get("referee") or {}),
            "build": dict(preflight.capabilities.get("build") or {}),
        }

    async def _recover_approved_continuations(
        self,
        *,
        continuations: ContinuationStore,
        task_store: TaskStore,
        task_manager: TaskManager,
        kernel: AgentKernel,
    ) -> None:
        return await RecoveryCoordinator(self)._recover_approved_continuations(
            continuations=continuations,
            task_store=task_store,
            task_manager=task_manager,
            kernel=kernel,
        )

    def _track_approval_recovery(self, task_id: str, recovery: asyncio.Task):
        return RecoveryCoordinator(self)._track_approval_recovery(task_id, recovery)

    async def _recover_answered_input_requests(
        self,
        *,
        input_requests: InputRequestStore,
        task_store: TaskStore,
        task_manager: TaskManager,
        kernel: AgentKernel,
    ) -> None:
        return await RecoveryCoordinator(self)._recover_answered_input_requests(
            input_requests=input_requests,
            task_store=task_store,
            task_manager=task_manager,
            kernel=kernel,
        )

    # ------------------------------------------------------------------ #
    # Application API
    # ------------------------------------------------------------------ #
    async def submit(self, request: AgentRequest, *, wait: bool = True) -> TaskSpec:
        return await TaskAPI(self).submit(request, wait=wait)

    async def submit_spec(
        self,
        spec: TaskSpec,
        *,
        wait: bool = False,
        user_request: Any | None = None,
        trusted: bool = False,
    ) -> TaskSpec:
        return await TaskAPI(self).submit_spec(
            spec, wait=wait, user_request=user_request, trusted=trusted
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
        return await self._self_host._plan_next_self_host_item(
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
        return await self._self_host._verify_self_host_performance(
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
        return await self._self_host._verify_self_host_completion(
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
        return await self._self_host._run_hermes_mission_referee(
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
    ):
        return await TaskAPI(self)._enqueue_spec(
            task_manager, spec, wait=wait, user_request=user_request
        )

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
            return
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

    @staticmethod
    def _self_host_verification_environment(
        workspace,
        *,
        include_project_root: bool = False,
        include_rust: bool = False,
        task_id: str | None = None,
    ) -> VerificationEnvironment:
        """Validate the only supported self-host target and its proof tools."""
        if workspace is None:
            raise ValueError("athena self requires an Athena source checkout")
        root = Path(workspace.root).resolve()
        if not (root / "src" / "athena" / "__init__.py").is_file():
            raise ValueError("athena self must target an Athena source checkout")
        try:
            import tomllib

            project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError("Athena pyproject.toml could not be validated") from exc
        if project.get("project", {}).get("name") != "athena-agent":
            raise ValueError("athena self must target the athena-agent checkout")
        try:
            return VerificationEnvironment.from_project(
                str(root),
                include_project_root=include_project_root,
                include_rust=include_rust,
                task_id=task_id,
            )
        except ValueError as exc:
            raise ValueError(f"athena self: {exc}") from exc

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
        from athena.capabilities.dispatcher import SuspendedCall
        from athena.protocol.capabilities import (
            CapabilityRequest,
            CapabilityResult,
            CapabilityResultStatus,
            CapabilityRequestOrigin,
        )

        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("AthenaService not started")
        ws = self._default_workspace
        request = CapabilityRequest(
            capability_id="execute",
            arguments={
                "language": language,
                "code": source,
                **({"cwd": cwd} if cwd is not None else {}),
            },
            # Direct escapes have no model task, so the task FK remains NULL;
            # their session transcript and approval record are still durable.
            task_id=None,
            session_id=session_id,
            origin=CapabilityRequestOrigin.USER_DIRECT,
        )

        async def _dispatch():
            return await dispatcher.dispatch(
                request,
                workspace=ws,
                profile=self.config.autonomy_level,
            )

        result = await _dispatch()
        if isinstance(result, SuspendedCall):
            approval_id = result.approval_id
            scopes = [s.value for s in result.decision.approval_scope_options]
            if not approval_id:
                return {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "approval required but no approval id was issued",
                    "status": "failed",
                }
            if on_approval is None:
                return {
                    "approval_id": approval_id,
                    "scopes": scopes,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "approval required",
                    "status": "approval_required",
                }
            decision = on_approval(approval_id, scopes)
            if hasattr(decision, "__await__"):
                decision = await decision
            granted = bool(getattr(decision, "granted", decision))
            scope = getattr(decision, "scope", None)
            await self.approve(approval_id, granted=granted, scope=scope)
            if not granted:
                result = CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error="denied: approval not granted",
                    metadata={"decision": "deny"},
                )
            else:
                # The approval grant is exact and the request object is
                # unchanged, so the dispatcher re-evaluates the same call.
                result = await _dispatch()

        if not isinstance(result, CapabilityResult):
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": "direct execute returned an invalid capability result",
                "status": "failed",
            }

        ok = result.status is CapabilityResultStatus.OK
        metadata = dict(result.metadata or {})
        output = result.output or ""
        error = result.error or ""
        payload = {
            "exit_code": metadata.get("exit_code", 0 if ok else 1),
            "stdout": output if ok else output,
            "stderr": "" if ok else error,
            "status": "completed" if ok else "failed",
            "duration_ms": metadata.get("duration_ms"),
            "artifact_uri": result.ref_uri,
            "session_id": session_id,
        }
        await self._record_direct_result(
            session_id=session_id,
            source=source,
            language=language,
            result=result,
            inject_into_context=inject_into_context,
        )
        return payload

    async def _record_direct_result(
        self,
        *,
        session_id: str | None,
        source: str,
        language: str,
        result: Any,
        inject_into_context: bool,
    ) -> None:
        """Persist a direct execution as a capability transcript message."""
        if not session_id or self._store_messages is None:
            return
        from athena.protocol.messages import (
            CapabilityCallBlock,
            CapabilityResultBlock,
            Message,
            Provenance,
            Role,
            SourceType,
            utcnow,
        )

        if self._sessions is not None and await self._sessions.get(session_id) is None:
            create_session = self._sessions.create
            kwargs: dict[str, Any] = {}
            try:
                if "principal_id" in inspect.signature(create_session).parameters:
                    kwargs["principal_id"] = self.config.cache_namespace
            except (TypeError, ValueError):
                # A legacy adapter may not expose an inspectable signature;
                # its positional create(session_id) contract remains valid.
                pass
            await create_session(session_id, **kwargs)
        call_id = getattr(result, "call_id", "") or new_id("call")
        block_call = CapabilityCallBlock(
            call_id=call_id,
            capability_id="execute",
            arguments={"language": language, "code": source},
        )
        block_result = CapabilityResultBlock(
            call_id=call_id,
            capability_id="execute",
            ok=result.status.value == "ok",
            output=result.output or "",
            error=result.error,
            metadata=dict(result.metadata or {}),
            ref_uri=result.ref_uri,
        )
        await self._store_messages.append_to_session(
            session_id,
            Message(
                id=new_id("msg"),
                role=Role.CAPABILITY,
                blocks=(block_call, block_result),
                created_at=utcnow(),
                provenance=Provenance(source_type=SourceType.CAPABILITY),
                metadata={
                    "session_id": session_id,
                    "direct_execution": True,
                    "inject_into_context": inject_into_context,
                },
            ),
        )

    async def run_task(self, task_id: str) -> TaskSpec:
        return await TaskAPI(self).run_task(task_id)

    async def wait_for(self, task_id: str, *, timeout: float | None = None) -> TaskSpec:
        return await TaskAPI(self).wait_for(task_id, timeout=timeout)

    async def get_task(self, task_id: str) -> TaskSpec:
        return await TaskAPI(self).get_task(task_id)

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

    async def list_packs(self, query: str | None = None) -> list[dict[str, Any]]:
        """List installed declarative packs and their live health."""
        manager = self._pack_manager
        if manager is None:
            return []
        rows = list(await manager.list())
        if query:
            needle = query.casefold()
            rows = [
                row
                for row in rows
                if needle in str(row.get("id", "")).casefold()
                or needle in str(row.get("publisher", "")).casefold()
            ]
        for row in rows:
            state = await manager._store.get(str(row["id"]))
            if state is not None:
                row["health_detail"] = manager.health(state)
        return rows

    async def inspect_pack(self, pack_id: str) -> dict[str, Any] | None:
        if self._pack_manager is None:
            return None
        try:
            return await self._pack_manager.inspect_installed(pack_id)
        except KeyError:
            return None

    async def install_pack(self, source_path: str, *, enable: bool = True) -> dict[str, Any]:
        if self._pack_manager is None:
            raise RuntimeError("pack manager is unavailable")
        state = await self._pack_manager.install(
            source_path,
            allowed_root=self.config.workspace_root,
            enable=enable,
        )
        return state.to_record()

    async def enable_pack(self, pack_id: str) -> dict[str, Any]:
        if self._pack_manager is None:
            raise RuntimeError("pack manager is unavailable")
        return (await self._pack_manager.enable(pack_id)).to_record()

    async def disable_pack(self, pack_id: str) -> dict[str, Any]:
        if self._pack_manager is None:
            raise RuntimeError("pack manager is unavailable")
        return (await self._pack_manager.disable(pack_id)).to_record()

    async def remove_pack(self, pack_id: str) -> bool:
        if self._pack_manager is None:
            return False
        return await self._pack_manager.uninstall(pack_id)

    async def get_result(self, task_id: str):
        return await TaskAPI(self).get_result(task_id)

    async def stream_events(self, task_id: str, after_sequence: int = 0):
        async for ev in TaskAPI(self).stream_events(task_id, after_sequence=after_sequence):
            yield ev

    async def stream_all(self, after_rowid: int = 0, limit: int = 200):
        async for ev in TaskAPI(self).stream_all(after_rowid=after_rowid, limit=limit):
            yield ev

    async def get_task_status(self, task_id: str) -> str | None:
        return await TaskAPI(self).get_task_status(task_id)

    async def cancel(self, task_id: str, reason: str = "cancelled by user") -> TaskStatus:
        return await OperatorInteractionService(self).cancel(task_id, reason)

    async def interrupt(self, task_id: str, reason: str = "externally interrupted") -> TaskStatus:
        return await OperatorInteractionService(self).interrupt(task_id, reason)

    async def pending_input(self, task_id: str) -> dict | None:
        return await OperatorInteractionService(self).pending_input(task_id)

    async def provide_input(self, task_id: str, answer: str) -> None:
        return await OperatorInteractionService(self).provide_input(task_id, answer)

    async def approve(self, approval_id: str, *, granted: bool, scope: str | None = None) -> None:
        return await OperatorInteractionService(self).approve(
            approval_id, granted=granted, scope=scope
        )

    async def _mark_approval_recovery(
        self, task_id: str | None, approval_id: str, error: BaseException
    ) -> None:
        return await OperatorInteractionService(self)._mark_approval_recovery(
            task_id, approval_id, error
        )

    def _install_grant(
        self,
        approval_id: str,
        task_id: str | None,
        metadata: dict,
        scope: str | None = None,
        first: ApprovalScope | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        return OperatorInteractionService(self)._install_grant(
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
        return await OperatorInteractionService(self)._rehydrate_approval_grants(
            approvals, continuations
        )

    def _clamp_approval_scope(self, choice: str | None, metadata: dict) -> ApprovalScope | None:
        return OperatorInteractionService(self)._clamp_approval_scope(choice, metadata)

    async def pending_approval_id(self, task_id: str) -> str | None:
        return await OperatorInteractionService(self).pending_approval_id(task_id)

    async def list_sessions(self) -> list[dict]:
        return await OperatorInteractionService(self).list_sessions()

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
        return await OperatorInteractionService(self).resume(session_id, prompt=prompt)

    async def list_interrupted(self) -> list[dict]:
        return await OperatorInteractionService(self).list_interrupted()

    async def resume_task(self, task_id: str) -> TaskSpec:
        return await OperatorInteractionService(self).resume_task(task_id)

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
    # Candidate lifecycle — mechanism lives in
    # :mod:`athena.service.candidates` (P1-10). The facade keeps the public
    # entrypoints; durable state, shadow-engine truth, kernel review, and
    # durable events still resolve through this service instance.
    # ------------------------------------------------------------------ #

    def _candidate_branch(self, task_id: str):
        return self._candidates._candidate_branch(task_id)

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
        return await self._candidates._run_hermes_candidate_referee(
            mission, task_row, candidate, review
        )

    async def _run_self_host_reviewer(
        self,
        task_row: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._candidates._run_self_host_reviewer(task_row, candidate)

    async def _candidate_diff_text(self, task_id: str) -> str:
        return await self._candidates._candidate_diff_text(task_id)

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
        return await self._candidates._candidate_apply_approval_matches(approval_id, branch)

    async def apply_candidate(self, task_id: str, approval_id: str | None = None) -> dict:
        return await self._candidates.apply_candidate(task_id, approval_id)

    async def discard_candidate(self, task_id: str) -> dict:
        return await self._candidates.discard_candidate(task_id)

    async def _candidate_review_event(
        self, event_key: str, task_id: str, review: Mapping[str, Any], outcome: Mapping[str, Any]
    ) -> None:
        return await self._candidates._candidate_review_event(event_key, task_id, review, outcome)

    # ------------------------------------------------------------------ #
    # Operator projections (stable views over canonical durable state)
    # ------------------------------------------------------------------ #
    # Each method projects ONE slice of the same canonical stores the kernel
    # reads.  They never mutate state and never become a second execution
    # path; the CLI renders them verbatim.

    async def operator_permissions(self) -> dict:
        return await OperatorQueryService(self).operator_permissions()

    async def operator_diff(self, *, limit: int = 25) -> list[dict]:
        return await OperatorQueryService(self).operator_diff(limit=limit)

    async def undo_mutation(self, mutation_id: str) -> dict:
        return await OperatorQueryService(self).undo_mutation(mutation_id)

    async def operator_context_summary(self, session_id: str | None = None) -> dict:
        return await OperatorQueryService(self).operator_context_summary(session_id)

    async def operator_artifacts(self, *, limit: int = 50) -> list[dict]:
        return await OperatorQueryService(self).operator_artifacts(limit=limit)

    async def operator_generated_capabilities(self, task_id: str | None = None) -> list[dict]:
        return await OperatorQueryService(self).operator_generated_capabilities(task_id)

    async def operator_memory_candidates(self, *, limit: int = 100) -> list[dict]:
        return await OperatorQueryService(self).operator_memory_candidates(limit=limit)

    async def operator_candidates(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return the shared operator review queue for learned candidates."""
        return await OperatorQueryService(self).operator_candidates(task_id)

    async def operator_candidate_item(
        self, candidate_id: str, task_id: str | None = None
    ) -> dict[str, Any] | None:
        return await OperatorQueryService(self).operator_candidate_item(candidate_id, task_id)

    async def operator_promote_candidate(
        self,
        candidate_id: str,
        *,
        target_scope: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return await OperatorQueryService(self).operator_promote_candidate(
            candidate_id,
            target_scope=target_scope,
            task_id=task_id,
        )

    async def operator_deprecate_candidate(
        self, candidate_id: str, *, task_id: str | None = None
    ) -> dict[str, Any]:
        return await OperatorQueryService(self).operator_deprecate_candidate(
            candidate_id, task_id=task_id
        )

    async def operator_memory_candidate(self, memory_id: str) -> dict | None:
        return await OperatorQueryService(self).operator_memory_candidate(memory_id)

    async def operator_promote_memory_candidate(
        self, memory_id: str, scope: str, scope_id: str | None = None
    ) -> dict:
        return await OperatorQueryService(self).operator_promote_memory_candidate(
            memory_id, scope, scope_id
        )

    async def operator_discard_memory_candidate(self, memory_id: str) -> dict:
        return await OperatorQueryService(self).operator_discard_memory_candidate(memory_id)

    async def operator_generated_capability(
        self, capability_id: str, task_id: str | None = None
    ) -> dict:
        return await OperatorQueryService(self).operator_generated_capability(
            capability_id, task_id
        )

    async def operator_promote_generated_capability(
        self, capability_id: str, scope: str, task_id: str | None = None
    ) -> dict:
        return await OperatorQueryService(self).operator_promote_generated_capability(
            capability_id, scope, task_id
        )

    async def operator_deprecate_generated_capability(
        self, capability_id: str, task_id: str | None = None
    ) -> dict:
        return await OperatorQueryService(self).operator_deprecate_generated_capability(
            capability_id, task_id
        )

    async def _invoke_synthesis(self, arguments: dict, *, task_id: str | None) -> dict:
        return await OperatorQueryService(self)._invoke_synthesis(arguments, task_id=task_id)

    # ------------------------------------------------------------------ #
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
        ws = request.workspace or self._default_workspace
        autonomy = request.autonomy or self.config.autonomy_level
        self._validate_request_metadata(request.metadata)
        # Preserve request metadata (autonomy + any caller-supplied fields)
        meta: dict[str, Any] = {"autonomy": autonomy.value}
        if request.metadata:
            meta.update(request.metadata)
        self_host = trusted_self_host
        if self_host:
            # Self-hosting is a service invariant, not a CLI convention. A
            # caller cannot escape the candidate/review boundary by sending a
            # direct mutation mode or a permissive network workspace.
            autonomy = AutonomyLevel.CODING
            meta["autonomy"] = autonomy.value
            meta["_athena_self_host"] = True
            meta["_athena_review_before_commit"] = True
            if trusted_verification is None:
                raise ValueError("self-host tasks require a trusted verification environment")
            if not trusted_gate_criteria:
                raise ValueError("self-host tasks require service-owned verification gates")
            meta["_athena_verification_environment"] = trusted_verification.to_record()
            meta["_athena_required_gates"] = list(trusted_gate_criteria)
            # The trusted criteria are the task's actual proof contract.  Do
            # not leave them only in private metadata where the verifier's
            # acceptance evaluator cannot enforce them.
            meta["acceptance_criteria"] = list(trusted_gate_criteria)
            if trusted_gate_bundle is not None:
                meta["_athena_gate_bundle"] = dict(trusted_gate_bundle)
            if trusted_mission_plan is not None:
                meta["_athena_mission_plan"] = dict(trusted_mission_plan)
            ws = replace(
                ws,
                network_policy=NetworkPolicy.DENY,
                mutation_mode=MutationMode.SPECULATIVE,
            )
        legacy_mutation_mode = meta.pop("mutation_mode", None)
        typed_mutation_mode = request.mutation_mode
        if typed_mutation_mode is not None and legacy_mutation_mode is not None:
            try:
                legacy_value = MutationMode(str(legacy_mutation_mode))
            except ValueError as exc:
                raise ValueError("legacy mutation_mode conflicts with typed mutation_mode") from exc
            if legacy_value is not typed_mutation_mode:
                raise ValueError("typed mutation_mode conflicts with legacy metadata mutation_mode")
        raw_mutation_mode = (
            typed_mutation_mode if typed_mutation_mode is not None else legacy_mutation_mode
        )
        # OFFLINE autonomy is a hard egress boundary (P0): the task's model
        # routing is narrowed to local-only models, not merely biased toward
        # them. Model calls do not pass through PolicyEngine; ModelRouter's
        # privacy gate is the authority that owns this boundary, and it only
        # enforces what the task's ModelPolicy carries. A network-DENY
        # workspace pins model egress the same way.
        model_policy = request.model_policy or _default_model_policy()
        if autonomy is AutonomyLevel.OFFLINE or (
            ws.network_policy is not None
            and ws.network_policy == NetworkPolicy.DENY
            and model_policy.privacy not in _OFFLINE_MODEL_PRIVACY
        ):
            if model_policy.privacy not in _OFFLINE_MODEL_PRIVACY:
                model_policy = replace(model_policy, privacy="offline")
                meta["_athena_offline_narrowed"] = True
        if self_host:
            # The service-enforced self-host boundary wins over any copied
            # request metadata, including an explicit direct-mode escape.
            ws = replace(ws, mutation_mode=MutationMode.SPECULATIVE)
        elif raw_mutation_mode is not None:
            try:
                mutation_mode = (
                    raw_mutation_mode
                    if isinstance(raw_mutation_mode, MutationMode)
                    else MutationMode(str(raw_mutation_mode))
                )
            except ValueError as exc:
                raise ValueError(
                    "mutation_mode must be one of: "
                    + ", ".join(mode.value for mode in MutationMode)
                ) from exc
            ws = replace(ws, mutation_mode=mutation_mode)
        elif autonomy is AutonomyLevel.CODING:
            # Coding tasks are protected by default.  The escape hatch is
            # explicit metadata (mutation_mode=direct), never a model choice.
            ws = replace(ws, mutation_mode=MutationMode.SPECULATIVE)
        # Typed acceptance criteria are authoritative. The legacy metadata
        # spelling remains a compatibility input, but a request carrying both
        # forms must agree canonically instead of silently choosing one.
        raw_criteria = meta.pop("acceptance_criteria", None)
        typed_criteria = tuple(request.acceptance_criteria or ())
        legacy_criteria = decode_criteria(raw_criteria) if raw_criteria is not None else ()
        if self_host:
            criteria = legacy_criteria
        elif typed_criteria and legacy_criteria:
            if encode_criteria(typed_criteria) != encode_criteria(legacy_criteria):
                raise ValueError(
                    "typed acceptance_criteria conflicts with legacy metadata acceptance_criteria"
                )
            criteria = typed_criteria
        else:
            criteria = typed_criteria or legacy_criteria
        # Normalize requested_capabilities into the task's capability policy
        cap_policy = None
        if request.requested_capabilities:
            cap_policy = CapabilityPolicy(allow=tuple(request.requested_capabilities))
        # Persist attachments as context refs so they survive beyond the request
        context_refs: list[ContextRef] = []
        for att in request.attachments or []:
            if isinstance(att, ContextRef):
                context_refs.append(att)
            elif hasattr(att, "uri") and hasattr(att, "mime_type"):
                # ArtifactRef attachments need to survive the request boundary
                # as canonical context refs, including their media type.
                context_refs.append(
                    ContextRef(
                        kind="artifact",
                        ref=str(att.uri),
                        source_id=getattr(att, "id", None),
                        summary=getattr(att, "producer", None),
                        mime_type=getattr(att, "mime_type", None),
                    )
                )
            elif isinstance(att, dict):
                context_refs.append(
                    ContextRef(
                        kind=att.get("kind", "artifact"),
                        ref=str(att.get("ref", att.get("uri", "")) or ""),
                        source_id=att.get("source_id"),
                        summary=att.get("summary"),
                        mime_type=att.get("mime_type"),
                    )
                )
        spec_kwargs: dict = dict(
            id=request.task_id or new_id("task"),
            objective=request.prompt,
            session_id=session_id,
            workspace=ws,
            model_policy=model_policy,
            resource_budget=ResourceBudget(),
            context_refs=tuple(context_refs),
            metadata=meta,
        )
        if criteria:
            spec_kwargs["acceptance_criteria"] = tuple(criteria)
        if cap_policy is not None:
            spec_kwargs["capability_policy"] = cap_policy
        return TaskSpec(**spec_kwargs)

    @staticmethod
    def _validate_request_metadata(metadata: Mapping[str, Any] | None) -> None:
        for key in metadata or {}:
            name = str(key)
            if name.startswith("_") or name in _RESERVED_REQUEST_METADATA:
                raise ValueError(f"reserved Athena metadata: {name}")

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
        from athena.capabilities.artifacts import ArtifactCapability

        registry.register(FilesystemCapability(workspace))
        from athena.capabilities.git import GitCapability

        registry.register(GitCapability(candidate_resolver=self._candidate_git_view))
        if self._artifacts is not None:
            registry.register(ArtifactCapability(self._artifacts))
        registry.register(
            ExecuteCapability(
                execution,
                workspace,
                artifact_store=self._artifacts,
                failure_memory=self._failure_memory,
            )
        )
        from athena.capabilities.diagnostics import DiagnosticsCapability

        if self._failure_memory is not None:
            registry.register(DiagnosticsCapability(self._failure_memory))
        registry.register(MemoryCapability(memory))
        registry.register(SkillsCapability(skills_store))
        from athena.capabilities.session_search import SessionSearchCapability

        registry.register(SessionSearchCapability(self._store_messages))
        registry.register(DelegateCapability(self._delegation))
        from athena.capabilities.external_delegate import ExternalDelegateCapability
        from athena.delegates.sessions import ExternalDelegateManager

        if self._delegate_session_store is not None:
            self._external_delegate_manager = ExternalDelegateManager(
                self._delegate_registry,
                self._delegate_session_store,
                dispatcher=self._dispatcher,
            )
            registry.register(ExternalDelegateCapability(self._external_delegate_manager))
            self.register_shutdown_hook(
                "external_delegate_sessions",
                self._external_delegate_manager.close_all,
            )
        from athena.capabilities.context_blocks import ContextBlocksCapability

        if self._context_block_store is not None:
            registry.register(ContextBlocksCapability(self._context_block_store))
        from athena.capabilities.packs import PacksCapability

        if self._pack_manager is not None:
            registry.register(PacksCapability(self._pack_manager))
        from athena.capabilities.health import CapabilityHealthCapability

        if self._capability_health is not None:
            registry.register(CapabilityHealthCapability(self._capability_health))
        # Computational-body capabilities (P0 roadmap).
        from athena.capabilities.system import MachineCapability, ProcessCapability
        from athena.capabilities.terminal_session import TerminalSessionCapability
        from athena.capabilities.dependency import DependencyCapability
        from athena.capabilities.reflection import CapabilityReflection
        from athena.capabilities.truth import TruthCapability
        from athena.capabilities.research import HttpDiscoveryProvider, ResearchCapability
        from athena.capabilities.scratch import ScratchCapability
        from athena.capabilities.observer import ObserverCapability
        from athena.capabilities.capsule import ProcedureCapsuleCapability
        from athena.capabilities.workflow import WorkflowCapability
        from athena.research.policy import SourcePolicy

        # Scratch and formal synthesis share one engine so a useful one-shot
        # helper can be promoted without losing source/provenance.
        if self._synthesis is None:
            from athena.synthesis.engine import SynthesisEngine

            self._synthesis = SynthesisEngine(
                dispatcher=self._dispatcher,
                research_store=self._research_store,
            )
        elif self._dispatcher is not None:
            self._synthesis.bind_dispatcher(self._dispatcher)
            self._synthesis.bind_research_store(self._research_store)
        fabric = self._fabric
        if fabric is None:
            raise RuntimeError("capability fabric is not constructed")
        self._synthesis.bind_proof_sink(fabric.update_generated_proof)
        registry.register(
            ScratchCapability(
                self._synthesis,
                self._scratch,
                self._fabric,
            )
        )
        registry.register(
            ObserverCapability(
                self._synthesis,
                self._fabric,
                dispatcher=self._dispatcher,
            )
        )
        self.register_shutdown_hook(
            "generated_persistent_sessions",
            self._synthesis.close_persistent_sessions,
        )

        if TerminalSessionCapability.available():
            self._terminals = TerminalSessionCapability(
                event_sink=self._forward_events(self._require_events()),
            )
            registry.register(self._terminals)
            self._optional_capability_health["terminal"] = {
                "installed": True,
                "configured": True,
                "state": "available",
                "reason": None,
            }
        else:
            self._optional_capability_health["terminal"] = {
                "installed": False,
                "configured": True,
                "state": "unavailable",
                "reason": "pexpect and pyte are required",
            }
            _logger.info("terminal_session capability unavailable: install pexpect and pyte")
        self._processes = ProcessCapability(execution)
        registry.register(self._processes)
        registry.register(MachineCapability())
        registry.register(
            CapabilityReflection(
                self._fabric,
                workflow_store=self._workflow_store,
                skills_store=skills_store,
                execution_manager=execution,
                device_provider=self._device_provider,
                policy_engine=self._policy,
                approval_store=self._store_approvals,
                health_provider=self._capability_health,
                runtime_health_provider=self.runtime_health,
                model_provider=lambda: self._model_registry,
                mcp_status_provider=self.mcp_status,
                delegate_provider=lambda: self._delegate_registry,
            )
        )
        registry.register(TruthCapability(self))
        registry.register(DependencyCapability(execution))
        if self._store_input_requests is not None:
            # Descriptor-only registration: the kernel intercepts
            # request_input calls before any dispatcher runs, so the bound
            # executor is a truthful fallback that never runs on a healthy
            # path. The fabric entry makes the affordance discoverable and
            # compilable into the model's tool surface.
            from athena.capabilities.input_request import InputRequestCapability

            registry.register(InputRequestCapability())
        if research_store is not None:
            research_policy = SourcePolicy(
                allowed_domains=tuple(self.config.research_allowed_domains),
                denied_domains=tuple(self.config.research_denied_domains),
                allow_private_network=self.config.research_allow_private_network,
            )
            discovery_endpoints = self.config.research_discovery_endpoints or (
                (self.config.research_discovery_endpoint,)
                if self.config.research_discovery_endpoint
                else ()
            )
            discovery_providers = tuple(
                HttpDiscoveryProvider(
                    endpoint,
                    source_policy=research_policy,
                    timeout=self.config.research_discovery_timeout,
                )
                for endpoint in discovery_endpoints
            )
            registry.register(
                ResearchCapability(
                    research_store,
                    artifact_store=self._artifacts,
                    source_policy=research_policy,
                    discovery_providers=discovery_providers,
                )
            )
        if self._workflow_store is not None and self._fabric is not None:
            workflow_capability = WorkflowCapability(
                self._workflow_store,
                self._dispatcher,
                self._fabric,
                run_store=self._workflow_run_store,
                external_store=self._external_effect_store,
                workflow_observer=self._knowledge.observe_workflow_execution,
            )
            registry.register(workflow_capability)
            registry.register(
                ProcedureCapsuleCapability(
                    self._workflow_store,
                    self._fabric,
                    self._synthesis,
                    workflow_capability,
                    research_store=self._research_store,
                    dispatcher=self._dispatcher,
                )
            )
        try:
            from athena.capabilities.debugger import DebuggerCapability

            if DebuggerCapability.available():
                self._debugger = DebuggerCapability(
                    execution_manager=self._execution,
                    event_sink=self._forward_events(self._require_events()),
                )
                registry.register(self._debugger)
                self._optional_capability_health["debugger"] = {
                    "installed": True,
                    "configured": True,
                    "state": "available",
                    "reason": None,
                }
            else:
                self._optional_capability_health["debugger"] = {
                    "installed": False,
                    "configured": True,
                    "state": "unavailable",
                    "reason": "debugpy is not installed",
                }
                _logger.info("debugger capability unavailable: debugpy is not installed")
        except Exception as exc:  # debugpy optional
            self._optional_capability_health["debugger"] = {
                "installed": False,
                "configured": True,
                "state": "degraded",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            _logger.info("debugger capability unavailable: %s", exc)

        # Computer + browser interaction (P1-20/P1-28, SPEC 36/65): optional
        # packs behind the standard governance surface. Both register only
        # when their driver is importable; operators wire a driver factory
        # for structured browser control, and computer control stays on the
        # ask-by-default COMPUTER_INPUT path.
        try:
            from athena.capabilities.browser import BrowserCapability
            from athena.capabilities.computer import ComputerCapability

            self._computer = None
            self._browser = None
            if ComputerCapability.available():
                computer = ComputerCapability(artifact_store=self._artifacts)
                self._computer_health = await computer.probe_health()
                self._optional_capability_health["computer"] = {
                    "installed": True,
                    "configured": True,
                    "state": self._computer_health.get("state", "unknown"),
                    "reason": self._computer_health.get("reason"),
                }
                if self._computer_health.get("state") == "ready":
                    self._computer = computer
                    registry.register(self._computer)
                else:
                    _logger.info(
                        "computer capability unavailable at runtime: %s",
                        self._computer_health.get("reason", "display probe failed"),
                    )
            else:
                self._computer_health = {
                    "state": "unavailable",
                    "backend": "none",
                    "reason": "pyautogui is not importable",
                }
                self._optional_capability_health["computer"] = {
                    "installed": False,
                    "configured": True,
                    "state": "unavailable",
                    "reason": self._computer_health["reason"],
                }
                _logger.info(
                    "computer capability unavailable: install the 'computer' extra (pyautogui)"
                )
            browser_factory = self.config.browser_driver_factory
            if browser_factory is None and self.config.browser_enabled:
                if BrowserCapability.available():
                    from athena.capabilities.browser import PlaywrightBrowserDriver

                    preflight = await PlaywrightBrowserDriver.preflight(
                        browser_name=self.config.browser_engine,
                        executable_path=self.config.browser_executable_path,
                        channel=self.config.browser_channel,
                        cdp_endpoint=self.config.browser_cdp_endpoint,
                    )
                    if preflight.get("state") in {"available", "configured"}:

                        async def browser_factory():
                            return await PlaywrightBrowserDriver.launch(
                                browser_name=self.config.browser_engine,
                                headless=self.config.browser_headless,
                                launch_args=self.config.browser_launch_args,
                                executable_path=self.config.browser_executable_path,
                                channel=self.config.browser_channel,
                                cdp_endpoint=self.config.browser_cdp_endpoint,
                                timeout_ms=self.config.browser_timeout_ms,
                                viewport=self.config.browser_viewport,
                            )
                    else:
                        self._browser_health = {
                            "state": "unavailable",
                            "configured": True,
                            "active_sessions": 0,
                            "reason": preflight.get("reason") or "browser preflight failed",
                        }
                        self._optional_capability_health["browser"] = {
                            "installed": True,
                            "configured": True,
                            "state": "unavailable",
                            "reason": self._browser_health["reason"],
                        }

                else:
                    self._browser_health = {
                        "state": "unavailable",
                        "configured": True,
                        "active_sessions": 0,
                        "reason": "browser_enabled but Playwright is not installed",
                    }
                    self._optional_capability_health["browser"] = {
                        "installed": False,
                        "configured": True,
                        "state": "unavailable",
                        "reason": self._browser_health["reason"],
                    }
            if browser_factory is not None:
                self._browser = BrowserCapability(
                    driver_factory=browser_factory,
                    session_scope=self.config.browser_session_scope,
                )
                self._browser_health = self._browser.health()
                self._optional_capability_health["browser"] = {
                    "installed": True,
                    "configured": True,
                    "state": self._browser_health.get("state", "unknown"),
                    "reason": self._browser_health.get("reason"),
                }
                registry.register(self._browser)
                self.register_shutdown_hook("browser_sessions", self._browser.close)
            elif not self.config.browser_enabled:
                self._browser_health = {
                    "state": "unavailable",
                    "configured": False,
                    "active_sessions": 0,
                    "reason": "browser_driver_factory is not configured",
                }
                self._optional_capability_health["browser"] = {
                    "installed": BrowserCapability.available(),
                    "configured": False,
                    "state": "unavailable",
                    "reason": self._browser_health["reason"],
                }
                _logger.info(
                    "browser capability not wired: set browser_driver_factory to enable "
                    "structured browser automation"
                )
        except Exception as exc:  # optional interaction packs
            self._computer_health = {
                "state": "degraded",
                "backend": "unknown",
                "reason": f"computer setup failed: {type(exc).__name__}: {exc}",
            }
            self._optional_capability_health["computer"] = {
                "installed": False,
                "configured": True,
                "state": "degraded",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            self._optional_capability_health["browser"] = {
                "installed": False,
                "configured": bool(self.config.browser_enabled),
                "state": "degraded",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            _logger.info("computer/browser capability unavailable: %s", exc)

        # P1/P2 environment families.
        from athena.capabilities.environment import (
            DatabaseCapability,
            NetworkCapability,
            ServiceCapability,
            WorkspaceCapability,
        )
        from athena.capabilities.watch import WatchCapability, WatchRegistry

        registry.register(
            ServiceCapability(
                external_store=self._external_effect_store,
            )
        )
        registry.register(
            NetworkCapability(
                external_store=self._external_effect_store,
            )
        )
        self._database = DatabaseCapability(
            mutation_store=self._store_mutations,
            artifact_store=self._artifacts,
            external_store=self._external_effect_store,
        )
        registry.register(self._database)
        if getattr(self, "_watch_registry", None) is None:
            self._watch_registry = WatchRegistry(
                observer_runner=self._run_watch_observer,
            )
        self._watches = WatchCapability(
            registry=self._watch_registry,
            execution_manager=self._execution,
        )
        registry.register(self._watches)
        if self._task_manager is not None:
            self._task_manager.add_finalize_observer(self._cleanup_task_watches)
            self._task_manager.add_finalize_observer(self._cleanup_task_affordances)
        from athena.causal.checkpoint import CheckpointManager

        # Checkpoints are part of Athena's durable recovery state.  They must
        # survive a service restart and cannot live in the process-global
        # temporary directory, which may be cleaned independently of the
        # transaction ledger.
        checkpoint_root = (
            os.path.join(
                self._runtime_state_root,
                "checkpoints",
            )
            if self._runtime_state_root
            else None
        )
        self._checkpoints = CheckpointManager(root=checkpoint_root or "/tmp/athena-checkpoints")
        self._reality_gate.bind_checkpoint_manager(self._checkpoints)
        registry.register(
            WorkspaceCapability(
                checkpoint_manager=self._checkpoints,
                mutation_store=self._store_mutations,
                mutation_observer=self._on_mutation_completed,
                project_index_store=self._project_index_store,
                project_index_coordinator=self._project_index_coordinator,
            )
        )
        from athena.capabilities.fusion import FusionCapability

        registry.register(FusionCapability(self))

        # Capability-owned resource teardown (P1-32).
        if self._terminals is not None:
            self.register_shutdown_hook("terminal_sessions", self._terminals.close_all)
        if self._debugger is not None:
            self.register_shutdown_hook("debugger_sessions", self._debugger.close_all)
        if hasattr(self, "_watch_registry"):
            self.register_shutdown_hook("watch_registry", self._watch_registry.close)

    async def _run_watch_observer(
        self,
        task_id: str | None,
        observer_id: str,
        input_value: Mapping[str, Any],
        workspace,
        *,
        profile=None,
        task_policy=None,
        task_budget=None,
    ) -> dict[str, Any]:
        """Run a generated sensor through the canonical dispatcher.

        Watches are event producers, not a second execution path.  The
        observer call therefore gets the same capability lookup, policy,
        budgets, sandbox, and proof handling as a model-requested call.
        """
        from athena.protocol.capabilities import (
            CapabilityRequest,
            CapabilityRequestOrigin,
            CapabilityResult,
            CapabilityResultStatus,
        )

        if self._dispatcher is None:
            return {"status": "failed", "error": "dispatcher unavailable"}
        result = await self._dispatcher.dispatch(
            CapabilityRequest(
                capability_id=observer_id,
                arguments={"input": dict(input_value)},
                task_id=task_id,
                call_id=new_id("watch-observer"),
                origin=CapabilityRequestOrigin.GENERATED,
            ),
            workspace=workspace,
            profile=profile,
            task_policy=task_policy,
            task_budget=task_budget,
        )
        if not isinstance(result, CapabilityResult):
            return {"status": "failed", "error": "observer call suspended"}
        status = result.status
        if status is not CapabilityResultStatus.OK:
            return {
                "status": getattr(status, "value", "failed"),
                "error": getattr(result, "error", None) or "observer failed",
            }
        try:
            value = json.loads(result.output or "null")
        except (TypeError, ValueError):
            value = result.output
        return {
            "status": "ok",
            "observer_id": observer_id,
            "value": value,
            "proof": dict(getattr(result, "metadata", {}) or {}),
        }

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
    async def _poll_watches(self) -> None:
        """Background poll of registered watches -> WatchObserved events."""
        events = None
        while True:
            await asyncio.sleep(2.0)
            registry = getattr(self, "_watch_registry", None)
            if registry is None or not (registry.file_watches or registry.process_watches):
                continue
            if events is None:
                try:
                    events = self._require_events()
                except Exception as exc:
                    registry.record_poll_error(exc)
                    continue

            async def sink(type_, payload, task_id=None):
                if type_ == "WatchObserved":
                    # External reality changes are proof invalidators too.
                    # Apply this before EventStore subscribers wake the
                    # maintenance task, so its first verification sees stale
                    # claims rather than a transiently trusted snapshot.
                    await self._invalidate_watch_claims(payload)
                await events.append_event(type_, payload, task_id=task_id)

            try:
                await registry.poll_all(sink)
            except Exception as exc:
                registry.record_poll_error(exc)
                _logger.warning("watch poll error: %s", exc)
            else:
                if not registry.poll_had_error:
                    registry.record_poll_success()

    async def _invalidate_watch_claims(self, payload: Mapping[str, Any]) -> None:
        """Invalidate claims affected by an observed external change."""
        raw_changes = payload.get("changes")
        if isinstance(raw_changes, list):
            changes = [str(item).removesuffix(" (removed)") for item in raw_changes if str(item)]
        else:
            changes = []
        if payload.get("kind") == "process":
            # A process exit has no file path boundary; claims without an
            # explicit scope are conservatively invalidated.
            changes = ["*"]
        if not changes:
            return
        root = os.path.realpath(str(payload.get("root") or ""))
        workspace_root = os.path.realpath(str(getattr(self._default_workspace, "root", "")))
        if root and workspace_root and root != workspace_root:
            if root.startswith(workspace_root + os.sep):
                prefix = os.path.relpath(root, workspace_root)
                changes = [os.path.join(prefix, path) for path in changes]
            else:
                # A watcher should already be workspace-scoped. Fail closed
                # if stale metadata says otherwise instead of invalidating
                # unrelated claim paths.
                return
        cache = getattr(self, "_world_states", {})
        for world_state in list(cache.values()):
            world_state.claims.invalidate_for_paths(changes)
        index_coordinator = getattr(self, "_project_index_coordinator", None)
        if index_coordinator is not None:
            absolute_changes = [
                os.path.join(root or workspace_root, path)
                if path != "*"
                else (root or workspace_root)
                for path in changes
            ]
            index_coordinator.mark_stale_for_paths(absolute_changes)
        store = getattr(self, "_world_state_store", None)
        if store is not None:
            try:
                await store.invalidate_for_paths(None, changes)
            except Exception as exc:
                _logger.warning("durable watch claim invalidation failed: %s", exc)

    async def _cleanup_task_watches(self, task, result) -> None:
        registry = getattr(self, "_watch_registry", None)
        if registry is not None:
            registry.remove_task(getattr(task, "id", None))

    async def _cleanup_task_affordances(self, task, result) -> None:
        task_id = getattr(task, "id", None)
        if task_id and self._external_delegate_manager is not None:
            try:
                await self._external_delegate_manager.close_task(task_id)
            except Exception as exc:
                _logger.warning(
                    "task external delegate cleanup failed for %s: %s",
                    task_id,
                    exc,
                )
        if task_id and self._synthesis is not None:
            try:
                await self._synthesis.close_persistent_sessions_for_task(task_id)
            except Exception as exc:
                _logger.warning(
                    "task generated runtime cleanup failed for %s: %s",
                    task_id,
                    exc,
                )
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
        if self._shadow._dispatcher is None and self._dispatcher is not None:
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
        """Return the service-owned, single-agent fusion orchestrator."""
        from athena.fusion.orchestrator import FusionOrchestrator

        if getattr(self, "_fusion", None) is None:
            self._fusion = FusionOrchestrator(self)
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
        pcs = tuple(self.config.providers)
        if not pcs:
            return
        provider: Any = None
        for pc in pcs:
            if pc.kind == "fake":
                provider = FakeModelProvider(
                    tool_calling=True,
                    model=pc.model,
                    provider=pc.name,
                    scripts=list(pc.extra.get("scripts") or []),
                    vision=bool(pc.extra.get("vision", False)),
                    cost=pc.extra.get("cost"),
                    latency_class=pc.latency_class or pc.extra.get("latency_class"),
                )
                registry.register(pc.name, provider)
                registry.set_profile(pc.name, resolve_profile("fake", model_id=pc.model))
                registry.set_model_profile(
                    pc.name,
                    pc.model,
                    _model_profile_from_config(pc.model, pc.extra.get("model_profile")),
                )
                continue
            profile = resolve_profile(
                pc.kind,
                base_url=pc.base_url,
                model_id=pc.model,
                cache_mode=pc.cache_mode,
            )
            if profile.protocol in {"openai", "openai-compat"}:
                if not profile.base_url:
                    raise ValueError(f"provider {pc.name!r} needs an explicit base_url")
                provider = OpenAICompatProvider(
                    base_url=profile.base_url,
                    api_key=self._resolve_api_key(pc),
                    model=profile.model_id or pc.model,
                    provider=pc.name,
                    headers=pc.extra.get("headers"),
                    timeout=float(pc.extra.get("timeout", 60.0)),
                    http2=bool(pc.extra.get("http2", False)),
                    authentication=pc.authentication,
                    cost=pc.extra.get("cost"),
                    latency_class=pc.latency_class or pc.extra.get("latency_class"),
                    vision=bool(pc.extra.get("vision", False)),
                )
            elif profile.protocol == "anthropic":
                provider = AnthropicProvider(
                    api_key=self._resolve_api_key(pc) or None,
                    base_url=profile.base_url,
                    model=profile.model_id or pc.model,
                    provider=pc.name,
                    headers=pc.extra.get("headers"),
                    timeout=float(pc.extra.get("timeout", 60.0)),
                    use_sdk=bool(pc.extra.get("use_sdk", True)),
                    cost=pc.extra.get("cost"),
                    latency_class=pc.latency_class or pc.extra.get("latency_class"),
                )
            else:
                raise ValueError(f"unsupported provider protocol: {profile.protocol!r}")
            registry.register(pc.name, provider)
            registry.set_profile(pc.name, profile)
            registry.set_model_profile(
                pc.name,
                profile.model_id or pc.model,
                _model_profile_from_config(
                    profile.model_id or pc.model,
                    pc.extra.get("model_profile"),
                ),
            )

    async def _connect_mcp(self) -> None:
        for server in self.config.mcp_servers:
            await self._connect_mcp_server(server)
            status = self._mcp_connection_status.get(server.name, {})
            if server.required and status.get("state") != "connected":
                raise RuntimeError(
                    f"required MCP server {server.name!r} is not ready: "
                    f"{status.get('last_error') or status.get('state') or 'unknown'}"
                )

    async def _connect_mcp_server(self, server: MCPConfig) -> dict[str, Any]:
        transport = "http" if server.url else "stdio"
        self._mcp_connection_status[server.name] = {
            "id": server.name,
            "configured": True,
            "required": bool(server.required),
            "state": "connecting",
            "transport": transport,
            "tool_count": 0,
            "last_successful_connection": None,
            "last_error": None,
        }
        client: MCPClient | None = None
        try:
            env = dict(server.env)
            if server.secret_env and self._secrets is not None:
                for env_name, credential_id in server.secret_env.items():
                    env[env_name] = self._secrets.resolve(credential_id)
            client = MCPClient(
                server.name,
                command=server.command,
                args=list(server.args),
                url=server.url,
                env=env,
                connect_timeout=server.connect_timeout,
            )
            await client.connect()
            self._mcp_clients.append(client)
            if self._mcp is not None:
                descriptors = await self._mcp.collect_and_register(client, server_alias=server.name)
            else:
                descriptors = []
            self._mcp_connection_status[server.name] = {
                **client.health(),
                "state": "connected",
                "tool_count": len(descriptors),
            }
            self._mcp_reconnect_failures[server.name] = 0
        except Exception as exc:
            _logger.warning("MCP server %s failed to connect: %s", server.name, exc)
            if client is not None:
                if client in self._mcp_clients:
                    self._mcp_clients.remove(client)
                if self._mcp is not None:
                    self._mcp.unregister_connection(server.name)
                try:
                    await client.close()
                except Exception as close_exc:  # noqa: BLE001 - preserve original failure
                    _logger.info("MCP failed-connection cleanup failed: %s", close_exc)
            failures = self._mcp_reconnect_failures.get(server.name, 0) + 1
            self._mcp_reconnect_failures[server.name] = failures
            self._mcp_connection_status[server.name] = {
                **self._mcp_connection_status[server.name],
                "state": "circuit_open" if failures >= 3 else "failed",
                "consecutive_failures": failures,
                "circuit_open": failures >= 3,
                "last_error": f"{type(exc).__name__}: {exc}",
            }
        return dict(self._mcp_connection_status[server.name])

    async def mcp_reconnect(self, name: str) -> dict[str, Any]:
        """Reconnect one configured MCP server and refresh its tool inventory."""
        server = next((item for item in self.config.mcp_servers if item.name == name), None)
        if server is None:
            return {"id": name, "state": "failed", "last_error": "server is not configured"}
        for client in list(self._mcp_clients):
            if client.connection_id != name:
                continue
            if self._mcp is not None:
                self._mcp.unregister_connection(name)
            await client.close()
            self._mcp_clients.remove(client)
        return await self._connect_mcp_server(server)

    def _resolve_api_key(self, pc: ProviderConfig) -> str:
        """Return the key for a provider, preferring a leased credential.

        A ``credential_id`` is treated as the NAME of a secret and resolved
        through the SecretManager at the authorized boundary; the raw
        ``api_key`` field remains a backward-compatible fallback.
        """
        if pc.credential_id and self._secrets is not None:
            try:
                return self._secrets.resolve(pc.credential_id)
            except SecretError as exc:
                # Provider registration is diagnostic and must not turn a
                # missing credential into a half-started service. The adapter
                # remains registered and reports ``auth_missing`` readiness;
                # admission then fails closed through require_agent_ready().
                _logger.warning("provider credential %s unavailable: %s", pc.credential_id, exc)
                return ""
        if pc.api_key is not None:
            return pc.api_key
        return ""

    async def _preflight_hermes_referee(self) -> None:
        """Run the optional safety probe without blocking normal startup."""
        settings = self.config.hermes_referee
        if not settings.transport_enabled:
            return
        evaluator: HermesAgentEvaluator | HermesReferee | None = self._hermes_adapter
        if evaluator is None:
            evaluator = self._hermes_referee
        preflight = getattr(evaluator, "preflight", None)
        if not callable(preflight):
            reason = "Hermes referee does not expose a safety preflight"
            self._hermes_status_error = reason
            self._startup_health["checks"]["hermes_referee"] = {
                "status": "degraded",
                "blocking": False,
                "state": "injected",
                "profile": settings.profile,
                "endpoint": settings.endpoint,
                "error": reason,
            }
            return
        try:
            result = preflight()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            from athena.hermes.agent_adapter import HermesRefereeSafetyError

            reason = str(exc)
            self._hermes_status_error = reason
            self._startup_health["checks"]["hermes_referee"] = {
                "status": "degraded",
                "blocking": False,
                "state": (
                    "unsafe"
                    if isinstance(exc, HermesRefereeSafetyError)
                    else ("disconnected" if isinstance(exc, httpx.HTTPError) else "error")
                ),
                "profile": settings.profile,
                "endpoint": settings.endpoint,
                "error": reason,
            }
            return
        record = result.to_record() if hasattr(result, "to_record") else {}
        self._startup_health["checks"]["hermes_referee"] = {
            "status": "ok",
            "blocking": False,
            "state": "safety_verified",
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "runtime": record.get("runtime", {}),
            "referee": record.get("referee", {}),
            "build": record.get("build", {}),
        }
        self._hermes_status_error = None

    async def _require_verified_hermes_referee(self) -> None:
        """Refuse self-host work only under required Hermes supervision."""
        settings = self.config.hermes_referee
        if self._hermes_supervision_mode is not HermesSupervisionMode.REQUIRED:
            return
        if self._hermes_adapter is None:
            reason = self._hermes_status_error or "Hermes referee is not configured"
            raise RuntimeError(
                "Self-hosting refused: Hermes referee is configured as required "
                f"but has not proven its read-only runtime contract ({reason}). "
                "Run `athena referee repair`."
            )
        try:
            preflight = await self._hermes_adapter.preflight()
        except Exception as exc:
            self._hermes_status_error = str(exc)
            raise RuntimeError(
                "Self-hosting refused: Hermes referee safety verification failed "
                f"({exc}). Run `athena referee repair`."
            ) from exc
        self._startup_health["checks"]["hermes_referee"] = {
            "status": "ok",
            "blocking": False,
            "state": "safety_verified",
            "profile": settings.profile,
            "endpoint": settings.endpoint,
            "runtime": dict(preflight.capabilities.get("runtime") or {}),
            "referee": dict(preflight.capabilities.get("referee") or {}),
            "build": dict(preflight.capabilities.get("build") or {}),
        }

    def _configure_hermes_referee(self) -> None:
        """Build the optional transport after the secret boundary exists."""
        settings = self.config.hermes_referee
        if self._hermes_referee is not None and not self._hermes_referee_owned:
            self._startup_health["checks"]["hermes_referee"] = {
                "status": "ok",
                "blocking": False,
                "state": "configured_unverified",
            }
            return
        if not settings.transport_enabled:
            self._startup_health["checks"]["hermes_referee"] = {
                "status": "ok",
                "blocking": False,
                "state": "disabled",
            }
            return
        api_key = ""
        self._hermes_status_error = None
        if settings.credential_id:
            try:
                if self._secrets is None:
                    raise RuntimeError("SecretManager is not ready")
                api_key = self._secrets.resolve(settings.credential_id)
            except Exception as exc:
                reason = f"Hermes credential unavailable: {exc}"
                self._hermes_status_error = reason

                async def unavailable(_packet: ReviewPacket) -> Mapping[str, Any]:
                    raise RuntimeError(reason)

                self._hermes_referee = HermesReferee(unavailable)
                self._hermes_referee_owned = True
                self._startup_health["checks"]["hermes_referee"] = {
                    "status": "degraded",
                    "blocking": False,
                    "state": "error",
                    "error": reason,
                }
                return
        try:
            adapter = HermesAgentEvaluator(
                endpoint=settings.endpoint,
                profile=settings.profile,
                timeout_seconds=settings.timeout_seconds,
                api_key=api_key,
                allow_remote=settings.allow_remote,
                allow_insecure_remote=settings.allow_insecure_remote,
            )
        except (TypeError, ValueError) as exc:
            reason = f"Hermes configuration invalid: {exc}"
            self._hermes_status_error = reason

            async def unavailable(_packet: ReviewPacket) -> Mapping[str, Any]:
                raise RuntimeError(reason)

            self._hermes_referee = HermesReferee(unavailable)
            self._hermes_referee_owned = True
            self._startup_health["checks"]["hermes_referee"] = {
                "status": "degraded",
                "blocking": False,
                "state": "error",
                "error": reason,
            }
            return
        self._hermes_adapter = adapter
        self._hermes_referee = HermesReferee(adapter)
        self._hermes_referee_owned = True
        self._startup_health["checks"]["hermes_referee"] = {
            "status": "ok",
            "blocking": False,
            "state": "configured",
            "profile": settings.profile,
            "endpoint": settings.endpoint,
        }

    # ------------------------------------------------------------------ #
    # Internal accessors
    # ------------------------------------------------------------------ #
    async def require_agent_ready(self, request: AgentRequest | None = None) -> None:
        """Admit model-backed work before any Task or session is created.

        Startup intentionally remains inspectable in a degraded, first-run
        state when no provider is configured. That state is not an admission
        state: every caller must pass through this service-owned predicate.
        """
        await self._require_provider_ready()
        if request is None:
            return
        base_policy = request.model_policy or _default_model_policy()
        criteria = (
            request.metadata.get("acceptance_criteria")
            if isinstance(request.metadata, Mapping)
            else None
        )
        await self._admit_model_roles(
            base_policy,
            self._required_model_roles(base_policy, request.metadata, criteria=criteria),
        )

    async def require_task_ready(self, spec: TaskSpec) -> None:
        """Admit every model-backed Task before it enters durable state."""
        await self._require_provider_ready()
        policy = spec.model_policy or _default_model_policy()
        await self._admit_model_roles(
            policy,
            self._required_model_roles(policy, spec.metadata, criteria=spec.acceptance_criteria),
        )

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


def _default_model_policy():
    from athena.protocol.tasks import ModelPolicy

    return ModelPolicy(require_tools=False)


# Privacy values ModelRouter treats as a hard LOCAL-only gate. OFFLINE
# autonomy and network-DENY workspaces narrow task model policy into this
# set; "local-preferred" (the default) is deliberately NOT in it because the
# router only biases, never hard-gates, under that value.
_OFFLINE_MODEL_PRIVACY = frozenset({"offline", "local"})


def _model_profile_from_config(
    model_id: str,
    raw: Any,
) -> ModelProfile:
    """Build a behavioral model profile from optional provider config.

    Unknown keys are rejected at startup instead of silently changing the
    meaning of a route.  The default profile is still explicit and durable;
    it is not an untracked ``getattr`` fallback in the kernel.
    """
    if raw is None:
        return ModelProfile(model_pattern=model_id)
    if not isinstance(raw, Mapping):
        raise ValueError("provider model_profile must be a mapping")
    allowed = {
        "tools_structured",
        "tools_parallel",
        "tools_textual_fallback",
        "reasoning_native",
        "empty_content_with_tools",
        "requires_tool_result_name",
        "requires_assistant_replay_fields",
        "malformed_json_tendency",
        "context_window",
        "output_limit",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("unknown model_profile fields: " + ", ".join(sorted(unknown)))
    return ModelProfile(model_pattern=model_id, **dict(raw))


def _is_terminal_status(status: str | None) -> bool:
    if not status:
        return False
    return status in {s.value for s in TERMINAL_STATUSES}


def _result_from_row(row: dict):
    """Build a :class:`TaskResult` from a decoded ``TaskStore.get`` row.

    The store decodes JSON columns (including ``usage``) into dicts; this
    reconstructs the typed result without relying on the manager's re-parse.
    """
    status_raw = row.get("result_status") or row.get("status")
    if not status_raw or status_raw not in {s.value for s in TERMINAL_STATUSES}:
        return None

    usage = row.get("usage") or {}
    from decimal import Decimal

    cost = usage.get("cost_usd")
    from athena.protocol.tasks import UsageSummary, TaskResult

    return TaskResult(
        task_id=row["id"],
        status=TaskStatus(status_raw),
        summary=row.get("summary") or "",
        evidence=_decode_map_rows(row.get("evidence"), "ContextRef"),
        artifacts=_decode_map_rows(row.get("artifacts"), "ArtifactRef"),
        mutations=_decode_map_rows(row.get("mutations"), "MutationRef"),
        unresolved=tuple(row.get("unresolved") or []),
        usage=UsageSummary(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model_calls=int(usage.get("model_calls") or 0),
            cost_usd=Decimal(str(cost)) if cost is not None else Decimal(0),
            cost_known=bool(usage.get("cost_known", cost is not None)),
            duration_ms=int(usage.get("duration_ms") or 0),
            executions=int(usage.get("executions") or 0),
            mutations=int(usage.get("mutations") or 0),
        ),
    )


def _decode_map_rows(raw, kind: str):
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else []
    from athena.protocol.artifacts import ArtifactRef
    from athena.protocol.tasks import ContextRef, MutationRef

    if kind == "ContextRef":
        return tuple(
            ContextRef(
                kind=i.get("kind", "session"),
                ref=i.get("ref", ""),
                source_id=i.get("source_id"),
                summary=i.get("summary"),
                mime_type=i.get("mime_type"),
            )
            for i in items
            if isinstance(i, dict)
        )
    if kind == "ArtifactRef":
        return tuple(
            ArtifactRef(
                id=i.get("id", ""),
                uri=i.get("uri", ""),
                hash=i.get("hash"),
                mime_type=i.get("mime_type"),
                size=i.get("size"),
                producer=i.get("producer"),
                task_id=i.get("task_id"),
                metadata=i.get("metadata") or {},
            )
            for i in items
            if isinstance(i, dict)
        )
    if kind == "MutationRef":
        return tuple(
            MutationRef(
                id=i.get("id", ""),
                resource=i.get("resource", ""),
                operation=i.get("operation", ""),
                reversible=bool(i.get("reversible", False)),
            )
            for i in items
            if isinstance(i, dict)
        )
    return ()
