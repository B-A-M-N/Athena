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
import logging
import os
import tempfile
from typing import Any, Mapping

from athena.artifacts.store import ArtifactStore
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
from athena.tasks.budgets import BudgetTracker
from athena.tasks.cancellation import CancellationManager
from athena.tasks.delegation import DelegationManager
from athena.tasks.manager import TaskManager
from athena.tasks.worker import TaskWorker, WorkerConfig

from athena.protocol.events import Event, make_event
from athena.protocol.ids import new_id
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
        self._sessions: SessionRepository | None = None

        # Subsystem handles (assigned in start()).
        self._secrets: SecretManager | None = None
        self._execution: ExecutionManager | None = None
        self._policy: PolicyEngine | None = None
        self._registry: CapabilityRegistry | None = None
        self._dispatcher: CapabilityDispatcher | None = None
        self._compiler: ContextCompiler | None = None
        self._model_registry: ProviderRegistry | None = None
        self._kernel: AgentKernel | None = None
        self._task_manager: TaskManager | None = None
        self._worker: TaskWorker | None = None
        self._worker_task: asyncio.Task | None = None
        self._budgets: BudgetTracker | None = None
        self._cancellations: CancellationManager | None = None
        self._delegation: DelegationManager | None = None
        self._memory: MemoryStore | None = None
        self._skills: SkillStore | None = None
        self._scheduler: Scheduler | None = None
        self._artifacts: ArtifactStore | None = None
        self._mcp: MCPAdapter | None = None

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
        self._sessions = sessions
        self._store_tasks = tasks
        self._store_events = events
        self._store_messages = messages
        self._store_approvals = approvals
        self._store_mutations = mutations
        self._store_schedules = schedules

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

        # 5. Artifacts (construct BEFORE dispatcher so it can be injected).
        self._artifacts = ArtifactStore(root=cfg.artifact_root)

        # 6. Capability registry + dispatcher (single path, INV-004).
        registry = CapabilityRegistry()
        self._registry = registry
        dispatcher = CapabilityDispatcher(
            registry,
            policy,
            mutation_store=mutations,
            approval_store=approvals,
            event_sink=self._forward_events(events),
            artifact_store=self._artifacts,
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
            capability_registry=registry,
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
            context_compiler=compiler,
            termination=TerminationEvaluator(
                acceptance_verifier=self._build_verifier(
                    execution=execution,
                    artifact_store=self._artifacts,
                    capability_registry=registry,
                    model_registry=model_registry,
                ),
            ),
            dispatch_factory=self._dispatch_factory,
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
        )

        # 12b. Register schedule capability over the scheduler subsystem.
        registry.register(ScheduleCapability(ScheduleAPI(self._scheduler, self._task_manager)))

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

        # 13. Worker + scheduler.
        worker = TaskWorker(
            task_manager=task_manager,
            kernel=kernel,
            config=WorkerConfig(max_parallel=cfg.worker_max_parallel),
        )
        self._worker = worker
        self._worker_task = asyncio.create_task(self._worker.run_forever())

        scheduler = Scheduler(
            store=schedules,
            task_manager=task_manager,
            max_concurrent=cfg.scheduler_max_concurrent,
            loop_interval_seconds=cfg.scheduler_interval_seconds,
        )
        self._scheduler = scheduler

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
            try:
                await self._db.close()
            except Exception as exc:
                _logger.warning("db close failed: %s", exc)
            self._db = None

        self._cancellations = None
        self._started = False

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

        if task_id is not None and self._kernel is not None:
            await self._kernel.notify_approval_resolved(
                task_id, "granted" if granted else "denied"
            )

    def _install_grant(
        self,
        approval_id: str,
        task_id: str | None,
        metadata: dict,
        scope: str | None = None,
        first: ApprovalScope | None = None,
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
                )
            manager.grant(approval_id, resolver="user")
        except Exception:
            pass

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
        """A focused, human-inspection summary of a task's life."""
        task = await self.get_task(task_id)
        result = await self.get_result(task_id)
        gathered = [ev async for ev in self.stream_events(task_id, after_sequence=0)]
        return {
            "task_id": task_id,
            "status": (task.metadata or {}).get("status"),
            "objective": task.objective,
            "result": result,
            "events": [e.type for e in gathered],
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
            elif isinstance(att, dict):
                context_refs.append(ContextRef(
                    kind=att.get("kind", "artifact"),
                    ref=att.get("ref", att.get("uri", "")),
                    source_id=att.get("source_id"),
                    summary=att.get("summary"),
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

        root = getattr(self._default_workspace, "root", None) if self._default_workspace else None
        if not root:
            return None

        class _Reader:
            def list_agents_md(_self):
                try:
                    return hierarchical_agents_md(root)
                except Exception:
                    return []

        return _Reader()

    def _build_verifier(self, *, execution, artifact_store, capability_registry, model_registry):
        """Build the acceptance verifier for the kernel."""
        from athena.kernel.verifiers import CompositeVerifier
        return CompositeVerifier(
            execution=execution,
            artifact_store=artifact_store,
            capability_registry=capability_registry,
            model_registry=model_registry,
        )

    def _dispatch_factory(self, task: TaskSpec):
        if self._dispatcher is None:
            raise RuntimeError("AthenaService not started")
        ws = task.workspace or self._default_workspace
        profile = (task.metadata or {}).get("autonomy") or self.config.autonomy_level.value
        return CapabilityDispatchShim(self._dispatcher, ws, profile=profile)

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
                async for event in provider.complete(request):
                    if getattr(event, "type", None) is not None and event.type.value == "done":
                        resp = event.response
                        if resp is not None:
                            from athena.protocol.messages import TextBlock as _TB
                            parts[:] = [
                                b.text for b in resp.blocks
                                if isinstance(b, _TB) and b.text
                            ]
                summary = " ".join(parts).strip()
                return summary or None
            except Exception as exc:
                _logger.debug("model summarizer unavailable (%s); using truncation", exc)
                return None

        return _summarize

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
        self, *, registry, workspace, execution, memory, skills_store
    ) -> None:
        registry.register(FilesystemCapability(workspace))
        registry.register(ExecuteCapability(execution, workspace, artifact_store=self._artifacts))
        registry.register(MemoryCapability(memory))
        registry.register(SkillsCapability(skills_store))
        registry.register(DelegateCapability(self._delegation))
        # Computational-body capabilities (P0 roadmap).
        from athena.capabilities.system import MachineCapability, ProcessCapability
        from athena.capabilities.terminal_session import TerminalSessionCapability

        self._terminals = TerminalSessionCapability()
        registry.register(self._terminals)
        self._processes = ProcessCapability()
        registry.register(self._processes)
        registry.register(MachineCapability())
        try:
            from athena.capabilities.debugger import DebuggerCapability

            self._debugger = DebuggerCapability()
            registry.register(self._debugger)
        except Exception as exc:  # debugpy optional
            _logger.info("debugger capability unavailable: %s", exc)

    def _register_providers(self, registry: ProviderRegistry) -> None:
        pcs = tuple(self.config.providers)
        if not pcs:
            registry.register(
                "fake",
                FakeModelProvider(tool_calling=True, model="fake-1", provider="fake"),
            )
            return
        for pc in pcs:
            if pc.kind == "fake":
                registry.register(
                    pc.name,
                    FakeModelProvider(
                        tool_calling=True,
                        model=pc.model,
                        provider=pc.name,
                        scripts=list(pc.extra.get("scripts") or []),
                    ),
                )
            elif pc.kind in ("openai", "openai-compat"):
                registry.register(
                    pc.name,
                    OpenAICompatProvider(
                        base_url=pc.base_url or "https://api.openai.com/v1",
                        api_key=self._resolve_api_key(pc),
                        model=pc.model,
                        provider=pc.name,
                    ),
                )
            elif pc.kind == "anthropic":
                registry.register(
                    pc.name,
                    AnthropicProvider(
                        api_key=self._resolve_api_key(pc) or None,
                        model=pc.model,
                        provider=pc.name,
                    ),
                )
            else:
                raise ValueError(f"unknown provider kind: {pc.kind!r}")

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
                       source_id=i.get("source_id"), summary=i.get("summary"))
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
