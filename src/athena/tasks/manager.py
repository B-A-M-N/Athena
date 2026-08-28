from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from athena.protocol.errors import (
    IllegalStateTransition,
    RequestCancelled,
    TaskBudgetExceeded,
    TaskDeadlineExceeded,
    TaskError,
)
from athena.protocol.messages import utcnow
from athena.protocol.tasks import (
    ContextRef,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TERMINAL_STATUSES,
    UsageSummary,
)
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore

_logger = logging.getLogger("athena.tasks")

__all__ = [
    "TaskManager",
    "Task",
    "Decision",
    "TaskNotRunnable",
    "RequestCancelled",
]

Task = TaskSpec


def _deserialize(row: dict[str, Any]) -> TaskSpec:
    from athena.kernel.lifecycle import deserialize_task

    return deserialize_task(row)


@dataclass(frozen=True)
class Decision:
    """Literal stand-in for a termination decision (BUILDSPEC §18 finalize).

    The kernel's ``TerminationDecision`` is structurally interchangeable with
    this when feeding :meth:`TaskManager.finalize`.
    """

    terminal: bool = True
    reason: str = ""
    status: TaskStatus | None = None
    unresolved: tuple[str, ...] = ()
    summary: str = ""


class TaskNotRunnable(TaskError):
    code = "task_not_runnable"

    def __init__(self, task_id: str, status: TaskStatus, message: str = "") -> None:
        super().__init__(message or f"task {task_id} not runnable (status={status.value})")
        self.task_id = task_id
        self.status = status


class TaskManager:
    """Canonical task lifecycle owner (BUILDSPEC §15, §17-18).

    The manager is the single authority for task creation, status transitions,
    runnability, budget rollup, and result persistence. It never reasons or
    executes; it validates against the store-enforced table, persists, and
    emits events (BHV-014..024).
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        events: EventStore | None = None,
        sessions: SessionRepository | None = None,
        budgets: Any = None,
        cancellations: Any = None,
    ) -> None:
        self._store = task_store
        self._events = events
        self._sessions = sessions
        self._budgets = budgets
        self._cancellations = cancellations
        self._running_emitted: set[str] = set()
        # Optional post-finalization observers (knowledge pipeline). Each is an
        # async callable ``(task, result)`` invoked AFTER the terminal state is
        # durable; observer failures never affect the finalized result.
        self._finalize_observers: list[Any] = []
        # ``finalize_with_result`` makes the task terminal before observers
        # run. Keep a small per-task barrier so waiters do not return during
        # that visibility window and fork/cross-interface snapshots remain
        # stable.
        self._finalization_events: dict[str, asyncio.Event] = {}

    def add_finalize_observer(self, observer: Any) -> None:
        """Register an async ``(task, result)`` post-finalization hook."""
        self._finalize_observers.append(observer)

    def set_budget_tracker(self, budgets: Any) -> None:
        """Late-bind the budget authority (construction-order tolerant, §19)."""
        self._budgets = budgets

    def set_cancellation_manager(self, cancellations: Any) -> None:
        """Late-bind the cancellation authority (construction-order tolerant, §20)."""
        self._cancellations = cancellations

    @property
    def budgets(self) -> Any:
        return self._budgets

    @property
    def cancellations(self) -> Any:
        return self._cancellations

    # ------------------------------------------------------------------ #
    # Creation / intake (BHV-002, BHV-011..013)
    # ------------------------------------------------------------------ #
    async def create(self, spec: TaskSpec) -> Task:
        await self._ensure_session(spec)
        await self._store.insert_task(
            spec.id,
            spec.session_id,
            spec.parent_task_id,
            spec.objective,
            autonomy=_autonomy(spec),
            acceptance_criteria=spec.acceptance_criteria,
            context_refs=spec.context_refs,
            workspace=spec.workspace,
            capability_policy=spec.capability_policy,
            model_policy=spec.model_policy,
            resource_budget=spec.resource_budget,
            deadline=spec.deadline,
            delivery=spec.delivery,
            metadata=dict(spec.metadata),
            status=TaskStatus.CREATED,
        )
        if self._budgets is not None:
            self._budgets.register(spec)
        if self._cancellations is not None:
            self._cancellations.reset(spec.id)
        await self._emit(spec, TaskStatus.CREATED)
        return spec

    async def _ensure_session(self, spec: TaskSpec) -> None:
        if self._sessions is None or not spec.session_id:
            return
        existing = await self._sessions.get(spec.session_id)
        if existing is None:
            await self._sessions.create(spec.session_id)

    async def enqueue(self, task_id: str) -> Task:
        await self.transition(task_id, TaskStatus.QUEUED)
        return await self.get(task_id)

    async def get(self, task_id: str) -> Task:
        row = await self._store.get(task_id)
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return _deserialize(row)

    async def list_by_status(self, status: TaskStatus) -> list[Task]:
        rows = await self._store.list_by_status(status) or []
        return [_deserialize(r) for r in rows]

    async def list_by_session(self, session_id: str) -> list[Task]:
        rows = await self._store.list_by_session(session_id) or []
        return [_deserialize(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Acquisition / runnability (§17-18: acquire -> assert_runnable)
    # ------------------------------------------------------------------ #
    async def acquire(self, task_id: str) -> Task:
        # Backward-compatible acquisition for callers that don't use ownership.
        # Targeted runners (e.g. TaskWorker.run_task) should use the store's
        # acquire_with_ownership so a RUNNING task's lease/owner is respected
        # instead of being re-acquired blindly here.
        row = await self._store.get(task_id)
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        status = TaskStatus(row["status"])
        if status == TaskStatus.CREATED:
            await self._store.transition(task_id, TaskStatus.QUEUED)
            await self._emit(_deserialize(row), TaskStatus.QUEUED)
            status = TaskStatus.QUEUED
        if status == TaskStatus.QUEUED:
            await self._store.transition(task_id, TaskStatus.RUNNING)
            await self._emit(_deserialize(row), TaskStatus.RUNNING)
        elif status == TaskStatus.RUNNING:
            if task_id not in self._running_emitted:
                await self._emit(_deserialize(row), TaskStatus.RUNNING)
            self._running_emitted.add(task_id)
        elif status == TaskStatus.INTERRUPTED:
            await self._store.transition(task_id, TaskStatus.RUNNING)
            await self._emit(_deserialize(row), TaskStatus.RUNNING)
        elif status in TERMINAL_STATUSES:
            raise IllegalStateTransition(
                f"task {task_id} cannot be acquired (status={status.value})"
            )
        return await self.get(task_id)

    async def assert_runnable(self, task: Task | str) -> Task:
        task_id = task.id if isinstance(task, TaskSpec) else str(task)
        row = await self._store.get(task_id)
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        status = TaskStatus(row["status"])
        if status == TaskStatus.CANCELLED:
            raise RequestCancelled(f"task {task_id} cancelled")
        if status != TaskStatus.RUNNING:
            raise TaskNotRunnable(task_id, status)
        spec = _deserialize(row)
        if spec.deadline is not None and utcnow() >= spec.deadline:
            raise TaskDeadlineExceeded(f"task {task_id} passed its deadline")
        if self._budgets is not None:
            exhausted = await self._budgets.exhausted(task_id)
            if exhausted:
                raise TaskBudgetExceeded(f"task {task_id} resource budget exhausted")
        if self._cancellations is not None and self._cancellations.is_cancelled(task_id):
            raise RequestCancelled(f"task {task_id} cancelled")
        return spec

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #
    async def transition(self, task_id: str, to: TaskStatus, *, reason: str = "") -> None:
        await self._store.transition(task_id, to)
        spec = await self.get(task_id)
        await self._emit(spec, to)

    # ------------------------------------------------------------------ #
    # Finalization (§18; §72 TaskResult)
    # ------------------------------------------------------------------ #
    async def finalize(
        self,
        task: Task | str,
        response: Any = None,
        decision: Any | None = None,
        *,
        status: TaskStatus | None = None,
        reason: str | None = None,
        usage: UsageSummary | None = None,
        summary: str = "",
        evidence: tuple = (),
        artifacts: tuple = (),
        mutations: tuple = (),
        _allow_recovery_completion: bool = False,
    ) -> TaskResult:
        task_id = task.id if isinstance(task, TaskSpec) else str(task)
        resolved = await self.get(task_id)

        if status is None:
            status = getattr(decision, "status", None) or TaskStatus.COMPLETE
        if not reason:
            reason = getattr(decision, "reason", "") or "task finalised"
        if not summary:
            summary = getattr(decision, "summary", "") or reason

        if usage is None:
            usage = UsageSummary()
        result = TaskResult(
            task_id=task_id,
            status=status,
            summary=summary,
            evidence=tuple(evidence or ()),
            artifacts=tuple(artifacts or ()),
            mutations=tuple(mutations or ()),
            unresolved=tuple(getattr(decision, "unresolved", ()) or ()),
            usage=usage,
            created_at=utcnow(),
        )

        barrier = self._finalization_events.setdefault(task_id, asyncio.Event())

        # Status + result MUST land atomically (§86): do the transition and the
        # result persistence inside a single DB transaction so a crash cannot
        # leave a terminal task with no result. Events are append-only side
        # effects emitted after commit.
        try:
            await self._finalize_atomically(
                task_id,
                status,
                result,
                allow_recovery_completion=_allow_recovery_completion,
            )

            self._running_emitted.discard(task_id)

            await self._emit(resolved, status)

            if self._budgets is not None:
                self._budgets.consume_result(task_id, usage)
            if self._cancellations is not None:
                self._cancellations.reset(task_id)

            # Post-finalization knowledge pipeline (BUILDSPEC 64/68): observers see
            # the DURABLE result and may propose memory/skill candidates. Their
            # failures are logged, never propagated — finalization already landed.
            for observer in self._finalize_observers:
                try:
                    await observer(resolved, result)
                except Exception as exc:
                    _logger.warning(
                        "finalize observer %s failed for task %s: %s",
                        getattr(observer, "__name__", type(observer).__name__),
                        task_id,
                        exc,
                    )

            return result
        finally:
            barrier.set()

    async def wait_for_finalization(self, task_id: str, *, timeout: float | None = None) -> None:
        """Wait for current-process post-finalization observers, if any.

        A terminal task loaded from a previous process has no in-memory barrier
        and is already safe to observe. This makes restart and API callers
        compatible while closing the in-process terminal/observer race.
        """
        barrier = self._finalization_events.get(task_id)
        if barrier is None or barrier.is_set():
            return
        if timeout is None:
            await barrier.wait()
        else:
            await asyncio.wait_for(barrier.wait(), timeout=max(float(timeout), 0.0))

    async def _finalize_atomically(
        self,
        task_id: str,
        status: TaskStatus,
        result: TaskResult,
        *,
        allow_recovery_completion: bool = False,
    ) -> None:
        usage = {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "model_calls": result.usage.model_calls,
            "cost_usd": str(result.usage.cost_usd),
            "duration_ms": result.usage.duration_ms,
            "executions": result.usage.executions,
            "mutations": result.usage.mutations,
        }
        await self._store.finalize_with_result(
            task_id,
            status,
            result_status=result.status,
            summary=result.summary,
            evidence=[_ref_kv(e) for e in result.evidence],
            artifacts=[_art(e) for e in result.artifacts],
            mutations=[_mut(e) for e in result.mutations],
            unresolved=list(result.unresolved),
            usage=usage,
            allow_recovery_completion=allow_recovery_completion,
        )

    async def get_result(self, task_id: str) -> TaskResult | None:
        row = await self._store.get(task_id)
        if row is None:
            return None
        return _decode_result(row)

    async def apply_result(self, task_id: str, result: TaskResult) -> None:
        await self._persist_result(task_id, result)
        if self._budgets is not None:
            self._budgets.consume_result(task_id, result.usage)

    # ------------------------------------------------------------------ #
    # Persistence / events
    # ------------------------------------------------------------------ #
    async def _persist_result(self, task_id: str, result: TaskResult) -> None:
        raw = await self._store.get(task_id)
        if raw is None:
            return
        usage = {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "model_calls": result.usage.model_calls,
            "cost_usd": str(result.usage.cost_usd),
            "duration_ms": result.usage.duration_ms,
            "executions": result.usage.executions,
            "mutations": result.usage.mutations,
        }
        await self._store.persist_result(
            task_id,
            status=result.status,
            summary=result.summary,
            evidence=[_ref_kv(e) for e in result.evidence],
            artifacts=[_art(e) for e in result.artifacts],
            mutations=[_mut(e) for e in result.mutations],
            unresolved=list(result.unresolved),
            usage=usage,
        )

    async def _emit(self, task: Task, status: TaskStatus) -> None:
        if self._events is None:
            return
        payload: dict[str, Any] = {"status": status.value}
        # Scheduler event triggers are durable observations. Preserve the
        # bounded trigger envelope on the task lifecycle event so the task's
        # world-state view can explain what caused this maintenance run.
        trigger_event = (task.metadata or {}).get("_trigger_event")
        if isinstance(trigger_event, dict):
            payload["trigger_event"] = dict(trigger_event)
        await self._events.append_event(
            _event_type(status),
            payload,
            task_id=task.id,
            session_id=task.session_id,
        )


def _event_type(status: TaskStatus) -> str:
    return {
        TaskStatus.CREATED: "TaskCreated",
        TaskStatus.RUNNING: "TaskStarted",
        TaskStatus.WAITING_APPROVAL: "ApprovalRequested",
        TaskStatus.PARTIAL: "TaskPartial",
        TaskStatus.COMPLETE: "TaskCompleted",
        TaskStatus.FAILED: "TaskFailed",
        TaskStatus.CANCELLED: "TaskCancelled",
        TaskStatus.INTERRUPTED: "TaskInterrupted",
        TaskStatus.BLOCKED: "TaskBlocked",
        TaskStatus.QUEUED: "TaskQueued",
    }.get(status, "TaskStateChanged")


def _autonomy(spec: TaskSpec) -> str:
    meta = spec.metadata or {}
    val = meta.get("autonomy")
    if callable(val):
        try:
            return str(val)
        except Exception:
            return "supervised"
    return str(val or "supervised")


def _ref_kv(r: Any) -> dict:
    return {"kind": r.kind, "ref": r.ref, "source_id": r.source_id, "summary": r.summary}


def _art(a: Any) -> dict:
    return {"id": getattr(a, "id", ""), "uri": getattr(a, "uri", "")}


def _mut(m: Any) -> dict:
    return {
        "id": m.id,
        "resource": m.resource,
        "operation": m.operation,
        "reversible": m.reversible,
    }


def _decode_result(row: dict[str, Any]) -> TaskResult | None:
    status = row.get("result_status")
    if not status:
        current = row.get("status")
        status = current if current and current in TERMINAL_STATUSES else None
    if not status:
        return None
    import json as _json

    usage = (
        row["usage"]
        if isinstance(row.get("usage"), dict)
        else (_json.loads(row["usage"]) if row.get("usage") else {})
    )
    return TaskResult(
        task_id=row["id"],
        status=TaskStatus(status),
        summary=row.get("summary") or "",
        evidence=_decode_context_refs(row.get("evidence")),
        artifacts=_decode_artifact_refs(row.get("artifacts")),
        mutations=_decode_mutation_refs(row.get("mutations")),
        unresolved=tuple(row["unresolved"])
        if isinstance(row.get("unresolved"), (list, tuple))
        else (tuple(_json.loads(row["unresolved"])) if row.get("unresolved") else ()),
        usage=UsageSummary(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            model_calls=int(usage.get("model_calls", 0)),
            cost_usd=Decimal(str(usage.get("cost_usd", "0")) or "0"),
            duration_ms=int(usage.get("duration_ms", 0)),
            executions=int(usage.get("executions", 0)),
            mutations=int(usage.get("mutations", 0)),
        ),
    )


def _decode_context_refs(raw: Any) -> tuple[ContextRef, ...]:
    import json as _json

    if not raw:
        return ()
    try:
        items = raw if isinstance(raw, list) else _json.loads(raw)
    except ValueError:
        return ()
    return tuple(
        ContextRef(
            kind=i.get("kind", "session"),
            ref=i.get("ref", ""),
            source_id=i.get("source_id"),
            summary=i.get("summary"),
            mime_type=i.get("mime_type"),
        )
        for i in items
    )


def _decode_artifact_refs(raw: Any) -> tuple:
    from athena.protocol.artifacts import ArtifactRef
    import json as _json

    if not raw:
        return ()
    try:
        items = raw if isinstance(raw, list) else _json.loads(raw)
    except ValueError:
        return ()
    out = []
    for i in items:
        if isinstance(i, ArtifactRef):
            out.append(i)
        elif isinstance(i, dict):
            out.append(
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
            )
    return tuple(out)


def _decode_mutation_refs(raw: Any) -> tuple:
    from athena.protocol.tasks import MutationRef
    import json as _json

    if not raw:
        return ()
    try:
        items = raw if isinstance(raw, list) else _json.loads(raw)
    except ValueError:
        return ()
    return tuple(
        MutationRef(
            id=i.get("id", ""),
            resource=i.get("resource", ""),
            operation=i.get("operation", ""),
            reversible=bool(i.get("reversible", False)),
        )
        for i in items
    )
