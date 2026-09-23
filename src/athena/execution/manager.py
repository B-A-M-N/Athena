"""ExecutionManager — the single execution authority (INV-005).

All process execution initiated by the agent flows through here. The manager:

* maps runtime names to concrete runtimes and owns runtime sessions per task;
* normalizes both sync-generator runtimes (the OI-derived shell/python workers)
  and async-generator runtimes (the Runtime protocol) into one async stream,
  offloading blocking runtimes to a worker thread so the event loop is never
  frozen;
* enforces process-tree cleanup on task cancellation;
* caps accumulated output to bound memory (large outputs are artifactized by
  callers via ArtifactStore, BUILDSPEC §53-54).

Application modules MUST NOT call subprocess.run/os.system for agent work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Mapping

from athena.execution.backend_readiness import BackendReadiness
from athena.execution.cancellation import CancellationRegistry, RuntimeCancellationResult
from athena.execution.receipts import ExecutionReceiptPersistence
from athena.execution.result_buffer import ExecutionResultCollector
from athena.execution.runtime_readiness import RuntimeReadiness
from athena.execution.routing import ExecutionRouting
from athena.execution.session_lifecycle import ExecutionSessionCoordinator
from athena.execution.shutdown import ExecutionShutdownCoordinator
from athena.execution.streaming import ExecutionStreamCoordinator
from athena.execution.backend import ExecutionBackend
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    Runtime,
)
from athena.protocol.ids import new_id

_DEFAULT_MAX_BYTES = 8 * 1024 * 1024

_logger = logging.getLogger("athena.execution")


class Sink:
    """Async output sink. Subclasses override ``chunk``."""

    async def chunk(self, text: str, *, stream: str = "stdout") -> None: ...


class ExecutionManager:
    def __init__(
        self,
        *,
        runtime_session_store=None,
        execution_store=None,
        event_sink=None,
        durability_mandatory: bool = False,
        recovery_sink=None,
    ) -> None:
        self._routing = ExecutionRouting()
        self._cancellation_registry = CancellationRegistry()
        self._runtime_readiness = RuntimeReadiness(
            runtimes=lambda: self._routing.runtimes,
            task_sessions=lambda: self._cancellation_registry.task_sessions,
            execution_runtimes=lambda: self._cancellation_registry.exec_runtimes,
        )
        self._rt_sessions = runtime_session_store
        self._exec_store = execution_store
        self._event_sink = event_sink
        self._receipts = ExecutionReceiptPersistence(
            runtime_session_store=runtime_session_store,
            execution_store=execution_store,
            event_sink=event_sink,
            durability_mandatory=durability_mandatory,
        )
        self._sessions = ExecutionSessionCoordinator(
            registry=self._cancellation_registry,
            select_backend=self._selected_backend,
            resolve_runtime=self._resolve,
            lookup_backend=self._routing.backend_for_reattach,
            persist_session_start=self._persist_session_start,
        )
        self._streaming = ExecutionStreamCoordinator(self)
        self._shutdown = ExecutionShutdownCoordinator(
            registry=self._cancellation_registry,
            routing=self._routing,
            cancel_task=self.cancel_task,
        )
        self._durability_mandatory = durability_mandatory
        self._recovery_sink = recovery_sink
        self._backend_readiness = BackendReadiness(
            self._routing.catalog,
            local_backend=lambda: self._routing.local_backend,
            available_runtimes=lambda: self.available_runtimes(),
            selected_backend=self._selected_backend,
            available_backends=lambda: self.available_backends(),
        )
        # A cancellation may race the final backend event.  This marker keeps
        # a late session identity from being re-adopted after its process tree
        # was closed, which would otherwise leave a dead session in the
        # manager's live-resource accounting.

    def set_local_backend(self, backend: ExecutionBackend | None) -> None:
        """Select an operator-owned local backend such as the runtime host."""
        self._routing.set_local_backend(backend)

    def set_backend_passport(self, backend: str, passport: Mapping[str, Any]) -> None:
        """Bind a release-bound behavioral passport to a backend inventory row."""
        self._routing.set_backend_passport(backend, dict(passport))

    def set_recovery_sink(self, sink) -> None:
        """Bind the task-state recovery authority after construction."""
        self._recovery_sink = sink

    def register_runtime(self, runtime: Runtime) -> None:
        self._routing.register_runtime(runtime)

    def register_backend(self, backend: ExecutionBackend) -> None:
        """Register a non-local execution backend.

        Local, shadow, sandbox, and verification requests intentionally remain
        on the manager's existing runtime path.  Additional backends are
        selected by ``ExecutionRequest.backend`` and still inherit this
        manager's persistence, event, ownership, and cancellation handling.
        """
        self._routing.register_backend(backend)

    def available_backends(self) -> list[str]:
        return self._backend_readiness.available_backends()

    def backend_status(self) -> list[dict[str, Any]]:
        """Return availability for registered non-local backends."""
        return self._backend_readiness.backend_status()

    def backend_capabilities(self, name: str = "local") -> Any:
        """Return the declared capability contract for one execution target."""
        return self._backend_readiness.backend_capabilities(name)

    def backend(self, name: str) -> ExecutionBackend | None:
        """Return the selected backend for structured backend RPCs."""
        return self._backend_readiness.backend(name)

    def resolve_backend(self, name: str | None = None) -> object:
        """Resolve the canonical backend identity using execution semantics.

        This is the single preflight authority for readiness.  It returns the
        selected backend object for registered/local backends, or a structured
        explicit result for built-in runtime backends whose implementation is
        selected only at execution time.  Unknown names fail closed.
        """
        return self._backend_readiness.resolve_backend(name)

    async def check_backend(self, name: str | None = None) -> dict[str, Any]:
        """Prove effective availability for the backend execution would use."""
        return await self._backend_readiness.check_backend(name)

    def available_runtimes(self) -> list[str]:
        return self._runtime_readiness.available_runtimes()

    def runtime_status(self) -> list[dict[str, Any]]:
        return self._runtime_readiness.status()

    def has_runtime(self, name: str) -> bool:
        return self._runtime_readiness.has_runtime(name)

    def validate_execution_request(self, request: ExecutionRequest) -> None:
        """Reject limits that the selected execution boundary cannot enforce."""
        self._validate_resource_limits(
            request.backend, request.runtime, request.resource_limits
        )

    def _validate_resource_limits(self, backend: str, runtime: str, resource_limits) -> None:
        if resource_limits is None:
            return
        selected_backend = self._selected_backend(backend)
        if selected_backend is not None:
            capabilities = selected_backend.capabilities()
            if not bool(getattr(capabilities, "resource_limits", False)):
                raise RuntimeError(
                    f"backend {backend!r} does not enforce resource limits"
                )
            return
        runtime_impl = self._resolve(runtime)
        if not bool(getattr(runtime_impl, "supports_resource_limits", False)):
            raise RuntimeError(
                f"runtime {runtime!r} does not enforce resource limits"
            )

    def has_live_runtime(self, task_id: str) -> bool:
        """Return whether this task currently owns an execution/session."""
        if self._cancellation_registry.task_sessions.get(task_id):
            return True
        return any(
            owner == task_id for owner in self._cancellation_registry.executions.values()
        ) or bool(self._cancellation_registry.pending_runtime_cancellations.get(task_id))

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        backend: str = "local",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        workspace_root: str | None = None,
        network_policy: str | None = None,
        resource_limits=None,
    ) -> str:
        self._validate_resource_limits(backend, runtime, resource_limits)
        return await self._sessions.create_session(
            task_id=task_id,
            runtime=runtime,
            backend=backend,
            cwd=cwd,
            env=env,
            workspace_root=workspace_root,
            network_policy=network_policy,
            resource_limits=resource_limits,
        )

    async def reattach_session(self, record: Mapping[str, Any]) -> bool:
        """Reattach one persisted backend session after identity proof.

        Local runtimes deliberately return ``False``: their worker processes
        are service-lifetime resources until a durable supervisor exists.
        Backend-specific implementations may opt in, but the returned session
        id must exactly match durable state before ownership is rebuilt.
        """
        return await self._sessions.reattach_session(record)

    async def execute(
        self,
        request: ExecutionRequest,
        execution_id: str | None = None,
        *,
        sink: Sink | None = None,
        max_results_bytes: int | None = _DEFAULT_MAX_BYTES,
    ) -> ExecutionResult:
        execution_id = execution_id or new_id("exec")
        return await ExecutionResultCollector(max_results_bytes=max_results_bytes).collect(
            self.stream(request, execution_id),
            execution_id=execution_id,
            sink=sink,
        )

    def stream(
        self,
        request: ExecutionRequest,
        execution_id: str | None = None,
    ) -> AsyncIterator[ExecutionEvent]:
        """Return the canonical manager-owned execution event stream."""
        return self._streaming.stream(request, execution_id)

    async def interrupt(self, execution_id: str) -> None:
        rt = self._resolve_runtime_by_execution(execution_id)
        if rt is None:
            return
        fn = getattr(rt, "interrupt", None)
        if fn is None:
            return
        if asyncio.iscoroutinefunction(fn):
            await fn(execution_id)
        else:
            fn(execution_id)

    async def destroy_session(self, runtime_session_id: str) -> None:
        """Close one owned runtime/backend session.

        This is intentionally narrower than ``cancel_task`` for capabilities
        such as the debugger that own a long-lived auxiliary session but must
        not tear down the rest of the task's execution surface.
        """
        await self._cancellation_registry.destroy_session(
            runtime_session_id,
            persist_session_closed=self._persist_session_closed,
        )

    async def cancel_task(self, task_id: str) -> RuntimeCancellationResult:
        """Interrupt/close every runtime session owned by a task (tree cancel).

        Runtime failures are returned to the task cancellation authority. A
        caller must not convert a failed close into a durable CANCELLED state.
        """
        return await self._cancellation_registry.cancel_task(
            task_id,
            persist_session_closed=self._persist_session_closed,
        )

    async def close_task(self, task_id: str) -> RuntimeCancellationResult:
        """Task-finalization lifecycle hook for the shared resource coordinator."""
        return await self.cancel_task(task_id)

    async def close_all(self) -> dict[str, Any]:
        return await self._shutdown.close_all()

    def live_resource_count(self) -> int:
        return self._shutdown.live_resource_count()

    async def _emit_event(self, event_type: str, payload: dict, task_id: str | None = None) -> None:
        """Emit an execution event to the event sink if one is configured."""
        if self._event_sink is None:
            return
        try:
            from athena.protocol.events import make_event

            event = make_event(event_type, payload, task_id=task_id)
            await self._event_sink(event)
        except Exception as exc:  # rationale: owned shutdown/persistence boundary must record failure and preserve task state
            _logger.warning("failed to emit execution event %s: %s", event_type, exc)

    def is_session_owned_by_task(self, runtime_session_id: str, task_id: str) -> bool:
        """Check if a runtime session is owned by a given task.

        Prevents a model from attaching itself to another task's runtime
        session by guessing its ID.
        """
        return self._cancellation_registry.session_owners.get(runtime_session_id) == task_id

    def owns_process(
        self,
        task_id: str | None,
        pid: int,
        start_identity: str | None = None,
    ) -> bool:
        """Return whether *pid* is a currently live Athena runtime process.

        Process control is keyed to the live ``Popen`` object, rather than a
        bare PID. That makes a recycled PID fail closed after the original
        runtime exits and keeps host-process control out of the normal
        ``process`` capability. When supplied, ``start_identity`` is checked
        against Linux's process-start token as an additional PID-reuse guard.
        """
        if not task_id or pid <= 0:
            return False
        from athena.execution.process_tree import process_start_identity

        current_identity = process_start_identity(pid)
        if current_identity is None:
            return False
        if start_identity is not None and current_identity != str(start_identity):
            return False
        for runtime, known_sid in self._cancellation_registry.task_sessions.get(task_id, ()):
            session = getattr(runtime, "_sessions", {}).get(known_sid)
            process = getattr(session, "process", None)
            if process is not None and process.pid == pid and process.poll() is None:
                return True
        for execution_id, (
            runtime,
            adopted_sid,
        ) in self._cancellation_registry.exec_runtimes.items():
            if (
                self._cancellation_registry.executions.get(execution_id) != task_id
                or not adopted_sid
            ):
                continue
            session = getattr(runtime, "_sessions", {}).get(adopted_sid)
            process = getattr(session, "process", None)
            if process is not None and process.pid == pid and process.poll() is None:
                return True
        return False

    def _selected_backend(self, name: str) -> ExecutionBackend | None:
        return self._routing.select_backend(name, available_backends=self.available_backends)

    def _resolve(self, name: str) -> Runtime:
        return self._routing.resolve_runtime(name, available_runtimes=self.available_runtimes)

    def _resolve_runtime_by_execution(self, execution_id: str) -> Runtime | None:
        return self._routing.resolve_runtime_by_execution(
            execution_id,
            executions=self._cancellation_registry.executions,
            execution_runtimes=self._cancellation_registry.exec_runtimes,
            task_sessions=self._cancellation_registry.task_sessions,
        )

    # ------------------------------------------------------------------ #
    # Persistence (P0-22): runtime_sessions + executions behind the stores.
    # ------------------------------------------------------------------ #
    async def _persist_session_start(
        self,
        session_id: str,
        task_id: str,
        *,
        backend: str,
        runtime: str,
        cwd: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await self._receipts.session_start(
            session_id,
            task_id,
            backend=backend,
            runtime=runtime,
            cwd=cwd,
            metadata=metadata,
        )

    async def _persist_session_closed(self, session_id: str) -> None:
        await self._receipts.session_closed(session_id)

    async def _update_execution_runtime_session(
        self, execution_id: str, runtime_session_id: str
    ) -> None:
        await self._receipts.update_execution_runtime_session(execution_id, runtime_session_id)

    async def _persist_execution_start(
        self,
        execution_id: str,
        *,
        task_id: str,
        runtime_session_id: str | None,
        source: str,
        cwd: str | None = None,
        env=None,
    ) -> None:
        await self._receipts.execution_start(
            execution_id,
            task_id=task_id,
            runtime_session_id=runtime_session_id,
            source=source,
            cwd=cwd,
            env=env,
        )

    async def _persist_execution_finish(
        self,
        execution_id: str,
        *,
        task_id: str,
        runtime: str,
        backend: str,
        cwd: str | None,
        exit_status,
        exit_code: int | None,
        timed_out: bool = False,
        interrupted: bool = False,
        execution_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await self._receipts.execution_finish(
            execution_id,
            task_id=task_id,
            runtime=runtime,
            backend=backend,
            cwd=cwd,
            exit_status=exit_status,
            exit_code=exit_code,
            timed_out=timed_out,
            interrupted=interrupted,
            execution_metadata=execution_metadata,
        )


__all__ = ["ExecutionManager", "Sink"]
