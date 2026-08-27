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
from datetime import datetime
from typing import Any, Mapping

from athena.artifacts.store import ArtifactStore
from athena.affordances import (
    CapabilityFabric, GeneratedCapabilityStore, ScratchManager,
)
from athena.capabilities.delegate import DelegateCapability
from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.execute import ExecuteCapability
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.memory import MemoryCapability
from athena.capabilities.schedule import ScheduleCapability, ScheduleAPI
from athena.capabilities.registry import CapabilityRegistry
from athena.capabilities.skills import SkillsCapability
from athena.context.compiler import ContextCompiler
from athena.execution.manager import ExecutionManager
from athena.knowledge.pipeline import KnowledgePipeline
from athena.models.router import ModelRouter
from athena.interpreter import InterpreterExtension
from athena.models.compat.profiles import ModelProfile, resolve_profile
from athena.execution.runtimes import PythonRuntime, ShellRuntime
from athena.execution.runtimes.powershell import PowerShellRuntime
from athena.execution.runtimes.node import NodeRuntime
from athena.kernel.kernel import AgentKernel
from athena.kernel.dispatch import CapabilityDispatchShim
from athena.kernel.termination import TerminationEvaluator
from athena.mcp.adapter import MCPAdapter
from athena.mcp.client import MCPClient
from athena.memory.store import MemoryStore
from athena.models.providers.anthropic import AnthropicProvider
from athena.models.providers.fake import FakeModelProvider
from athena.models.providers.openai_compat import OpenAICompatProvider
from athena.models.registry import ProviderRegistry
from athena.policy.credentials import EnvSource, SecretManager, FileSource
from athena.policy.engine import PolicyEngine
from athena.scheduler.scheduler import Scheduler
from athena.skills.lifecycle import SkillLifecycle, SkillStore
from athena.skills.loader import SkillLoader
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
from athena.tasks.budgets import BudgetTracker
from athena.tasks.cancellation import CancellationManager
from athena.tasks.delegation import DelegationManager
from athena.tasks.manager import TaskManager
from athena.tasks.worker import TaskWorker, WorkerConfig

from athena.protocol.events import Event, make_event
from athena.protocol.ids import new_id
from athena.protocol.errors import PersistenceError
from athena.protocol.policy import ApprovalScope, Principal
from athena.protocol.tasks import (
    AgentRequest,
    CapabilityPolicy,
    ContextRef,
    ResourceBudget,
    TaskSpec,
    TaskStatus,
    TERMINAL_STATUSES,
    WorkspaceSpec,
)

from athena.service.config import AthenaConfig, DEFAULT_DB_PATH, ProviderConfig

__all__ = ["AthenaService"]

_DEFAULT_ANSWER_SCRIPTS = (
    {"match": {"user_contains": "2+2"}, "respond": {"text": "4", "done": True}},
)

_logger = logging.getLogger("athena.service")


class AthenaService:
    """The application API — composition root above the core services."""

    # ------------------------------------------------------------------ #
    # Construction / factories
    # ------------------------------------------------------------------ #
    def __init__(self, *, config: AthenaConfig | None = None) -> None:
        self.config = config or AthenaConfig()
        self._started = False
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
        self._tool_repair_store: ToolRepairStore | None = None
        self._world_state_store: Any = None
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
        self._compiler: ContextCompiler | None = None
        self._model_registry: ProviderRegistry | None = None
        self._kernel: AgentKernel | None = None
        self._task_manager: TaskManager | None = None
        self._worker: TaskWorker | None = None
        self._worker_task: asyncio.Task | None = None
        self._approval_recovery_tasks: set[asyncio.Task] = set()
        self._watch_poll_task: asyncio.Task | None = None
        self._shutdown_hooks: list[tuple[str, Any]] = []
        self._budgets: BudgetTracker | None = None
        self._cancellations: CancellationManager | None = None
        self._delegation: DelegationManager | None = None
        self._memory: MemoryStore | None = None
        self._skills: SkillStore | None = None
        self._scheduler: Scheduler | None = None
        self._artifacts: ArtifactStore | None = None
        self._mcp: MCPAdapter | None = None
        self._workflow_store: Any = None
        self._synthesis: Any = None
        self._scratch = ScratchManager()

        self._mcp_clients: list[MCPClient] = []

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
        if self._started:
            return

        try:
            await self._start_impl()
        except BaseException:
            # Startup is a transaction over resources acquired in order. The
            # service must not leak a DB, worker, poller, scheduler, client, or
            # runtime when a later stage fails.
            try:
                await asyncio.shield(self.stop())
            except BaseException as cleanup_error:
                _logger.error(
                    "startup unwind failed: %s", cleanup_error, exc_info=True)
            raise

    async def _start_impl(self) -> None:
        """Acquire service resources in dependency order.

        ``start`` owns the unwind boundary; keeping acquisition in this helper
        makes it impossible for a new stage to accidentally bypass cleanup.
        """

        cfg = self.config
        workspace = WorkspaceSpec(
            id="root",
            root=cfg.workspace_root or self._default_workspace.root,
        )
        self._default_workspace = workspace

        # 1. State: DB + stores (migrations apply lazily on first query).
        db = Database(cfg.db_path or DEFAULT_DB_PATH())
        await db._ensure_ready()  # noqa: SLF001 - apply migrations exactly once, deterministically
        self._db = db
        sessions = SessionRepository(db)
        tasks = TaskStore(db)
        events = EventStore(db)
        messages = MessageStore(db)
        approvals = ApprovalStore(db)
        mutations = MutationStore(db)
        schedules = ScheduleStore(db)
        continuations = ContinuationStore(db)
        self._sessions = sessions
        self._store_tasks = tasks
        self._store_events = events
        self._store_messages = messages
        self._store_approvals = approvals
        self._store_mutations = mutations
        self._store_schedules = schedules
        self._store_continuations = continuations
        from athena.worldstate import WorldStateStore
        self._world_state_store = WorldStateStore(db)
        from athena.workflows import WorkflowStore
        self._workflow_store = WorkflowStore(db)
        self._generated_store = GeneratedCapabilityStore(db)
        from athena.research import ResearchStore
        self._research_store = ResearchStore(db)
        from athena.state.provider_usage import ProviderUsageStore
        self._provider_usage = ProviderUsageStore(db)

        # 2. Credentials (SecretManager owns resolution + leases).
        self._secrets = SecretManager(
            sources=[EnvSource(), FileSource("/etc/athena/secrets")],
        )

        # 3. Execution + runtimes.
        runtime_sessions = RuntimeSessionStore(db)
        execution_store = ExecutionStore(db)
        execution = ExecutionManager(
            runtime_session_store=runtime_sessions,
            execution_store=execution_store,
            event_sink=self._forward_events(events),
        )
        execution.register_runtime(PythonRuntime())
        execution.register_runtime(ShellRuntime())
        if PowerShellRuntime.available():
            execution.register_runtime(PowerShellRuntime())
        if NodeRuntime.available():
            execution.register_runtime(NodeRuntime())
        self._execution = execution
        self._store_runtime_sessions = runtime_sessions
        self._store_executions = execution_store
        self._tool_repair_store = ToolRepairStore(db)

        # 4. Memory + skills.
        memory = MemoryStore(db)
        self._memory = memory
        skill_loader = SkillLoader(search_paths=tuple(cfg.skills_paths))
        skill_lifecycle = SkillLifecycle(db, events=events)
        skills_store = SkillStore(loader=skill_loader, lifecycle=skill_lifecycle)
        self._skills = skills_store
        try:
            discovered = await skill_loader.load()
            await self._sync_skills(skill_lifecycle, discovered)
        except Exception as exc:
            _logger.warning("skill discovery failed: %s", exc)
            # Track skill discovery status for visibility
            self._skill_discovery_status = f"failed: {exc}"

        # 4. Policy engine.
        policy = PolicyEngine(profile=cfg.autonomy_level)
        self._policy = policy
        await self._rehydrate_approval_grants(approvals, continuations)

        # 5. Artifacts (construct BEFORE dispatcher so it can be injected).
        self._artifacts = ArtifactStore(root=cfg.artifact_root)

        # 6. Capability registry + dispatcher (single path, INV-004).
        registry = CapabilityRegistry()
        self._registry = registry
        fabric = CapabilityFabric(registry, store=self._generated_store)
        self._fabric = fabric
        dispatcher = CapabilityDispatcher(
            registry,
            policy,
            mutation_store=mutations,
            approval_store=approvals,
            continuation_store=continuations,
            repair_store=self._tool_repair_store,
            event_sink=self._forward_events(events),
            artifact_store=self._artifacts,
            mutation_observer=self._on_mutation_completed,
            fabric=fabric,
        )
        self._dispatcher = dispatcher

        # 7. TaskManager (needs budgets/cancellations, built a bit later).
        budgets = BudgetTracker(task_store=tasks)
        self._budgets = budgets
        task_manager = TaskManager(
            task_store=tasks,
            events=events,
            sessions=sessions,
            budgets=budgets,
        )
        self._task_manager = task_manager

        cancellations = CancellationManager(
            task_manager=task_manager,
            execution_manager=execution,
            task_store=tasks,
        )
        self._cancellations = cancellations
        task_manager._cancellations = cancellations  # noqa: SLF001

        # Post-finalization knowledge pipeline (BUILDSPEC 64/68): every
        # completed/partial task feeds memory + skill candidates. Bound after
        # all stores exist; the observer itself is failure-isolated.
        self._knowledge = KnowledgePipeline(
            messages=messages,
            memory_store=memory,
            skill_lifecycle=skill_lifecycle,
            events=events,
        )
        task_manager.add_finalize_observer(self._knowledge)

        # Watch polling: file/process watchers push WatchObserved events
        # into the durable stream while the service runs (P1 'watch').
        self._watch_poll_task = asyncio.create_task(self._poll_watches())

        # 8. Models + router (with role-divided policies: "summarizer",
        # "judge", etc. can be pinned to specific models in config; roles
        # without an entry fall back to the user's primary/global choice).
        model_registry = ProviderRegistry()
        self._register_providers(model_registry)
        self._model_registry = model_registry
        router = ModelRouter(
            model_registry, role_policies=self._role_policies(cfg.model_roles)
        )
        self._router = router

        # 9. Context compiler (with a model-backed compression summarizer so older
        # transcript is genuinely summarized, not just truncated).
        compiler = ContextCompiler(
            message_store=messages,
            memory_store=memory,
            skill_loader=skills_store,
            capability_registry=fabric,
            artifact_store=self._artifacts,
            research_store=self._research_store,
            summarizer=self._make_model_summarizer(model_registry),
            context_window=cfg.context_window,
            reserve_output=cfg.reserve_output,
            workspace_reader=self._workspace_reader(),
        )
        self._compiler = compiler

        # 10. Kernel.
        kernel = AgentKernel(
            task_store=tasks,
            events=events,
            task_manager=task_manager,
            messages=messages,
            registry=model_registry,
            router=router,
            context_compiler=compiler,
            termination=TerminationEvaluator(
                acceptance_verifier=self._build_verifier(
                    execution=execution,
                    dispatcher=self._dispatcher,
                    artifact_store=self._artifacts,
                    capability_registry=registry,
                    model_registry=router,   # ModelRouter: judge role routing
                    evidence_provider=self._verification_evidence,
                ),
            ),
            dispatch_factory=self._dispatch_factory,
            continuation_store=continuations,
            interpreter=self._make_interpreter(),
        )
        self._kernel = kernel

        # 11. Delegation (needs kernel).
        delegation = DelegationManager(
            task_manager=task_manager, kernel=kernel, budgets=budgets
        )
        self._delegation = delegation

        # 12. Register core capabilities (bind executors to current handles).
        self._register_core_capabilities(
            registry=registry,
            workspace=workspace,
            execution=execution,
            memory=memory,
            skills_store=skills_store,
            research_store=self._research_store,
        )

        # Rehydrate only validated project/user machinery. Task-local
        # capabilities are intentionally recreated by the owning task and
        # never survive terminal cleanup or a process restart.
        from athena.capabilities.synthesis import SynthesisCapability

        registry.register(SynthesisCapability(self._synthesis, fabric))
        await fabric.load_persisted(
            lambda generated: self._synthesis.restore_executor(
                generated, proof_sink=fabric.update_generated_proof,
            ),
            project_id=workspace.id,
            user_id="athena",
        )

        # 12b. Register schedule capability AFTER the scheduler is constructed
        # (P1-25: ScheduleAPI must capture a live scheduler, not None).
        scheduler = Scheduler(
            store=schedules,
            task_manager=task_manager,
            max_concurrent=cfg.scheduler_max_concurrent,
            loop_interval_seconds=cfg.scheduler_interval_seconds,
        )
        self._scheduler = scheduler
        registry.register(ScheduleCapability(ScheduleAPI(scheduler, task_manager)))

        # 12.5 Crash recovery: reconcile orphaned state before claiming new work.
        from athena.recovery.manager import RecoveryManager

        recovery = RecoveryManager(
            task_store=tasks,
            mutation_store=mutations,
            execution_store=execution_store,
            runtime_session_store=runtime_sessions,
        )
        try:
            recovery_summary = await recovery.recover()
            if any(recovery_summary.values()):
                _logger.info("crash recovery reconciled: %s", recovery_summary)
        except Exception as exc:
            _logger.warning("crash recovery failed: %s", exc)

        # 12.75 Durable approval recovery: a resolved continuation is not
        # ordinary queued work. It belongs to a task that was already parked
        # in WAITING_APPROVAL, so the worker would never claim it. Recover the
        # exact task before the worker starts and let the kernel consume the
        # canonical call without asking the model to reproduce it.
        await self._recover_approved_continuations(
            continuations=continuations,
            task_store=tasks,
            task_manager=task_manager,
            kernel=kernel,
        )

        # 13. Worker + scheduler.
        worker = TaskWorker(
            task_manager=task_manager,
            kernel=kernel,
            config=WorkerConfig(max_parallel=cfg.worker_max_parallel),
        )
        self._worker = worker
        self._worker_task = asyncio.create_task(self._worker.run_forever())

        # 14. MCP (best-effort).
        self._mcp = MCPAdapter(registry)
        await self._connect_mcp()

        # 15. Start background scheduler loop.
        await scheduler.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started and self._db is None:
            return

        # 1. Stop accepting/claiming new work first (P0-23).
        if self._worker_task is not None:
            if self._worker is not None:
                try:
                    await self._worker.stop()
                except Exception as exc:
                    _logger.warning("worker stop failed: %s", exc)
            try:
                await self._worker_task
            except Exception as exc:
                _logger.warning("worker task teardown failed: %s", exc)
            self._worker_task = None

        # Approval recovery runs are not owned by TaskWorker, but they still
        # execute through the kernel and must not outlive service shutdown.
        # Cancelling the coroutine leaves the task recoverable; the normal
        # RUNNING -> INTERRUPTED pass below records that boundary.
        recovery_tasks = list(getattr(self, "_approval_recovery_tasks", ()))
        for recovery in recovery_tasks:
            recovery.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        self._approval_recovery_tasks.clear()

        # 2. Stop the scheduler (no new claims).
        if self._scheduler is not None:
            try:
                await self._scheduler.stop()
            except Exception as exc:
                _logger.warning("scheduler stop failed: %s", exc)
            self._scheduler = None

        # 3. INTERRUPT active tasks (recoverable), never CANCEL (P0-23).
        #    Graceful shutdown parks in-flight work as INTERRUPTED so it can be
        #    resumed on next startup; only explicit user cancellation is a
        #    terminal CANCELLED. QUEUED tasks stay QUEUED and run next startup.
        if self._store_tasks is not None and self._task_manager is not None:
            try:
                rows = await self._store_tasks.list_by_status(TaskStatus.RUNNING)
                for row in rows or []:
                    tid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
                    if not tid:
                        continue
                    if self._execution is not None:
                        try:
                            await self._execution.cancel_task(tid)
                        except Exception as exc:
                            _logger.warning("cancel task %s on stop failed: %s", tid, exc)
                    try:
                        await self._task_manager.transition(
                            tid, TaskStatus.INTERRUPTED, reason="service stopping"
                        )
                    except Exception as exc:
                        _logger.warning("interrupt task %s on stop failed: %s", tid, exc)
            except Exception as exc:
                _logger.warning("interrupt-running-tasks on stop failed: %s", exc)

        # Watch poller (P1-31): cancel and await before closing resources.
        poll_task = getattr(self, "_watch_poll_task", None)
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _logger.warning("watch poller teardown failed: %s", exc)
            self._watch_poll_task = None

        # Capability-owned resources via shutdown registry (P1-32).
        await self._run_shutdown_hooks()

        # MCP clients.
        for client in self._mcp_clients:
            try:
                await client.close()
            except Exception as exc:
                _logger.warning("MCP client close failed: %s", exc)
        self._mcp_clients = []

        # Runtimes / execution. Kill every in-flight subprocess tree so a
        # shutdown never leaves an orphan process, including sessions the
        # runtimes adopted that were never surfaced into _task_sessions.
        if self._execution is not None:
            try:
                await self._execution.close_all()
            except Exception as exc:
                _logger.warning("execution close_all failed: %s", exc)
            for rt in set(self._execution._runtimes.values()):
                close_all = getattr(rt, "close_all", None)
                if close_all is None:
                    continue
                try:
                    if asyncio.iscoroutinefunction(close_all):
                        await close_all()
                    else:
                        close_all()
                except Exception as exc:
                    _logger.warning("runtime %s close_all failed: %s", type(rt).__name__, exc)
            self._execution = None

        # DB last.
        if self._db is not None:
            if self._fabric is not None:
                try:
                    await self._fabric.flush()
                except Exception as exc:
                    _logger.warning("generated capability flush failed: %s", exc)
            try:
                await self._db.close()
            except Exception as exc:
                _logger.warning("db close failed: %s", exc)
            self._db = None

        self._cancellations = None
        self._world_state_store = None
        self._generated_store = None
        self._workflow_store = None
        self._research_store = None
        self._synthesis = None
        self._world_states = {}
        self._started = False

    async def _recover_approved_continuations(
        self,
        *,
        continuations: ContinuationStore,
        task_store: TaskStore,
        task_manager: TaskManager,
        kernel: AgentKernel,
    ) -> None:
        """Resume resolved approval calls after a process restart.

        Approval resolution and the canonical call are durable, but the old
        kernel coroutine is not. This method reconstructs only the missing
        continuation boundary: it never re-runs model repair and never creates
        a new task. A resolved call for a terminal task is left untouched for
        forensic recovery rather than being executed against a completed task.
        """
        try:
            await continuations.release_claims_for_restart()
            task_ids = await continuations.recoverable_task_ids()
        except Exception as exc:
            raise PersistenceError(
                f"approval continuation recovery lookup failed: {exc}",
                cause=exc,
            ) from exc

        for task_id in task_ids:
            try:
                row = await task_store.get(task_id)
            except Exception as exc:
                raise PersistenceError(
                    f"approval continuation task lookup failed for {task_id}: {exc}",
                    cause=exc,
                ) from exc
            if row is None:
                _logger.error(
                    "approval continuation %s references missing task", task_id
                )
                continue

            try:
                status = TaskStatus(row["status"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"approval continuation task {task_id} has invalid status",
                    cause=exc,
                ) from exc

            # RecoveryManager converts orphaned RUNNING tasks to INTERRUPTED.
            # WAITING_APPROVAL is the normal hard-crash state. Both are safe to
            # move back to RUNNING for this exact durable continuation.
            if status in (TaskStatus.WAITING_APPROVAL, TaskStatus.INTERRUPTED):
                await task_manager.transition(task_id, TaskStatus.RUNNING,
                                              reason="resume approved continuation")
            elif status is not TaskStatus.RUNNING:
                _logger.error(
                    "not resuming approved continuation for task %s in status %s",
                    task_id, status.value,
                )
                continue

            recovery = asyncio.create_task(kernel.run_task(task_id))
            self._approval_recovery_tasks.add(recovery)
            recovery.add_done_callback(
                self._track_approval_recovery(task_id, recovery)
            )

    def _track_approval_recovery(self, task_id: str, recovery: asyncio.Task):
        def _done(task: asyncio.Task) -> None:
            self._approval_recovery_tasks.discard(task)
            self._log_background_failure(f"approval recovery {task_id}")(task)

        return _done

    # ------------------------------------------------------------------ #
    # Application API
    # ------------------------------------------------------------------ #
    async def submit(self, request: AgentRequest, *, wait: bool = True) -> TaskSpec:
        """Turn an :class:`AgentRequest` into a Task and optionally drive it
        through the worker to completion (BHV-002: all work becomes a Task)."""
        tm = self._require_task_manager()
        session_id = request.session_id or new_id("session")
        spec = self._build_task_spec(request, session_id)
        created = await tm.create(spec)
        await tm.enqueue(created.id)
        if wait:
            await self.wait_for(created.id)
        return created

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
            await self._sessions.create(session_id)
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
        """Drive the NAMED task through the kernel synchronously.

        The named task is acquired by id and run by the kernel, so it does not
        race the background ``run_forever`` worker for the next claimed task.
        """
        worker = self._require_worker()
        await worker.run_task(task_id)
        return await self.get_task(task_id)

    async def wait_for(self, task_id: str, *, timeout: float | None = None) -> TaskSpec:
        """Poll until the task reaches a terminal status (or timeout)."""
        import time

        deadline = time.monotonic() + (timeout or 60.0)
        while True:
            task = await self.get_task(task_id)
            status = (task.metadata or {}).get("status")
            if status in {s.value for s in TERMINAL_STATUSES}:
                return task
            if time.monotonic() >= deadline:
                return task
            await asyncio.sleep(0.02)

    async def get_task(self, task_id: str) -> TaskSpec:
        """Return the persisted :class:`TaskSpec` (status in ``metadata["status"]``)."""
        return await self._require_task_manager().get(task_id)

    async def get_result(self, task_id: str):
        """Return the :class:`TaskResult` (or None if not yet finalised)."""
        mgr = self._require_task_manager()
        return await mgr.get_result(task_id)

    async def stream_events(self, task_id: str, after_sequence: int = 0):
        """Yield a task's events, ordered by sequence (replayable log).

        Polls the event store while the task is still running so the stream is
        live: new events appended after the last yielded sequence are streamed
        out as they arrive. The generator stops once the task reaches a
        terminal status (and flushes any remaining events).
        """
        events = self._require_events()
        cursor = after_sequence
        while True:
            items = await events.list_for_task(task_id, after_sequence=cursor)
            for ev in items or []:
                seq = getattr(ev, "sequence", None)
                if seq is not None:
                    try:
                        cursor = int(seq)
                    except (TypeError, ValueError):
                        pass
                yield ev
            current = await self.get_task_status(task_id)
            if _is_terminal_status(current):
                return
            await asyncio.sleep(0.1)

    async def stream_all(self, after_rowid: int = 0, limit: int = 200):
        """Yield events across ALL tasks in insertion order (live tail).

        Backs the OI stream viewer: a read-only global subscription to the
        canonical event log. Never terminates; the caller cancels it.
        """
        events = self._require_events()
        cursor = after_rowid
        while True:
            items = await events.list_recent(after_rowid=cursor, limit=limit)
            for ev in items:
                rid = getattr(ev, "_rowid", None)
                if isinstance(rid, int) and rid > cursor:
                    cursor = rid
                yield ev
            await asyncio.sleep(0.15)

    async def get_task_status(self, task_id: str) -> str | None:
        """Return the task's status string (from ``metadata["status"]``), or None."""
        try:
            task = await self.get_task(task_id)
        except Exception:
            return None
        return (task.metadata or {}).get("status")

    async def cancel(self, task_id: str, reason: str = "cancelled by user") -> TaskStatus:
        status = await self._require_cancellations().cancel(task_id, reason)
        if self._kernel is not None:
            try:
                self._kernel.cancel_task(task_id)
            except Exception:
                pass
            try:
                await self._kernel.notify_approval_resolved(task_id, "denied")
            except Exception:
                pass
        return status

    async def interrupt(self, task_id: str, reason: str = "externally interrupted") -> TaskStatus:
        return await self._require_cancellations().interrupt(task_id, reason)

    async def approve(
        self, approval_id: str, *, granted: bool, scope: str | None = None
    ) -> None:
        """Resolve a pending approval and wake the parked task, if any.

        The persisted resolution (ApprovalStore) and the runtime grant
        (ApprovalManager) share the same approval_id. A granted call installs an
        exact scoped ApprovalGrant so the SAME capability call (identical
        arguments) passes policy on resume; a denied call records the denial and
        wakes the task with no effect (BHV-043).
        """
        task_id = None
        metadata: dict = {}
        if self._store_approvals is not None:
            try:
                rec = await self._store_approvals.get(approval_id)
                if isinstance(rec, dict):
                    task_id = rec.get("task_id")
                    metadata = rec.get("metadata") or {}
            except Exception:
                task_id = None

        if granted:
            effective_scope = self._clamp_approval_scope(scope, metadata)
            if effective_scope is not None and self._store_approvals is not None:
                try:
                    await self._store_approvals.record_grant(
                        approval_id,
                        resolver="user",
                        scope=effective_scope.value,
                        expires_at=metadata.get("expires_at"),
                        metadata={"resolved_by_service": True},
                    )
                except Exception as exc:
                    _logger.warning("record_grant failed for %s: %s", approval_id, exc)
            if effective_scope is not None:
                self._install_grant(
                    approval_id, task_id, metadata, first=effective_scope
                )
        elif self._store_approvals is not None:
            try:
                await self._store_approvals.record_deny(
                    approval_id, resolver="user", metadata={"resolved_by_service": True}
                )
            except Exception as exc:
                _logger.warning("record_deny failed for %s: %s", approval_id, exc)

        # Durable continuation: retain the canonical call until the kernel
        # consumes it. A live kernel wakes its in-memory wait; after restart,
        # no coroutine exists, so transition the same task back to RUNNING and
        # launch the normal kernel entry point, which claims the stored call.
        store_cont = getattr(self, "_store_continuations", None)
        if store_cont is not None and metadata.get("call_id"):
            try:
                for cont in await store_cont.pending(task_id):
                    if cont.get("call_id") == metadata.get("call_id"):
                        await store_cont.mark_resolved(
                            cont["id"], "granted" if granted else "denied"
                        )
            except Exception as exc:
                _logger.warning("continuation resolve failed for %s: %s", approval_id, exc)

        kernel = self._kernel
        active = bool(
            task_id is not None
            and kernel is not None
            and task_id in getattr(kernel, "_runs", {})
        )
        if active and kernel is not None and task_id is not None:
            await kernel.notify_approval_resolved(
                task_id, "granted" if granted else "denied"
            )
        elif task_id is not None and kernel is not None and self._task_manager is not None:
            try:
                row = await self._store_tasks.get(task_id) if self._store_tasks else None
                if row and row.get("status") == TaskStatus.WAITING_APPROVAL.value:
                    await self._task_manager.transition(task_id, TaskStatus.RUNNING)
                    recovery = asyncio.create_task(kernel.run_task(task_id))
                    recovery.add_done_callback(self._log_background_failure(
                        f"approval recovery {task_id}"))
            except Exception as exc:
                _logger.warning("approval recovery failed for %s: %s", approval_id, exc)

    def _install_grant(
        self,
        approval_id: str,
        task_id: str | None,
        metadata: dict,
        scope: str | None = None,
        first: ApprovalScope | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Install an exact scoped ApprovalGrant so the approved call passes on resume."""
        if self._policy is None or getattr(self._policy, "approvals", None) is None:
            return
        manager = self._policy.approvals
        digest = metadata.get("args_digest")
        scope_choice = first or self._clamp_approval_scope(scope, metadata)
        if scope_choice is None:
            return
        if scope_choice == ApprovalScope.CALL and not digest:
            return
        cap = metadata.get("capability_id")
        call_id = metadata.get("call_id")
        effects = metadata.get("effects") or []
        primary_name = effects[0] if effects and isinstance(effects, list) else None

        # Exact-args pinning is a TOCTOU guard for resuming THE approved call
        # (CALL scope).  TASK/SESSION/PROJECT scopes authorize future calls and
        # must not be pinned to one argument digest, or they never match.
        pinned_digest = digest or None
        pinned_call = call_id
        if scope_choice != ApprovalScope.CALL:
            pinned_digest = None
            pinned_call = None

        try:
            if manager.state(approval_id) is None:
                manager.create_request(
                    Principal("agent", "athena"),
                    scope_choice,
                    capability=cap,
                    effect=str(primary_name) if primary_name else None,
                    task_id=task_id,
                    # SESSION-scoped grants are keyed on session_id in
                    # ApprovalManager._covers_locked; omitting it makes every
                    # session grant unmatchable and forces re-approval.
                    session_id=metadata.get("session_id"),
                    approval_id=approval_id,
                    args_digest=pinned_digest,
                    call_id=pinned_call,
                    expires_at=expires_at,
                )
            manager.grant(approval_id, resolver="user")
        except Exception:
            pass

    async def _rehydrate_approval_grants(
        self, approvals: ApprovalStore, continuations: ContinuationStore,
    ) -> None:
        """Restore only persisted grants that are still safe to use.

        CALL grants are rehydrated only when their exact durable continuation
        is resolved and unconsumed. Without that check, restarting Athena
        would reset the in-memory ``used`` bit and make a one-shot approval
        replayable. Broader scopes are restored from their persisted grant
        rows and retain the original expiry boundary.
        """
        if self._policy is None:
            return
        try:
            records = await approvals.list_granted()
        except Exception as exc:
            _logger.warning("approval grant rehydration failed: %s", exc)
            return
        for record in records:
            metadata = record.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            scope_raw = record.get("grant_scope") or metadata.get("scope")
            try:
                scope = ApprovalScope(scope_raw)
            except (TypeError, ValueError):
                _logger.warning(
                    "skipping granted approval %s with invalid scope %r",
                    record.get("id"), scope_raw,
                )
                continue

            approval_id = str(record.get("id") or "")
            if scope is ApprovalScope.CALL:
                try:
                    pending = await continuations.unconsumed_for_approval(approval_id)
                except Exception as exc:
                    _logger.warning(
                        "cannot check approval continuation %s: %s",
                        approval_id, exc,
                    )
                    continue
                if not pending:
                    continue

            raw_expiry = record.get("grant_expires_at") or metadata.get("expires_at")
            expiry = None
            if raw_expiry:
                try:
                    expiry = datetime.fromisoformat(str(raw_expiry))
                except ValueError:
                    _logger.warning(
                        "ignoring invalid expiry on approval %s", approval_id)
            self._install_grant(
                approval_id,
                record.get("task_id"),
                metadata,
                first=scope,
                expires_at=expiry,
            )

    def _clamp_approval_scope(
        self, choice: str | None, metadata: dict
    ) -> ApprovalScope | None:
        """Resolve the effective approval scope.

        A caller-provided ``choice`` is clamped to the scopes the approval
        store actually requested (``metadata["requested_scope"]``). Defaults to
        the stored ``scope`` (or the single requested scope) when the caller
        offers none; returns None when the caller requests a scope that was not
        offered, so an unsupported/broader grant is never installed.
        """
        requested = metadata.get("requested_scope")
        supported: set[str] = set()
        if isinstance(requested, list):
            supported = {str(s) for s in requested}
        elif isinstance(requested, str):
            supported = {requested}

        default = metadata.get("scope")
        if default is None and len(supported) == 1:
            default = next(iter(supported))

        if choice in (None, ""):
            if default:
                try:
                    return ApprovalScope(default)
                except (ValueError, KeyError):
                    return None
            return None

        if choice not in supported:
            return None
        try:
            return ApprovalScope(choice)
        except (TypeError, ValueError):
            return None

    async def pending_approval_id(self, task_id: str) -> str | None:
        """Return the id of the most recent pending approval for a task, if any."""
        if self._store_approvals is None:
            return None
        try:
            recs = await self._store_approvals.list_for_task(task_id)
        except Exception:
            return None
        for rec in recs or []:
            if isinstance(rec, dict) and rec.get("status") == "PENDING":
                return rec.get("id") or rec.get("approval_id")
        return None

    async def list_sessions(self) -> list[dict]:
        if self._sessions is None:
            return []
        return await self._sessions.list_all()

    async def resume(self, session_id: str, *, prompt: str = "") -> TaskSpec:
        """Create and run a follow-up task in the given session."""
        return await self.submit(
            AgentRequest(prompt=prompt or "continue", session_id=session_id),
            wait=True,
        )

    async def list_interrupted(self) -> list[dict]:
        """Tasks parked by shutdown/crash, still awaiting completion."""
        if self._store_tasks is None:
            return []
        try:
            rows = await self._store_tasks.list_by_status(TaskStatus.INTERRUPTED)
        except Exception as exc:
            _logger.warning("interrupted task listing failed: %s", exc)
            return []
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "id": row.get("id"),
                    "objective": row.get("objective"),
                    "session_id": row.get("session_id"),
                    "created_at": row.get("created_at"),
                }
            )
        return out

    async def resume_task(self, task_id: str) -> TaskSpec:
        """Re-queue an INTERRUPTED task so it runs to completion.

        The task keeps its original objective, acceptance criteria, workspace,
        capability policy, and budget — this is a continuation of the SAME
        durable work, not a new conversation turn.
        """
        if self._store_tasks is None or self._task_manager is None:
            raise RuntimeError("AthenaService not started")
        row = await self._store_tasks.get(task_id)
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        status = (row.get("status") or "").upper()
        if status == "RUNNING":
            # Already claimed by a live worker.
            return await self.get_task(task_id)
        if status in ("COMPLETE", "FAILED", "CANCELLED"):
            raise ValueError(f"task {task_id} is terminal ({status}); cannot resume")
        # INTERRUPTED (and QUEUED re-queue): hand back to the worker pool.
        await self._task_manager.enqueue(task_id)
        return await self.get_task(task_id)

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
            "models": [e for e in details if str(e["type"]).startswith("Model")
                       or str(e["type"]).startswith("Inference")],
            "capabilities": [e for e in details if "Capability" in str(e["type"])
                             or str(e["type"]).startswith("Tool")],
            "policy": [e for e in details if "Policy" in str(e["type"])
                       or "Approval" in str(e["type"])],
            "execution": [e for e in details if "Execution" in str(e["type"])
                          or str(e["type"]).startswith("Std")],
            "mutations": [e for e in details if "Mutation" in str(e["type"])],
            "artifacts": [e for e in details if "Artifact" in str(e["type"])],
            "children": [e for e in details if "Child" in str(e["type"])
                         or "Delegat" in str(e["type"])],
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
    # Operator projections (stable views over canonical durable state)
    # ------------------------------------------------------------------ #
    # Each method projects ONE slice of the same canonical stores the kernel
    # reads.  They never mutate state and never become a second execution
    # path; the CLI renders them verbatim.

    async def operator_permissions(self) -> dict:
        """Active policy grants plus pending approval requests."""
        grants: list[dict] = []
        if self._policy is not None:
            try:
                for g in self._policy.approvals.list_active():
                    grants.append(
                        {
                            "approval_id": g.id,
                            "scope": getattr(g.scope, "value", str(g.scope)),
                            "capability": g.capability,
                            "resource_pattern": g.resource_pattern,
                            "task_id": g.task_id,
                            "session_id": g.session_id,
                            "expires_at": (
                                g.expires_at.isoformat() if g.expires_at else None
                            ),
                        }
                    )
            except Exception as exc:
                _logger.warning("list_active grants failed: %s", exc)
        pending: list[dict] = []
        if self._store_approvals is not None:
            try:
                for rec in await self._store_approvals.list_pending():
                    pending.append(
                        {
                            "approval_id": rec.get("id"),
                            "capability_id": rec.get("capability_id"),
                            "arguments": rec.get("arguments"),
                            "created_at": rec.get("created_at"),
                        }
                    )
            except Exception as exc:
                _logger.warning("list_pending approvals failed: %s", exc)
        return {"active_grants": grants, "pending": pending}

    async def operator_diff(self, *, limit: int = 25) -> list[dict]:
        """Recent file mutations from the write-ahead mutation ledger."""
        if self._store_mutations is None:
            return []
        try:
            rows = await self._store_mutations.list_recent(limit=limit)
        except Exception as exc:
            _logger.warning("mutation listing failed: %s", exc)
            return []
        return [
            {
                "id": r.get("id"),
                "task_id": r.get("task_id"),
                "resource": r.get("resource"),
                "operation": r.get("operation"),
                "status": r.get("status"),
                "reversible": bool(r.get("reversible")),
                "before_ref": r.get("before_ref") or r.get("before_state"),
                "after_state": r.get("after_state"),
                "created_at": r.get("created_at"),
            }
            for r in rows
            if isinstance(r, dict)
        ]

    async def undo_mutation(self, mutation_id: str) -> dict:
        """Roll back one completed mutation through the RollbackExecutor."""
        if self._store_mutations is None:
            return {"status": "error", "error": "mutation store unavailable"}
        from athena.state.rollback import RollbackExecutor

        executor = RollbackExecutor(self._store_mutations, self._artifacts)
        try:
            outcome = await executor.execute_inverse(mutation_id)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        # Emit an event so the surface and audit trail see the rollback.
        try:
            sink = self._forward_events(self._require_events())
            await sink(
                make_event(
                    "MutationRolledBack",
                    {
                        "mutation_id": mutation_id,
                        "outcome": outcome.get("status"),
                        "rollback_id": outcome.get("rollback_id"),
                    },
                )
            )
        except Exception as exc:
            _logger.warning("rollback event emission failed: %s", exc)
        return outcome

    async def operator_context_summary(self, session_id: str | None = None) -> dict:
        """What the model would actually see next turn (bounded-context view)."""
        info: dict = {"session_id": session_id}
        if session_id and self._store_messages is not None:
            try:
                info["message_count"] = await self._store_messages.count_session_messages(
                    session_id
                )
            except Exception as exc:
                _logger.warning("session message count failed: %s", exc)
        if self._compiler is not None:
            try:
                window = getattr(self._compiler, "context_window", None)
                reserve = getattr(self._compiler, "reserve_output", None)
                recent = getattr(self._compiler, "recent_verbatim_turns", None)
                info["window"] = int(window) if window else None
                info["reserve_output"] = int(reserve) if reserve else None
                info["recent_verbatim_turns"] = int(recent) if recent else None
            except Exception:
                pass
        return info

    async def operator_artifacts(self, *, limit: int = 50) -> list[dict]:
        """Artifact index across all tasks (evidence view)."""
        if self._artifacts is None:
            return []
        try:
            refs = await self._artifacts.list(limit=limit)
        except Exception as exc:
            _logger.warning("artifact listing failed: %s", exc)
            return []
        out: list[dict] = []
        for ref in refs:
            out.append(
                {
                    "uri": getattr(ref, "uri", None),
                    "name": getattr(ref, "name", None),
                    "mime_type": getattr(ref, "mime_type", None),
                    "kind": getattr(ref, "kind", None),
                    "task_id": getattr(ref, "task_id", None),
                    "producer": getattr(ref, "producer", None),
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Internal wiring
    # ------------------------------------------------------------------ #
    def _build_task_spec(self, request: AgentRequest, session_id: str) -> TaskSpec:
        ws = request.workspace or self._default_workspace
        autonomy = request.autonomy or self.config.autonomy_level
        # Preserve request metadata (autonomy + any caller-supplied fields)
        meta = {"autonomy": autonomy.value}
        if request.metadata:
            meta.update(request.metadata)
        # Acceptance criteria (BHV-005): ``metadata["acceptance_criteria"]``
        # carries human-specified checks. Each entry becomes a required
        # Criterion so the termination evaluator audits claimed completion.
        criteria: list = []
        raw_criteria = meta.pop("acceptance_criteria", None)
        if isinstance(raw_criteria, (list, tuple)):
            from athena.protocol.tasks import Criterion, VerificationSpec, VerificationType

            for i, item in enumerate(raw_criteria):
                text = str(item or "").strip()
                if not text:
                    continue
                # "command:..." prefix selects an executable probe; otherwise
                # the criterion is model-judged against task evidence.
                if text.lower().startswith("command:"):
                    verification = VerificationSpec(
                        type=VerificationType.COMMAND,
                        command=text.split(":", 1)[1].strip(),
                    )
                else:
                    verification = VerificationSpec(
                        type=VerificationType.MODEL_JUDGMENT,
                        predicate=text,
                    )
                criteria.append(Criterion(
                    id=f"ac_{i+1}",
                    description=text,
                    verification=verification,
                    required=True,
                ))
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
                context_refs.append(ContextRef(
                    kind="artifact",
                    ref=str(att.uri),
                    source_id=getattr(att, "id", None),
                    summary=getattr(att, "producer", None),
                    mime_type=getattr(att, "mime_type", None),
                ))
            elif isinstance(att, dict):
                context_refs.append(ContextRef(
                    kind=att.get("kind", "artifact"),
                    ref=str(att.get("ref", att.get("uri", "")) or ""),
                    source_id=att.get("source_id"),
                    summary=att.get("summary"),
                    mime_type=att.get("mime_type"),
                ))
        spec_kwargs: dict = dict(
            id=request.task_id or new_id("task"),
            objective=request.prompt,
            session_id=session_id,
            workspace=ws,
            model_policy=request.model_policy or _default_model_policy(),
            resource_budget=ResourceBudget(),
            context_refs=tuple(context_refs),
            metadata=meta,
        )
        if criteria:
            spec_kwargs["acceptance_criteria"] = tuple(criteria)
        if cap_policy is not None:
            spec_kwargs["capability_policy"] = cap_policy
        return TaskSpec(**spec_kwargs)

    def _workspace_reader(self):
        """Return a workspace instruction reader bound to the current workspace.

        Used by the context compiler to load the hierarchical AGENTS.md chain.
        Returns None when no workspace is bound so the compiler degrades gracefully.
        """
        from athena.context.instructions import hierarchical_agents_md

        root_value = (
            getattr(self._default_workspace, "root", None)
            if self._default_workspace else None
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

        return _Reader()

    def _build_verifier(
        self, *, execution, dispatcher, artifact_store, capability_registry,
        model_registry, evidence_provider=None,
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
        )

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
                dict(row) for row in
                (await self._store_executions.list_for_task(task.id))[-25:]
            ]
        if self._store_mutations is not None:
            evidence["mutations"] = [
                dict(row) for row in
                (await self._store_mutations.list_for_task(task.id))[-25:]
            ]
        result = await self.get_result(task.id)
        if result is not None:
            evidence["result"] = {
                "status": result.status.value,
                "summary": result.summary,
                "unresolved": list(result.unresolved),
                "artifacts": [getattr(ref, "uri", str(ref))
                              for ref in result.artifacts],
            }
        result_data = evidence.get("result", {})
        return {
            "evidence": evidence,
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
                    _logger.warning(
                        "model_roles[%r].max_cost_usd invalid: %r", role, raw_cost
                    )
            out[str(role)] = ModelPolicy(
                role=str(role),
                allowed=allowed,
                privacy=str(spec.get("privacy") or "local-preferred"),
                require_tools=bool(spec.get("require_tools", False)),
                max_cost_usd=max_cost,
            )
        return out

    def _make_model_summarizer(self, model_registry: Any):
        """Build the compression summarizer used by the context compiler.

        Uses the ``summarizer`` role's model policy (falling back to the
        user's primary choice when no role entry exists). On any failure it
        returns None, and ContextCompressor falls back to deterministic
        truncation — so offline/test operation is unaffected.
        """
        if model_registry is None:
            return None

        async def _summarize(text: str) -> str | None:
            try:
                from athena.protocol.tasks import ModelPolicy

                selection = await self._router.select(
                    policy=ModelPolicy(role="summarizer", require_tools=False)
                )
                provider = model_registry.provider_for(selection.provider)
                from athena.protocol.ids import new_id
                from athena.protocol.messages import (
                    Message,
                    Provenance,
                    Role,
                    SourceType,
                    TextBlock,
                    TrustClass,
                    utcnow,
                )
                from athena.protocol.models import ModelRequest

                prompt = (
                    "Summarize the following agent-work transcript excerpt into "
                    "at most 6 sentences, preserving decisions, file changes, "
                    "and unresolved issues. Output ONLY the summary.\n\n" + text[-8000:]
                )
                request = ModelRequest(
                    messages=(Message(
                        id=new_id("msg"),
                        role=Role.USER,
                        blocks=(TextBlock(type="text", text=prompt),),
                        created_at=utcnow(),
                        provenance=Provenance(
                            source_type=SourceType.RUNTIME,
                            trust=TrustClass.AGENT_CURATED,
                            scope="context_compression",
                        ),
                    ),),
                    model=selection.model,
                    provider=selection.provider,
                    request_id=new_id("sum"),
                )
                parts: list[str] = []
                usage_id = None
                if self._provider_usage is not None:
                    try:
                        usage_id = await self._provider_usage.record_attempt(
                            provider=selection.provider,
                            model=selection.model,
                            metadata={"role": "summarizer",
                                      "purpose": "context_compression"},
                        )
                    except Exception:
                        usage_id = None
                async for event in provider.complete(request):
                    if getattr(event, "type", None) is not None and event.type.value == "done":
                        resp = event.response
                        if resp is not None:
                            from athena.protocol.messages import TextBlock as _TB
                            parts[:] = [
                                b.text for b in resp.blocks
                                if isinstance(b, _TB) and b.text
                            ]
                            if usage_id is not None:
                                try:
                                    usage = getattr(resp, "usage", None)
                                    await self._provider_usage.record_completion(
                                        usage_id,
                                        input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                                        output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                                    )
                                    usage_id = None
                                except Exception:
                                    pass
                summary = " ".join(parts).strip()
                return summary or None
            except Exception as exc:
                _logger.debug("model summarizer unavailable (%s); using truncation", exc)
                return None

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
                context=context, system_prompt=system_prompt,
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

    def _register_core_capabilities(
        self, *, registry, workspace, execution, memory, skills_store,
        research_store=None
    ) -> None:
        from athena.capabilities.artifacts import ArtifactCapability

        registry.register(FilesystemCapability(workspace))
        if self._artifacts is not None:
            registry.register(ArtifactCapability(self._artifacts))
        registry.register(ExecuteCapability(execution, workspace, artifact_store=self._artifacts))
        registry.register(MemoryCapability(memory))
        registry.register(SkillsCapability(skills_store))
        registry.register(DelegateCapability(self._delegation))
        # Computational-body capabilities (P0 roadmap).
        from athena.capabilities.system import MachineCapability, ProcessCapability
        from athena.capabilities.terminal_session import TerminalSessionCapability
        from athena.capabilities.dependency import DependencyCapability
        from athena.capabilities.reflection import CapabilityReflection
        from athena.capabilities.research import ResearchCapability
        from athena.capabilities.scratch import ScratchCapability
        from athena.capabilities.workflow import WorkflowCapability
        from athena.research.policy import SourcePolicy

        # Scratch and formal synthesis share one engine so a useful one-shot
        # helper can be promoted without losing source/provenance.
        if self._synthesis is None:
            from athena.synthesis.engine import SynthesisEngine
            self._synthesis = SynthesisEngine()
        registry.register(ScratchCapability(self._synthesis, self._scratch))

        if TerminalSessionCapability.available():
            self._terminals = TerminalSessionCapability()
            registry.register(self._terminals)
        else:
            _logger.info("terminal_session capability unavailable: install pexpect and pyte")
        self._processes = ProcessCapability(execution)
        registry.register(self._processes)
        registry.register(MachineCapability())
        registry.register(CapabilityReflection(
            self._fabric,
            workflow_store=self._workflow_store,
            skills_store=skills_store,
            execution_manager=execution,
        ))
        registry.register(DependencyCapability(execution))
        if research_store is not None:
            registry.register(ResearchCapability(
                research_store,
                artifact_store=self._artifacts,
                source_policy=SourcePolicy(
                    allowed_domains=tuple(self.config.research_allowed_domains),
                    denied_domains=tuple(self.config.research_denied_domains),
                    allow_private_network=self.config.research_allow_private_network,
                ),
            ))
        if self._workflow_store is not None and self._fabric is not None:
            registry.register(WorkflowCapability(
                self._workflow_store, self._dispatcher, self._fabric))
        try:
            from athena.capabilities.debugger import DebuggerCapability

            if DebuggerCapability.available():
                self._debugger = DebuggerCapability()
                registry.register(self._debugger)
            else:
                _logger.info(
                    "debugger capability unavailable: debugpy is not installed")
        except Exception as exc:  # debugpy optional
            _logger.info("debugger capability unavailable: %s", exc)

        # P1/P2 environment families.
        from athena.capabilities.environment import (
            DatabaseCapability, NetworkCapability, ServiceCapability,
            WorkspaceCapability,
        )
        from athena.capabilities.watch import WatchCapability, WatchRegistry

        registry.register(ServiceCapability())
        registry.register(NetworkCapability())
        self._database = DatabaseCapability()
        registry.register(self._database)
        self._watch_registry = WatchRegistry()
        self._watches = WatchCapability(registry=self._watch_registry)
        registry.register(self._watches)
        if self._task_manager is not None:
            self._task_manager.add_finalize_observer(self._cleanup_task_watches)
            self._task_manager.add_finalize_observer(self._cleanup_task_affordances)
        from athena.causal.checkpoint import CheckpointManager

        self._checkpoints = CheckpointManager()
        registry.register(WorkspaceCapability(
            checkpoint_manager=self._checkpoints,
            mutation_store=self._store_mutations,
            mutation_observer=self._on_mutation_completed,
        ))

        # Capability-owned resource teardown (P1-32).
        if hasattr(self, "_terminals"):
            self.register_shutdown_hook(
                "terminal_sessions", self._terminals.close_all)
        if hasattr(self, "_debugger"):
            self.register_shutdown_hook(
                "debugger_sessions", self._debugger.close_all)
        if hasattr(self, "_watch_registry"):
            self.register_shutdown_hook(
                "watch_registry", self._watch_registry.close)

    def register_shutdown_hook(self, name: str, hook) -> None:
        """Register a capability-owned resource teardown (P1-32)."""
        self._shutdown_hooks.append((name, hook))

    async def _run_shutdown_hooks(self) -> None:
        for name, hook in reversed(self._shutdown_hooks):
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook()
                else:
                    result = hook()
                    if inspect.isawaitable(result):
                        await result
            except Exception as exc:
                _logger.warning("shutdown hook %s failed: %s", name, exc)
        self._shutdown_hooks.clear()

    # ------------------------------------------------------------------ #
    # Fusion engines: shadow execution + execution-grounded world state
    # ------------------------------------------------------------------ #
    async def _poll_watches(self) -> None:
        """Background poll of registered watches -> WatchObserved events."""
        events = None
        while True:
            await asyncio.sleep(2.0)
            registry = getattr(self, "_watch_registry", None)
            if registry is None or not (registry.file_watches
                                        or registry.process_watches):
                continue
            if events is None:
                try:
                    events = self._require_events()
                except Exception:
                    continue

            async def sink(type_, payload, task_id=None):
                await events.append_event(type_, payload, task_id=task_id)

            try:
                await registry.poll_all(sink)
            except Exception as exc:
                _logger.debug("watch poll error: %s", exc)

    async def _cleanup_task_watches(self, task, result) -> None:
        registry = getattr(self, "_watch_registry", None)
        if registry is not None:
            registry.remove_task(getattr(task, "id", None))

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
        cache = getattr(self, "_world_states", {})
        for world_state in list(cache.values()):
            if task_id is None or world_state.task_id == task_id:
                world_state.claims.invalidate_for_paths(
                    [resource], mutation_id=mutation_id,
                    mutation_sequence=mutation_sequence,
                    mutation_event_sequence=mutation_event_sequence)
        store = getattr(self, "_world_state_store", None)
        if store is not None and task_id is not None:
            try:
                await store.invalidate_for_paths(
                    task_id, [resource], mutation_id=mutation_id,
                    mutation_sequence=mutation_sequence,
                    mutation_event_sequence=mutation_event_sequence)
            except Exception as exc:
                _logger.warning("durable claim invalidation failed: %s", exc)

    def shadow_engine(self):
        """Speculative-execution engine bound to this service's dispatcher."""
        from athena.shadow.engine import ShadowEngine

        if getattr(self, "_shadow", None) is None:
            self._shadow = ShadowEngine()
        if self._shadow._dispatcher is None and self._dispatcher is not None:
            self._shadow.bind(self._dispatcher)
        self._shadow.bind_service(self)
        return self._shadow

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
            registry.register("fake", FakeModelProvider(
                tool_calling=True, model="fake-1", provider="fake"))
            registry.set_profile("fake", resolve_profile("fake", model_id="fake-1"))
            registry.set_model_profile(
                "fake", "fake-1", ModelProfile(model_pattern="fake-1"))
            return
        provider: Any = None
        for pc in pcs:
            if pc.kind == "fake":
                provider = FakeModelProvider(
                    tool_calling=True,
                    model=pc.model,
                    provider=pc.name,
                    scripts=list(pc.extra.get("scripts") or []),
                )
                registry.register(pc.name, provider)
                registry.set_profile(
                    pc.name, resolve_profile("fake", model_id=pc.model)
                )
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
            )
            if profile.protocol in {"openai", "openai-compat"}:
                if not profile.base_url:
                    raise ValueError(
                        f"provider {pc.name!r} needs an explicit base_url"
                    )
                provider = OpenAICompatProvider(
                    base_url=profile.base_url,
                    api_key=self._resolve_api_key(pc),
                    model=profile.model_id or pc.model,
                    provider=pc.name,
                    headers=pc.extra.get("headers"),
                    timeout=float(pc.extra.get("timeout", 60.0)),
                    http2=bool(pc.extra.get("http2", False)),
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
                    await self._mcp.collect_and_register(
                        client, server_alias=server.name
                    )
            except Exception as exc:
                _logger.warning("MCP server %s failed to connect: %s", server.name, exc)
                # Track failed connections for visibility
                self._mcp_connection_status = getattr(self, '_mcp_connection_status', {})
                self._mcp_connection_status[server.name] = f"failed: {exc}"
                continue

    def _resolve_api_key(self, pc: ProviderConfig) -> str:
        """Return the key for a provider, preferring a leased credential.

        A ``credential_id`` is treated as the NAME of a secret and resolved
        through the SecretManager at the authorized boundary; the raw
        ``api_key`` field remains a backward-compatible fallback.
        """
        if pc.credential_id and self._secrets is not None:
            return self._secrets.resolve(pc.credential_id)
        if pc.api_key is not None:
            return pc.api_key
        return ""

    # ------------------------------------------------------------------ #
    # Internal accessors
    # ------------------------------------------------------------------ #
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

    return ModelPolicy(require_tools=True)


def _model_profile_from_config(
    model_id: str, raw: Any,
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
        "tools_structured", "tools_parallel", "tools_textual_fallback",
        "reasoning_native", "empty_content_with_tools",
        "requires_tool_result_name", "requires_assistant_replay_fields",
        "malformed_json_tendency", "context_window", "output_limit",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            "unknown model_profile fields: " + ", ".join(sorted(unknown)))
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
            ContextRef(kind=i.get("kind", "session"), ref=i.get("ref", ""),
                       source_id=i.get("source_id"), summary=i.get("summary"),
                       mime_type=i.get("mime_type"))
            for i in items if isinstance(i, dict)
        )
    if kind == "ArtifactRef":
        return tuple(
            ArtifactRef(id=i.get("id", ""), uri=i.get("uri", ""), hash=i.get("hash"),
                        mime_type=i.get("mime_type"), size=i.get("size"),
                        producer=i.get("producer"), task_id=i.get("task_id"),
                        metadata=i.get("metadata") or {})
            for i in items if isinstance(i, dict)
        )
    if kind == "MutationRef":
        return tuple(
            MutationRef(id=i.get("id", ""), resource=i.get("resource", ""),
                        operation=i.get("operation", ""),
                        reversible=bool(i.get("reversible", False)))
            for i in items if isinstance(i, dict)
        )
    return ()
