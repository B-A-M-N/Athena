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
import inspect
import logging
import time
from typing import Any, AsyncIterator, cast

from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
    ExecutionRequest,
    ExecutionResult,
    Runtime,
)
from athena.protocol.ids import new_id

_DEFAULT_MAX_BYTES = 8 * 1024 * 1024

_logger = logging.getLogger("athena.execution")


class Sink:
    """Async output sink. Subclasses override ``chunk``."""

    async def chunk(self, text: str, *, stream: str = "stdout") -> None:
        ...


class ExecutionManager:
    def __init__(
        self,
        *,
        runtime_session_store=None,
        execution_store=None,
        event_sink=None,
        durability_mandatory: bool = True,
    ) -> None:
        self._runtimes: dict[str, Runtime] = {}
        self._executions: dict[str, str] = {}                 # execution_id -> task_id
        self._exec_runtimes: dict[str, tuple[Runtime, str | None]] = {}  # execution_id -> (runtime, runtime_session_id)
        self._task_sessions: dict[str, list[tuple[Runtime, str]]] = {}
        self._runtime_by_session: dict[str, Runtime] = {}
        self._rt_sessions = runtime_session_store
        self._exec_store = execution_store
        self._event_sink = event_sink
        self._durability_mandatory = durability_mandatory

    def register_runtime(self, runtime: Runtime) -> None:
        name = getattr(runtime, "name", None)
        if not name:
            raise ValueError("runtime must define a non-empty 'name'")
        self._runtimes[name] = runtime
        for alias in getattr(runtime, "aliases", ()) or ():
            self._runtimes.setdefault(alias, runtime)

    def available_runtimes(self) -> list[str]:
        return sorted(self._runtimes.keys())

    def has_runtime(self, name: str) -> bool:
        return name in self._runtimes

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        rt = self._resolve(runtime)
        kwargs: dict[str, Any] = {"task_id": task_id}
        if env is not None:
            kwargs["env"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd
        try:
            sig = inspect.signature(rt.create_session)
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        if asyncio.iscoroutinefunction(rt.create_session):
            sid = await rt.create_session(**kwargs)
        else:
            # create_session is a plain (non-coroutine) callable on this runtime.
            sid = cast(str, rt.create_session(**kwargs))
        self._task_sessions.setdefault(task_id, []).append((rt, sid))
        self._runtime_by_session[sid] = rt
        await self._persist_session_start(sid, task_id, runtime, cwd=cwd)
        return sid

    async def execute(
        self,
        request: ExecutionRequest,
        execution_id: str | None = None,
        *,
        sink: Sink | None = None,
        max_results_bytes: int | None = _DEFAULT_MAX_BYTES,
    ) -> ExecutionResult:
        execution_id = execution_id or new_id("exec")
        start = time.monotonic()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stdout_bytes = 0
        stderr_bytes = 0
        exit_status: ExecutionExitStatus = ExecutionExitStatus.FAILED
        exit_code: int | None = None
        cap = max_results_bytes or float("inf")

        if sink:
            await sink.chunk("", stream="start")

        async for event in self.stream(request, execution_id):
            etype = event.type
            if etype == ExecutionEventType.STDOUT:
                data = event.data or ""
                if stdout_bytes < cap:
                    take = data if stdout_bytes + len(data) <= cap else data[: int(cap - stdout_bytes)]
                    stdout_parts.append(take)
                    stdout_bytes += len(take)
                    if stdout_bytes >= cap:
                        stdout_parts.append("\n[output truncated]")
                if sink:
                    await sink.chunk(data, stream="stdout")
            elif etype == ExecutionEventType.STDERR:
                data = event.data or ""
                if stderr_bytes < cap:
                    take = data if stderr_bytes + len(data) <= cap else data[: int(cap - stderr_bytes)]
                    stderr_parts.append(take)
                    stderr_bytes += len(take)
                if sink:
                    await sink.chunk(data, stream="stderr")
            elif etype == ExecutionEventType.EXITED:
                exit_status = event.exit_status or ExecutionExitStatus.EXITED
                exit_code = event.exit_code

        if sink:
            await sink.chunk("", stream="exit")

        return ExecutionResult(
            execution_id=execution_id,
            exit_code=exit_code,
            status=exit_status,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def stream(
        self,
        request: ExecutionRequest,
        execution_id: str | None = None,
    ) -> AsyncIterator[ExecutionEvent]:
        """Yield raw ``ExecutionEvent`` items from the target runtime in real time.

        This is the canonical streaming API (protocol/execution.py docstring:
        "Streaming is the canonical API"). ``execute()`` is a buffering
        convenience built on top of it (INV-005: all process execution flows
        through this manager).
        """
        execution_id = execution_id or new_id("exec")
        self._executions[execution_id] = request.task_id
        rt = self._resolve(request.runtime)
        runtime_session_id: str | None = request.runtime_session_id
        if runtime_session_id and runtime_session_id not in self._runtime_by_session:
            self._runtime_by_session[runtime_session_id] = rt
        exit_status: ExecutionExitStatus | None = None
        exit_code: int | None = None
        persisted = False
        await self._persist_execution_start(
            execution_id,
            task_id=request.task_id,
            runtime_session_id=runtime_session_id,
            source=request.source,
            cwd=request.cwd,
            env=request.env,
        )
        # Emit execution started event
        await self._emit_event(
            "ExecutionStarted",
            {
                "execution_id": execution_id,
                "task_id": request.task_id,
                "runtime": request.runtime,
            },
            task_id=request.task_id,
        )
        try:
            async for event in rt.execute(request, execution_id):
                meta = event.metadata or {}
                if meta.get("runtime_session_id"):
                    await self._adopt_runtime_session(
                        rt, meta["runtime_session_id"], request.task_id
                    )
                    self._exec_runtimes[execution_id] = (
                        rt, meta["runtime_session_id"]
                    )
                    if not persisted:
                        persisted = True
                        await self._update_execution_runtime_session(
                            execution_id, meta["runtime_session_id"]
                        )
                else:
                    self._exec_runtimes[execution_id] = (
                        rt, runtime_session_id
                    )
                # Emit stream events for observability
                if event.type == ExecutionEventType.STDOUT:
                    await self._emit_event(
                        "StdoutChunk",
                        {"execution_id": execution_id, "data": event.data or ""},
                        task_id=request.task_id,
                    )
                elif event.type == ExecutionEventType.STDERR:
                    await self._emit_event(
                        "StderrChunk",
                        {"execution_id": execution_id, "data": event.data or ""},
                        task_id=request.task_id,
                    )
                if event.type == ExecutionEventType.EXITED:
                    exit_status = event.exit_status
                    exit_code = event.exit_code
                yield event
            adopted = self._exec_runtimes.get(execution_id)
            if adopted:
                rt, sid = adopted
                if sid:
                    await self._adopt_runtime_session(rt, sid, request.task_id)
        finally:
            self._executions.pop(execution_id, None)
            self._exec_runtimes.pop(execution_id, None)
            track_exit = exit_status is not None
            final_status = track_exit and exit_status or ExecutionExitStatus.FAILED
            await self._persist_execution_finish(
                execution_id,
                task_id=request.task_id,
                runtime=request.runtime,
                backend=request.backend,
                cwd=request.cwd,
                exit_status=final_status,
                exit_code=exit_code,
                timed_out=(track_exit and exit_status == ExecutionExitStatus.TIMED_OUT),
                interrupted=(track_exit and exit_status == ExecutionExitStatus.INTERRUPTED),
            )
            # Emit execution exited event for observability
            exit_event_type = "ExecutionExited"
            if final_status == ExecutionExitStatus.TIMED_OUT:
                exit_event_type = "ExecutionTimedOut"
            elif final_status == ExecutionExitStatus.INTERRUPTED:
                exit_event_type = "ExecutionInterrupted"
            await self._emit_event(
                exit_event_type,
                {
                    "execution_id": execution_id,
                    "task_id": request.task_id,
                    "runtime": request.runtime,
                    "exit_code": exit_code,
                    "exit_status": final_status.value,
                },
                task_id=request.task_id,
            )

    async def _adopt_runtime_session(self, rt: Runtime, runtime_session_id: str, task_id: str) -> None:
        if runtime_session_id in self._runtime_by_session:
            return
        self._runtime_by_session[runtime_session_id] = rt
        rooms = self._task_sessions.setdefault(task_id, [])
        if not any(sid == runtime_session_id for _r, sid in rooms):
            rooms.append((rt, runtime_session_id))
        await self._persist_session_start(
            runtime_session_id, task_id, getattr(rt, "name", None),
        )

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

    async def cancel_task(self, task_id: str) -> None:
        """Interrupt/close every runtime session owned by a task (tree cancel)."""
        loop = asyncio.get_running_loop()
        rooms = list(self._task_sessions.get(task_id, []))
        # Enumerate sessions across ALL executions of this task, including
        # ones the runtimes adopted implicitly during execute() (BHV-061/062).
        seen: set[tuple[int, str]] = {(id(rt), sid) for rt, sid in rooms}
        for _exec_id, (rt, sid) in list(self._exec_runtimes.items()):
            if self._executions.get(_exec_id) != task_id:
                continue
            if sid and (id(rt), sid) not in seen:
                rooms.append((rt, sid))
                seen.add((id(rt), sid))
        self._task_sessions.pop(task_id, None)

        async def _close_all() -> None:
            for rt, sid in rooms:
                self._runtime_by_session.pop(sid, None)
                close = getattr(rt, "close", None) or getattr(rt, "destroy_session", None)
                if close is not None:
                    if asyncio.iscoroutinefunction(close):
                        await close(sid)
                    else:
                        await loop.run_in_executor(None, close, sid)
                await self._persist_session_closed(sid)

        await _close_all()
        for rt in set(self._runtimes.values()):
            cancel = getattr(rt, "cancel_task", None)
            if cancel is not None:
                try:
                    if asyncio.iscoroutinefunction(cancel):
                        await cancel(task_id)
                    else:
                        await loop.run_in_executor(None, cancel, task_id)
                except Exception:
                    pass

    async def close_all(self) -> None:
        for task_id in list(self._task_sessions):
            await self.cancel_task(task_id)

    async def _emit_event(self, event_type: str, payload: dict, task_id: str | None = None) -> None:
        """Emit an execution event to the event sink if one is configured."""
        if self._event_sink is None:
            return
        try:
            from athena.protocol.events import make_event
            event = make_event(event_type, payload, task_id=task_id)
            await self._event_sink(event)
        except Exception as exc:
            _logger.warning("failed to emit execution event %s: %s", event_type, exc)

    def is_session_owned_by_task(self, runtime_session_id: str, task_id: str) -> bool:
        """Check if a runtime session is owned by a given task.

        Prevents a model from attaching itself to another task's runtime
        session by guessing its ID.
        """
        # Direct task_sessions lookup
        for rt, sid in self._task_sessions.get(task_id, ()):
            if sid == runtime_session_id:
                return True
        # Check adopted sessions from executions of this task
        for _exec_id, (rt, sid) in self._exec_runtimes.items():
            if sid == runtime_session_id and self._executions.get(_exec_id) == task_id:
                return True
        return False

    def _resolve(self, name: str) -> Runtime:
        rt = self._runtimes.get(name)
        if rt is None:
            raise RuntimeError(f"no such runtime: {name!r}; registered: {self.available_runtimes()}")
        return rt

    def _resolve_runtime_by_execution(self, execution_id: str) -> Runtime | None:
        _task_id = self._executions.get(execution_id)
        if _task_id is None:
            return None
        entry = self._exec_runtimes.get(execution_id)
        if entry is not None:
            return entry[0]
        for rt, _sid in self._task_sessions.get(_task_id, []):
            return rt
        return next(iter(self._runtimes.values()), None)

    # ------------------------------------------------------------------ #
    # Persistence (P0-22): runtime_sessions + executions behind the stores.
    # ------------------------------------------------------------------ #
    async def _persist_session_start(
        self, session_id: str, task_id: str, runtime: str | None, cwd: str | None = None
    ) -> None:
        store = self._rt_sessions
        if store is None:
            return
        try:
            await store.start(
                session_id, task_id=task_id, backend=runtime or "unknown",
                runtime=runtime, cwd=cwd,
            )
        except Exception as exc:
            _logger.warning("failed to persist session start %s: %s", session_id, exc)

    async def _persist_session_closed(self, session_id: str) -> None:
        store = self._rt_sessions
        if store is None:
            return
        try:
            await store.mark_closed(session_id)
        except Exception as exc:
            _logger.warning("failed to persist session close %s: %s", session_id, exc)

    async def _update_execution_runtime_session(
        self, execution_id: str, runtime_session_id: str
    ) -> None:
        store = self._exec_store
        if store is None:
            return
        try:
            await store.update_runtime_session(execution_id, runtime_session_id)
        except Exception as exc:
            _logger.warning("failed to update execution runtime session %s: %s", execution_id, exc)

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
        """Persist the pre-execution record. This is durability-mandatory:
        a failure here means the execution is not recorded, so we raise
        (the caller will not run what it cannot audit)."""
        store = self._exec_store
        if store is None:
            return
        try:
            await store.start(
                execution_id,
                task_id=task_id,
                runtime_session_id=runtime_session_id,
                command=source,
                cwd=cwd,
                env=dict(env) if env else None,
            )
        except Exception as exc:
            _logger.error("failed to persist execution start %s: %s", execution_id, exc)
            await self._emit_event(
                "ExecutionStartPersistFailed",
                {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "error": str(exc),
                },
                task_id=task_id,
            )
            if self._durability_mandatory:
                raise

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
    ) -> None:
        """Persist the post-effect record. Best-effort: log + emit event on
        failure so the audit gap is visible but the execution result is not
        lost."""
        store = self._exec_store
        if store is None:
            return
        try:
            await store.finish(
                execution_id,
                status=exit_status,
                exit_code=exit_code,
                metadata={
                    "task_id": task_id,
                    "runtime": runtime,
                    "backend": backend,
                    "cwd": cwd,
                    "timed_out": timed_out,
                    "interrupted": interrupted,
                },
            )
        except Exception as exc:
            _logger.warning("failed to persist execution finish %s: %s", execution_id, exc)
            await self._emit_event(
                "ExecutionFinishPersistFailed",
                {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "error": str(exc),
                },
                task_id=task_id,
            )


__all__ = ["ExecutionManager", "Sink"]
