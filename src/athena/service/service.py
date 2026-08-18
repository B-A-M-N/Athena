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

from athena.protocol.events import Event
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

        # 8. Models + router.
        model_registry = ProviderRegistry()
        self._register_providers(model_registry)
        self._model_registry = model_registry

        # 9. Context compiler.
        compiler = ContextCompiler(
            message_store=messages,
            memory_store=memory,
            skill_loader=skills_store,
            capability_registry=registry,
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
    ) -> dict:
        """Execute code directly WITHOUT routing through the model loop.

        Used by the CLI ``!``/``!!`` shell escapes. The execution still flows
        through policy and approval (safety), but bypasses AgentKernel inference.

        Args:
            source: The code/shell command to execute.
            language: Runtime language (``shell``, ``python``, ``node``, ...).
            cwd: Working directory (must be within workspace root).
            session_id: Optional session to record the execution against.
            inject_into_context: If True, the result is recorded as a capability
                result in the session transcript (the ``!`` form). If False,
                the result is only returned/printed (the ``!!`` form).

        Returns:
            A result dict with ``exit_code``, ``stdout``, ``stderr``, ``status``.
        """
        execution = self._require_execution()
        ws = self._default_workspace
        # Resolve cwd within workspace
        if cwd:
            from pathlib import Path as _Path
            root = _Path(ws.root).resolve()
            if _Path(cwd).is_absolute():
                candidate = _Path(cwd).resolve()
            else:
                candidate = (root / cwd).resolve()
            if candidate != root and not str(candidate).startswith(str(root) + "/"):
                cwd = None
            else:
                cwd = str(candidate)

        from athena.protocol.execution import ExecutionRequest

        # Map language to runtime name
        runtime_name = language
        if language in ("sh", "bash", "zsh"):
            runtime_name = "shell"
        elif language in ("py", "python3"):
            runtime_name = "python"
        elif language in ("js", "nodejs"):
            runtime_name = "node"
        elif language in ("ps1", "pwsh", "cmd"):
            runtime_name = "powershell"

        if runtime_name not in execution.available_runtimes():
            return {"exit_code": 1, "stdout": "", "stderr": f"runtime unavailable: {language}", "status": "failed"}

        task_id = new_id("direct")
        exec_req = ExecutionRequest(
            runtime=runtime_name,
            source=source,
            task_id=task_id,
            workspace_id=ws.id,
            cwd=cwd,
        )
        exec_id = new_id("exec")
        result = await execution.execute(exec_req, exec_id)

        status_str = result.status.value if result.status else "failed"
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "status": status_str,
            "duration_ms": result.duration_ms,
        }

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

        try:
            if manager.state(approval_id) is None:
                manager.create_request(
                    Principal("agent", "athena"),
                    scope_choice,
                    capability=cap,
                    effect=str(primary_name) if primary_name else None,
                    task_id=task_id,
                    approval_id=approval_id,
                    args_digest=digest or None,
                    call_id=call_id,
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
    # Internal wiring
    # ------------------------------------------------------------------ #
    def _build_task_spec(self, request: AgentRequest, session_id: str) -> TaskSpec:
        ws = request.workspace or self._default_workspace
        autonomy = request.autonomy or self.config.autonomy_level
        # Preserve request metadata (autonomy + any caller-supplied fields)
        meta = {"autonomy": autonomy.value}
        if request.metadata:
            meta.update(request.metadata)
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