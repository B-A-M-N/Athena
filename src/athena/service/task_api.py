"""Task intake and observation mechanism for the service façade (P1-10).

Moved verbatim from ``athena.service.service``. This is a subordinate
mechanism, not a second authority: every store, worker, kernel, compiler,
and validation seam resolves through the owning :class:`AthenaService`
instance (``self._svc``), so admission, session allocation, canonical user
persistence, enqueue ordering, and event replay remain single-sourced on
the façade exactly as when these bodies lived there.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.tasks import TERMINAL_STATUSES, AgentRequest, TaskSpec
from athena.tasks.manager import TaskManager

__all__ = ["TaskAPI"]


class TaskAPI:
    """Task intake (submit/enqueue) and observation (wait/stream/get)."""

    def __init__(self, service: Any) -> None:
        self._svc = service

    # ------------------------------------------------------------------ #
    # Intake
    # ------------------------------------------------------------------ #

    async def submit(self, request: AgentRequest, *, wait: bool = True) -> TaskSpec:
        """Turn an :class:`AgentRequest` into a Task and optionally drive it
        through the worker to completion (BHV-002: all work becomes a Task)."""
        tm = self._svc._require_task_manager()
        await self._svc.require_agent_ready(request)
        self._svc._validate_request_metadata(request.metadata)
        session_id = request.session_id or new_id("session")
        spec = self._svc._build_task_spec(request, session_id)
        # Admission must inspect the canonical TaskSpec as well as the
        # request envelope. This keeps typed acceptance criteria and every
        # future authority field on the same preflight path.
        await self._svc.require_task_ready(spec)
        return await self._enqueue_spec(tm, spec, wait=wait, user_request=request)

    async def submit_spec(
        self,
        spec: TaskSpec,
        *,
        wait: bool = False,
        user_request: Any | None = None,
        trusted: bool = False,
    ) -> TaskSpec:
        """Submit an already-decoded task through the service intake.

        Transports such as ACP may decode their wire envelope, but they do not
        own admission, session allocation, canonical user persistence, task
        creation, or enqueue ordering. Keeping those operations here makes all
        transports share the same authority boundary.
        """
        tm = self._svc._require_task_manager()
        await self._svc.require_task_ready(spec)
        if not trusted:
            self._svc._validate_request_metadata(spec.metadata)
        if not spec.session_id:
            session_id = new_id("session")
            spec = replace(spec, session_id=session_id)
        if self._svc._sessions is not None and spec.session_id:
            if await self._svc._sessions.get(spec.session_id) is None:
                await self._svc._sessions.create(
                    spec.session_id,
                    metadata={"origin": "service"},
                    principal_id=self._svc.config.cache_namespace,
                    project_id=getattr(spec.workspace, "id", None),
                )
        return await self._enqueue_spec(tm, spec, wait=wait, user_request=user_request)

    async def _enqueue_spec(
        self,
        task_manager: TaskManager,
        spec: TaskSpec,
        *,
        wait: bool,
        user_request: AgentRequest | None = None,
    ):
        created = await task_manager.create(spec)
        # Every task gets a durable causal root before it can run. Transport
        # callers provide the original request; internal/scheduled callers
        # use the TaskSpec objective. This prevents same-session tasks from
        # inheriting whichever unrelated turn happened to be most recent.
        await self._svc._record_canonical_user_turn(user_request or created, created)
        # Precompute the revisioned static context concurrently with worker
        # pickup (P1: precompute before first inference). The compile path
        # remains the sole authority — this only warms its cache, guarded by
        # the same revisions, so a stale warm entry is recomputed, never
        # trusted. Failure is swallowed: prefetch must never fail admission.
        self._svc._spawn_static_prefetch(created)
        await task_manager.enqueue(created.id)
        if wait:
            await self.wait_for(created.id)
        return created

    # ------------------------------------------------------------------ #
    # Observation
    # ------------------------------------------------------------------ #

    async def run_task(self, task_id: str) -> TaskSpec:
        """Drive the NAMED task through the kernel synchronously.

        The named task is acquired by id and run by the kernel, so it does not
        race the background ``run_forever`` worker for the next claimed task.
        """
        worker = self._svc._require_worker()
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
                manager = self._svc._task_manager
                if manager is not None and hasattr(manager, "wait_for_finalization"):
                    remaining = max(deadline - time.monotonic(), 0.0)
                    try:
                        await manager.wait_for_finalization(
                            task_id,
                            timeout=remaining,
                        )
                    except TimeoutError:
                        # The durable result is still authoritative. A slow
                        # optional observer must not turn a completed task
                        # into an unavailable one for callers with a deadline.
                        pass
                kernel = self._svc._kernel
                if kernel is not None and hasattr(kernel, "wait_for_completion"):
                    remaining = max(deadline - time.monotonic(), 0.0)
                    try:
                        await kernel.wait_for_completion(task_id, timeout=remaining)
                    except TimeoutError:
                        # The durable result is authoritative; the barrier only
                        # protects callers that require a fully quiesced run.
                        pass
                return task
            if time.monotonic() >= deadline:
                return task
            await asyncio.sleep(0.02)

    async def get_task(self, task_id: str) -> TaskSpec:
        """Return the persisted :class:`TaskSpec` (status in ``metadata["status"]``)."""
        return await self._svc._require_task_manager().get(task_id)

    async def get_result(self, task_id: str):
        """Return the :class:`TaskResult` (or None if not yet finalised)."""
        mgr = self._svc._require_task_manager()
        return await mgr.get_result(task_id)

    async def stream_events(self, task_id: str, after_sequence: int = 0):
        """Yield a task's events, ordered by sequence (replayable log).

        Polls the event store while the task is still running so the stream is
        live: new events appended after the last yielded sequence are streamed
        out as they arrive. The generator stops once the task reaches a
        terminal status (and flushes any remaining events).
        """
        events = self._svc._require_events()
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
            generation = events.append_generation
            try:
                await asyncio.wait_for(events.wait_for_append(generation), timeout=0.1)
            except TimeoutError:
                pass

    async def stream_all(self, after_rowid: int = 0, limit: int = 200):
        """Yield events across ALL tasks in insertion order (live tail).

        Backs the OI stream viewer: a read-only global subscription to the
        canonical event log. Never terminates; the caller cancels it.
        """
        events = self._svc._require_events()
        cursor = after_rowid
        while True:
            items = await events.list_recent(after_rowid=cursor, limit=limit)
            for ev in items:
                rid = getattr(ev, "_rowid", None)
                if isinstance(rid, int) and rid > cursor:
                    cursor = rid
                yield ev
            generation = events.append_generation
            try:
                await asyncio.wait_for(events.wait_for_append(generation), timeout=0.15)
            except TimeoutError:
                pass

    async def get_task_status(self, task_id: str) -> str | None:
        """Return the task's status string (from ``metadata["status"]``), or None."""
        try:
            task = await self.get_task(task_id)
        except Exception:
            return None
        return (task.metadata or {}).get("status")


def _is_terminal_status(status: str | None) -> bool:
    if not status:
        return False
    return status in {s.value for s in TERMINAL_STATUSES}
