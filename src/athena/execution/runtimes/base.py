"""BaseRuntime — common scaffolding for execution runtimes.

A runtime maps a name to a computation backend and bridges blocking subprocess
I/O into the async ``execute`` stream required by the ``Runtime`` protocol.

Contract (canonical signature, per protocol/execution.py ``Runtime``):

    async def execute(self, request, execution_id) -> AsyncIterator[ExecutionEvent]

``BaseRuntime`` provides:

* one persistent session per ``task_id`` (task-scoped persistent state,
  BHV-058; closing on task cancellation);
* an interrupt registry mapping execution_id -> owning session;
* ``_bridge``: a generic adapter that runs a blocking generator in a worker
  thread and re-yields its ``ExecutionEvent`` items as an async stream, keeping
  the event loop responsive.

Subclasses implement the runtime-specific sync generator (typically
``_run(session, request, execution_id)``) together with the process/session
lifecycle. Runtimes MUST route their process management through
``athena.execution.process_tree`` so cancellation can wipe the owned tree.
"""

from __future__ import annotations

import abc
import asyncio
import inspect
import os
import queue as thread_queue
import threading
from typing import Any, AsyncIterator, Mapping, cast

from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionRequest,
)

__all__ = ["BaseRuntime"]


class BaseRuntime(metaclass=abc.ABCMeta):
    """Abstract base for persistent, process-backed runtimes."""

    name: str = ""            # required: unique runtime name
    aliases: tuple[str, ...] = ()
    persistence: str = "persistent"

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        # execution_id -> session id (interrupt registry, BHV-061 ownership).
        self._exec_owner: dict[str, str] = {}
        # Serializes overlapping executions on the same runtime session
        # (BHV-058/059/060): concurrent run() on one session must not interleave.
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Concrete session bookkeeping (shared scaffolding)
    # ------------------------------------------------------------------ #
    def _register_session(self, runtime_session_id: str, session: Any) -> None:
        self._sessions[runtime_session_id] = session

    def _session_for(self, runtime_session_id: str) -> Any:
        return self._sessions[runtime_session_id]

    def _adopt_or_create(
        self, request: ExecutionRequest
    ) -> tuple[str, Any]:
        """Return (session_id, session) for ``request``, creating a task-scoped
        session synchronously if none exists (BHV-058 persistent default).

        If ``request`` carries per-request cwd/env that differ from the
        existing session's, a *new* session is spawned so the differences are
        honored instead of silently dropped (BUILDSPEC 43). The new session is
        tracked under a distinct id; it is still task-scoped and will be closed
        on task cancellation.
        """
        sid = request.runtime_session_id
        if sid and sid in self._sessions:
            session = self._sessions[sid]
            if self._request_matches_session(request, session):
                return sid, session
            # An explicit session id was requested with differing env/cwd: close
            # the old one and spawn a fresh session under the same id.
            self._close_session(session)
            self._sessions.pop(sid, None)
            self._register_session(sid, self._make_session_for_request(request))
            return sid, self._sessions[sid]
        existing_sid = f"{self.name}_{request.task_id}"
        if existing_sid in self._sessions:
            existing = self._sessions[existing_sid]
            if self._request_matches_session(request, existing):
                return existing_sid, existing
            runtime_session_id = f"{self.name}_{request.task_id}_{len(self._sessions) + 1}"
            self._register_session(runtime_session_id, self._make_session_for_request(request))
            return runtime_session_id, self._sessions[runtime_session_id]
        runtime_session_id = existing_sid
        self._register_session(runtime_session_id, self._make_session_for_request(request))
        return runtime_session_id, self._sessions[runtime_session_id]

    def _make_session_for_request(self, request: ExecutionRequest) -> Any:
        """Create a session while remaining compatible with small test runtimes."""
        kwargs: dict[str, Any] = {
            "env": request.env or None,
            "cwd": request.cwd,
            "sandbox_root": request.workspace_root,
            "network_policy": (
                request.network_policy.value
                if request.network_policy is not None
                else None
            ),
        }
        try:
            params = inspect.signature(self._make_session).parameters
            kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass
        return self._make_session(**kwargs)

    def _request_matches_session(self, request: ExecutionRequest, session: Any) -> bool:
        if request.cwd is not None and getattr(session, "cwd", None) != request.cwd:
            return False
        if request.env:
            session_env = getattr(session, "env", {}) or {}
            merged = dict(session_env)
            merged.update(request.env)
            if merged != session_env:
                return False
        if request.workspace_root is not None:
            requested_root = os.path.realpath(os.path.abspath(request.workspace_root))
            session_root = getattr(session, "sandbox_root", None)
            if session_root is None or os.path.realpath(
                os.path.abspath(session_root)
            ) != requested_root:
                return False
        if request.network_policy is not None:
            requested_network = getattr(
                request.network_policy, "value", request.network_policy
            )
            if getattr(session, "network_policy", None) != requested_network:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Runtime protocol (async)
    # ------------------------------------------------------------------ #
    async def create_session(
        self,
        *,
        task_id: str,
        backend: str = "local",
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,  # noqa: N802
        workspace_root: str | None = None,
        network_policy: str | None = None,
    ) -> str:
        runtime_session_id = f"{self.name}_{task_id}"
        self._register_session(
            runtime_session_id,
            self._make_session(
                env=env,
                cwd=cwd,
                sandbox_root=workspace_root,
                network_policy=network_policy,
            ),
        )
        return runtime_session_id

    async def execute(
        self, request: ExecutionRequest, execution_id: str
    ) -> AsyncIterator[ExecutionEvent]:
        """Default async-gen bridge over the blocking ``_run`` generator."""
        sid, session = self._adopt_or_create(request)
        self._exec_owner[execution_id] = sid
        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        reported = False
        try:
            async with lock:
                gen = self._run(session, request, execution_id)
                async for event in self._bridge_sync_generator(gen):
                    if not reported:
                        reported = True
                        metadata = dict(event.metadata or {})
                        metadata["runtime_session_id"] = sid
                        yield ExecutionEvent(
                            type=event.type,
                            execution_id=event.execution_id,
                            data=event.data,
                            exit_status=event.exit_status,
                            exit_code=event.exit_code,
                            duration_ms=event.duration_ms,
                            metadata=metadata,
                        )
                        continue
                    yield event
        finally:
            self._exec_owner.pop(execution_id, None)

    @abc.abstractmethod
    def _make_session(self, *, env: Mapping[str, str] | None = None,
                      cwd: str | None = None, sandbox_root: str | None = None,
                      network_policy: str | None = None) -> Any:
        ...

    @abc.abstractmethod
    def _run(self, session: Any, request: ExecutionRequest,
             execution_id: str) -> Any:
        ...

    # -- interrupt / close -------------------------------------------------- #
    async def interrupt(self, execution_id: str) -> None:
        session = self._resolve_by_execution(execution_id)
        if session is not None:
            self._interrupt_session(session)

    async def reset(self, runtime_session_id: str) -> None:
        await self.close(runtime_session_id)

    async def close(self, runtime_session_id: str) -> None:
        session = self._sessions.pop(runtime_session_id, None)
        if session is not None:
            self._close_session(session)

    async def close_all(self) -> None:
        for sid in list(self._sessions):
            await self.close(sid)

    # -- synchronous overridables (called from close()/interrupt()) ---------- #
    def _close_session(self, session: Any) -> None:
        """Synchronously tear down a session's process tree."""
        close = getattr(session, "close", None) or getattr(session, "terminate", None)
        if close is not None:
            close()

    def _interrupt_session(self, session: Any) -> None:
        interrupt = getattr(session, "interrupt", None)
        if interrupt is not None:
            interrupt()

    # ------------------------------------------------------------------ #
    # Internal resolution helpers
    # ------------------------------------------------------------------ #
    def _resolve_by_execution(self, execution_id: str) -> Any:
        sid = self._exec_owner.get(execution_id)
        return self._sessions.get(sid) if sid else None

    # -- async bridging helper ------------------------------------------- #
    @staticmethod
    async def _bridge_sync_generator(gen: Any) -> AsyncIterator[ExecutionEvent]:
        """Yield items from a sync generator ``gen`` on the event loop by
        running it in a worker thread and bridging over an asyncio.Queue."""
        if inspect.isasyncgen(gen):
            async for event in gen:
                # ``gen`` is intentionally accepted as ``Any`` because runtime
                # implementations expose different generator types.  Keep the
                # public bridge typed at the protocol boundary.
                yield cast(ExecutionEvent, event)
            return

        queue: thread_queue.Queue = thread_queue.Queue()
        done = threading.Event()

        def _runner() -> None:
            try:
                for item in gen:
                    queue.put(("item", item))
            except BaseException as exc:  # surface to async side
                queue.put(("error", exc))
            finally:
                done.set()
                queue.put(("done", None))

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        while True:
            try:
                kind, value = queue.get_nowait()
            except thread_queue.Empty:
                if done.is_set():
                    break
                await asyncio.sleep(0.01)
                continue
            if kind == "item":
                yield value
            elif kind == "error":
                raise value
            else:
                break
        # The generator has signalled completion. A short join makes thread
        # ownership explicit without ever blocking the event loop on runtime
        # I/O. The thread is daemonized as a final safety net for a broken
        # third-party runtime.
        thread.join(timeout=0)
