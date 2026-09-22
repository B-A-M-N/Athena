"""Task intake and observation mechanism for the service façade.

The explicit port owns the support boundary; admission and task state remain
authoritative on :class:`AthenaService`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.errors import ServiceNotReady
from athena.protocol.tasks import TERMINAL_STATUSES, AgentRequest, TaskSpec, TaskStatus
from athena.tasks.manager import TaskManager

__all__ = ["TaskAPI"]


class TaskAPIPorts:
    """Allowlisted admission/runtime resources and application operations."""

    _RESOURCE_NAMES = {
        "require_task_manager": "_require_task_manager",
        "require_worker": "_require_worker",
        "require_events": "_require_events",
        "sessions": "_sessions",
        "task_manager": "_task_manager",
        "kernel": "_kernel",
        "store_tasks": "_store_tasks",
        "config": "config",
    }
    _APPLICATION_OPERATIONS = {
        "require_agent_ready": "require_agent_ready",
        "validate_request_metadata": "_validate_request_metadata",
        "build_task_spec": "_build_task_spec",
        "_validate_request_metadata": "_validate_request_metadata",
        "_build_task_spec": "_build_task_spec",
        "require_task_ready": "require_task_ready",
        "check_complex_coding_readiness": "check_complex_coding_readiness",
        "_emit": "_emit",
        "emit": "_emit",
        "normalize_spec": "normalize_spec",
        "_record_canonical_user_turn": "_record_canonical_user_turn",
        "record_canonical_user_turn": "_record_canonical_user_turn",
        "_spawn_static_prefetch": "_spawn_static_prefetch",
        "spawn_static_prefetch": "_spawn_static_prefetch",
    }

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        resource_name = self._RESOURCE_NAMES.get(name)
        if resource_name is None:
            resource_name = self._APPLICATION_OPERATIONS.get(name)
        if resource_name is None:
            raise AttributeError(f"task api port is not allowed: {name}")
        return getattr(self._owner, resource_name, None)


class TaskAPI:
    """Task intake (submit/enqueue) and observation (wait/stream/get)."""

    def __init__(self, service: Any, *, ports: TaskAPIPorts | None = None) -> None:
        self._ports = ports or TaskAPIPorts(service)

    async def submit(self, request: AgentRequest, *, wait: bool = True) -> TaskSpec:
        """Turn an :class:`AgentRequest` into a Task and optionally drive it
        through the worker to completion (BHV-002: all work becomes a Task)."""
        tm = self._ports.require_task_manager()
        await self._ports.require_agent_ready(request)
        self._ports.validate_request_metadata(request.metadata)
        session_id = request.session_id or new_id("session")
        spec = self._ports.build_task_spec(request, session_id)
        # Admission must inspect the canonical TaskSpec as well as the
        # request envelope. This keeps typed acceptance criteria and every
        # future authority field on the same preflight path.
        await self._ports.require_task_ready(spec)
        # Complex-coding readiness: surface structured gaps before the model
        # starts planning around unavailable tooling (review item 18).
        await self._reject_complex_readiness_gaps(spec)
        return await self.enqueue_spec(tm, spec, wait=wait, user_request=request)

    async def _reject_complex_readiness_gaps(self, spec: TaskSpec) -> None:
        """Raise ServiceNotReady for required gaps; emit optional diagnostics."""
        readiness = await self._ports.check_complex_coding_readiness(spec)
        if not readiness.get("ready", False):
            raise ServiceNotReady(
                "complex coding requires candidate isolation and independent verification runtime",
                missing=[gap["check"] for gap in readiness.get("required_gaps", ())],
                gaps=readiness.get("required_gaps", ()),
            )
        if readiness.get("optional_gaps"):
            from athena.protocol.events import EV

            await self._ports.emit(
                EV["DIAGNOSTICS_PRODUCED"],
                {
                    "kind": "complex_coding_readiness_gap",
                    "gaps": readiness["optional_gaps"],
                },
                spec.id,
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
        """Submit an already-decoded task through the service intake.

        Transports such as ACP may decode their wire envelope, but they do not
        own admission, session allocation, canonical user persistence, task
        creation, or enqueue ordering. Keeping those operations here makes all
        transports share the same authority boundary.
        """
        tm = self._ports.require_task_manager()
        spec = self._ports.normalize_spec(spec, trusted=trusted)
        await self._ports.require_task_ready(spec)
        if not trusted:
            self._ports.validate_request_metadata(spec.metadata)
        if not spec.session_id:
            session_id = new_id("session")
            spec = replace(spec, session_id=session_id)
        if self._ports.sessions is not None and spec.session_id:
            if await self._ports.sessions.get(spec.session_id) is None:
                await self._ports.sessions.create(
                    spec.session_id,
                    metadata={"origin": "service"},
                    principal_id=self._ports.config.cache_namespace,
                    project_id=getattr(spec.workspace, "id", None),
                )
        # Pre-built specs share submit's fail-closed readiness contract; this
        # closes ACP's provisional TaskSpec admission bypass.
        await self._reject_complex_readiness_gaps(spec)
        return await self.enqueue_spec(
            tm,
            spec,
            wait=wait,
            user_request=user_request,
            enqueue=enqueue,
        )

    async def enqueue_spec(
        self,
        task_manager: TaskManager,
        spec: TaskSpec,
        *,
        wait: bool,
        user_request: AgentRequest | None = None,
        enqueue: bool = True,
    ):
        if wait and not enqueue:
            raise ValueError("wait=True requires enqueue=True")
        intake_owner = (
            "scheduler" if not enqueue and spec.metadata.get("_occurrence") else "ordinary"
        )
        metadata = dict(spec.metadata)
        metadata.setdefault("_intake_owner", intake_owner)
        metadata.setdefault("_intake_phase", "task_created")
        spec = replace(spec, metadata=metadata)
        try:
            existing = await task_manager.get(spec.id)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.objective != spec.objective
                or existing.session_id != spec.session_id
                or existing.parent_task_id != spec.parent_task_id
            ):
                raise ValueError(f"task id {spec.id!r} already identifies different work")
            if (existing.metadata or {}).get("status") == TaskStatus.CREATED.value:
                await self._repair_created_intake(
                    existing, user_request=user_request, enqueue=enqueue
                )
                existing = await task_manager.get(spec.id)
                if wait:
                    await self.wait_for(existing.id)
            return existing
        created = await task_manager.create(spec)
        # Every task gets a durable causal root before it can run. Transport
        # callers provide the original request; internal/scheduled callers
        # use the TaskSpec objective. This prevents same-session tasks from
        # inheriting whichever unrelated turn happened to be most recent.
        await self._ports.record_canonical_user_turn(user_request or created, created)
        await self._mark_intake_phase(created.id, "canonical_user_turn_persisted")
        # Precompute the revisioned static context concurrently with worker
        # pickup (P1: precompute before first inference). The compile path
        # remains the sole authority — this only warms its cache, guarded by
        # the same revisions, so a stale warm entry is recomputed, never
        # trusted. Failure is swallowed: prefetch must never fail admission.
        self._ports.spawn_static_prefetch(created)
        if enqueue:
            await task_manager.enqueue(created.id)
            await self._mark_intake_phase(created.id, "enqueued")
        if wait:
            await self.wait_for(created.id)
        return created

    async def _mark_intake_phase(self, task_id: str, phase: str) -> None:
        store = self._ports.store_tasks
        update = getattr(store, "update_metadata", None)
        if callable(update):
            await update(
                str(task_id),
                {
                    "_intake_phase": phase,
                    "_intake_phase_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    async def _repair_created_intake(
        self,
        task: TaskSpec,
        *,
        user_request: AgentRequest | None,
        enqueue: bool,
    ) -> None:
        """Complete a previously interrupted ordinary intake idempotently."""
        metadata = dict(task.metadata or {})
        if metadata.get("_intake_owner") == "scheduler" or metadata.get("_occurrence"):
            # Scheduler reconciliation owns occurrence claims and must finish
            # its task/claim transition before generic intake can enqueue it.
            return
        try:
            await self._ports.record_canonical_user_turn(user_request or task, task)
            await self._mark_intake_phase(task.id, "canonical_user_turn_persisted")
            if enqueue:
                await self._ports.require_task_manager().enqueue(task.id)
                await self._mark_intake_phase(task.id, "enqueued")
        except Exception:
            # A CREATED row is authoritative. If its causal root cannot be
            # reconstructed or queued, make the operator-visible recovery
            # state explicit instead of leaving inert work behind.
            manager = self._ports.require_task_manager()
            await manager.transition(
                task.id,
                TaskStatus.RECOVERY_REQUIRED,
                reason="task intake recovery could not complete canonical persistence/enqueue",
            )
            raise

    async def reconcile_created_intake(self) -> dict[str, int]:
        """Repair ordinary CREATED tasks before workers begin claiming work."""
        manager = self._ports.require_task_manager()
        rows = await manager.list_by_status(TaskStatus.CREATED)
        recovered = 0
        quarantined = 0
        skipped = 0
        for task in rows:
            metadata = dict(task.metadata or {})
            if metadata.get("_intake_owner") == "scheduler" or metadata.get("_occurrence"):
                skipped += 1
                continue
            try:
                await self._repair_created_intake(task, user_request=None, enqueue=True)
            except Exception:
                quarantined += 1
                continue
            recovered += 1
        return {"recovered": recovered, "quarantined": quarantined, "skipped": skipped}

    async def run_task(self, task_id: str) -> TaskSpec:
        """Drive the NAMED task through the kernel synchronously.

        The named task is acquired by id and run by the kernel, so it does not
        race the background ``run_forever`` worker for the next claimed task.
        """
        worker = self._ports.require_worker()
        await worker.run_task(task_id)
        return await self.get_task(task_id)

    async def wait_for(self, task_id: str, *, timeout: float | None = None) -> TaskSpec:
        """Poll until the task reaches a terminal status (or timeout)."""
        import time

        deadline = time.monotonic() + (60.0 if timeout is None else max(0.0, float(timeout)))
        while True:
            task = await self.get_task(task_id)
            status = (task.metadata or {}).get("status")
            if status in {s.value for s in TERMINAL_STATUSES}:
                manager = self._ports.task_manager
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
                kernel = self._ports.kernel
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
        return await self._ports.require_task_manager().get(task_id)

    async def get_result(self, task_id: str):
        """Return the :class:`TaskResult` (or None if not yet finalised)."""
        mgr = self._ports.require_task_manager()
        return await mgr.get_result(task_id)

    async def stream_events(self, task_id: str, after_sequence: int = 0):
        """Yield a task's events, ordered by sequence (replayable log).

        Polls the event store while the task is still running so the stream is
        live: new events appended after the last yielded sequence are streamed
        out as they arrive. The generator stops once the task reaches a
        terminal status (and flushes any remaining events).
        """
        events = self._ports.require_events()
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
        events = self._ports.require_events()
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
