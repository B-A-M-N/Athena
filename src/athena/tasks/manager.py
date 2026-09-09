from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
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
    Durability,
    FINAL_STATUSES,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TERMINAL_STATUSES,
    UsageSummary,
)
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.state.task_finalizations import (
    COMMITTED,
    OBSERVER_DONE,
    OBSERVER_FAILED,
    OBSERVER_RUNNING,
    QUIESCING,
    RECOVERY_REQUIRED,
    TaskFinalizationStore,
)

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
        admission: Any = None,
        principal_id: str | None = None,
        finalizations: TaskFinalizationStore | None = None,
        steering_store: Any = None,
    ) -> None:
        self._store = task_store
        self._events = events
        self._sessions = sessions
        self._budgets = budgets
        self._cancellations = cancellations
        self._admission = admission
        self._principal_id = principal_id
        self._finalizations = finalizations
        self._steering_store = steering_store
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
        self._finalization_barrier: Any = None
        self._wakeup_callback: Any = None

    def add_finalize_observer(self, observer: Any) -> None:
        """Register an async ``(task, result)`` post-finalization hook."""
        self._finalize_observers.append(observer)

    def set_budget_tracker(self, budgets: Any) -> None:
        """Late-bind the budget authority (construction-order tolerant, §19)."""
        self._budgets = budgets

    def set_cancellation_manager(self, cancellations: Any) -> None:
        """Late-bind the cancellation authority (construction-order tolerant, §20)."""
        self._cancellations = cancellations

    def set_admission(self, admission: Any) -> None:
        """Late-bind the service-owned task admission predicate."""
        self._admission = admission

    def set_wakeup_callback(self, callback: Any) -> None:
        """Bind the local worker wakeup without making it task authority."""
        self._wakeup_callback = callback

    def set_finalization_barrier(self, callback: Any) -> None:
        """Bind the pre-publication resource quiescence authority."""
        self._finalization_barrier = callback

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
        # Admission must precede session allocation and the durable Task row.
        # This is the canonical boundary shared by API, scheduler, delegation,
        # ACP, self-host, and future Task producers.
        if self._admission is not None:
            result = self._admission(spec)
            if inspect.isawaitable(result):
                await result
        existing = await self._store.get(spec.id)
        if existing is not None:
            current = _deserialize(existing)
            if (
                current.objective != spec.objective
                or current.session_id != spec.session_id
                or current.parent_task_id != spec.parent_task_id
            ):
                raise ValueError(f"task id {spec.id!r} already identifies different work")
            return current
        await self._ensure_session(spec)
        # AUTHORITY (Durability.AUTHORITY): the durable task row is the single
        # source of truth for the task's existence and state. It commits first
        # and is allowed to surface a real error (the task was NOT admitted).
        # Anything after it is BOOKKEEPING and must never roll back or mask
        # the authority commit (durability split, P1-27): a budget/cancellation
        # registration or event-emit failure cannot surface as a failed
        # ``create`` for a task that was actually admitted.
        try:
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
        except sqlite3.IntegrityError:
            existing = await self._store.get(spec.id)
            if existing is None:
                raise
            current = _deserialize(existing)
            if (
                current.objective != spec.objective
                or current.session_id != spec.session_id
                or current.parent_task_id != spec.parent_task_id
            ):
                raise ValueError(f"task id {spec.id!r} already identifies different work")
            return current

        # ---- BOOKKEEPING (Durability.BOOKKEEPING), after the commit ------ #
        # Derived in-memory state (budget ledger, cancellation reset) and the
        # lifecycle event. Not authority: if one fails, the task still exists
        # and is runnable. Failures are logged and non-fatal, matching
        # ``_finalize_observers`` semantics.
        await self._bookkeeping(
            spec.id,
            Durability.BOOKKEEPING,
            "bookkeeping registration",
            self._register_bookkeeping,
            spec,
        )
        await self._bookkeeping(
            spec.id,
            Durability.BOOKKEEPING,
            "CREATED event emit",
            self._emit_created,
            spec,
        )
        return spec

    def _register_bookkeeping(self, spec: TaskSpec) -> None:
        if self._budgets is not None:
            self._budgets.register(spec)
        if self._cancellations is not None:
            self._cancellations.reset(spec.id)

    async def _emit_created(self, spec: TaskSpec) -> None:
        await self._emit(spec, TaskStatus.CREATED)

    async def _bookkeeping(self, task_id: str, durability: Durability, what: str, op, *args):
        """Run a deferred write under its declared Durability contract.

        The classification is what makes the split mechanical: a BOOKKEEPING
        failure is logged and swallowed (the authority row already committed);
        anything else — an AUTHORITY-classified write routed here by mistake,
        or a future Durability member — is re-raised, so the contract cannot
        silently erode.
        """
        try:
            result = op(*args)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            if durability is not Durability.BOOKKEEPING:
                raise
            _logger.warning(
                "task %s committed but %s failed (non-fatal): %s",
                task_id,
                what,
                exc,
            )

    async def _ensure_session(self, spec: TaskSpec) -> None:
        if self._sessions is None or not spec.session_id:
            return
        existing = await self._sessions.get(spec.session_id)
        if existing is None:
            parent_session_id = None
            if spec.parent_task_id:
                parent = await self._store.get(spec.parent_task_id)
                if parent is not None:
                    parent_session_id = parent.get("session_id")
            await self._sessions.create(
                spec.session_id,
                parent_id=parent_session_id,
                principal_id=self._principal_id,
                project_id=getattr(spec.workspace, "id", None),
            )

    async def enqueue(self, task_id: str) -> Task:
        await self.transition(task_id, TaskStatus.QUEUED)
        callback = self._wakeup_callback
        if callback is not None:
            try:
                outcome = callback()
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception as exc:
                _logger.warning("worker wakeup failed after enqueue: %s", exc)
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

    async def required_child_state(
        self, parent_task_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return (pending, failed) required direct children.

        Detached children have their own lifecycle and never hold the parent
        completion gate. A required child must reach COMPLETE, not merely a
        terminal status such as PARTIAL.
        """
        rows = await self._store.list_children(parent_task_id)
        pending: list[str] = []
        failed: list[str] = []
        for child in [_deserialize(row) for row in rows or []]:
            workspace = child.workspace
            if workspace is not None and (
                not workspace.required_child
                or str(workspace.delegate_mode or "").upper() == "DETACHED"
            ):
                continue
            status = TaskStatus((child.metadata or {}).get("status", TaskStatus.CREATED.value))
            if status is TaskStatus.COMPLETE:
                continue
            if status in TERMINAL_STATUSES:
                failed.append(f"child:{child.id}:{status.value}")
            else:
                pending.append(f"child:{child.id}:{status.value}")
        return tuple(pending), tuple(failed)

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
        await self._emit(spec, to, reason=reason)

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

        # Write the complete intended result before attempting resource
        # quiescence. If the process dies in the barrier, recovery must have
        # the original terminal status and payload — not just a marker saying
        # that cleanup was uncertain.
        if self._finalizations is not None and status in FINAL_STATUSES:
            await self._finalizations.prepare(result)
            await self._finalizations.set_phase(task_id, QUIESCING)

        # Resource ownership is part of the completion claim. Do not publish
        # COMPLETE/PARTIAL/FAILED/CANCELLED while a task-owned process or
        # session still lacks a durable close proof. The barrier parks the
        # task in RECOVERY_REQUIRED, which is resumable and visible to the
        # operator, instead of manufacturing a terminal success.
        if self._finalization_barrier is not None:
            try:
                barrier_result = self._finalization_barrier(resolved, result)
                if inspect.isawaitable(barrier_result):
                    barrier_result = await barrier_result
            except Exception as exc:  # fail closed into recoverable state
                _logger.warning("task %s finalization barrier failed: %s", task_id, exc)
                barrier_result = {
                    "confirmed": False,
                    "unresolved": [{"resource_id": "unknown", "error": str(exc)}],
                    "failures": [{"error": str(exc)}],
                }
            if isinstance(barrier_result, dict) and not barrier_result.get("confirmed", False):
                unresolved = tuple(
                    str(
                        item.get("resource_id")
                        or item.get("resource_type")
                        or item.get("error")
                        or "resource"
                    )
                    if isinstance(item, dict)
                    else str(item)
                    for item in barrier_result.get("unresolved", ())
                )
                blocked_summary = (
                    "terminal result held: task-owned resource cleanup requires recovery"
                )
                blocked = TaskResult(
                    task_id=task_id,
                    status=TaskStatus.RECOVERY_REQUIRED,
                    summary=blocked_summary,
                    unresolved=unresolved or ("resource_cleanup",),
                    usage=usage,
                    created_at=utcnow(),
                )
                if self._finalizations is not None:
                    await self._finalizations.set_phase(
                        task_id,
                        RECOVERY_REQUIRED,
                        error=blocked_summary,
                    )
                marker_store = getattr(self._store, "record_recovery_marker", None)
                if marker_store is not None:
                    await marker_store(
                        task_id,
                        {
                            "kind": "task_finalization_quiescence",
                            "intended_status": status.value,
                            "summary": summary,
                            "failures": list(barrier_result.get("failures", ())),
                            "unresolved": list(barrier_result.get("unresolved", ())),
                            "recorded_at": utcnow().isoformat(),
                        },
                    )
                await self._finalize_atomically(task_id, TaskStatus.RECOVERY_REQUIRED, blocked)
                self._running_emitted.discard(task_id)
                await self._emit(
                    resolved,
                    TaskStatus.RECOVERY_REQUIRED,
                    reason=blocked_summary,
                )
                barrier.set()
                return blocked

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
                commit_pending=self._finalizations is not None,
            )
            await self._post_commit(resolved, result)
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
        recovery_finalization: bool = False,
        commit_pending: bool = False,
    ) -> None:
        usage = {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "model_calls": result.usage.model_calls,
            "cost_usd": (str(result.usage.cost_usd) if result.usage.cost_known else None),
            "cost_known": result.usage.cost_known,
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
            recovery_finalization=recovery_finalization,
            commit_pending=commit_pending,
        )

    async def _post_commit(self, task: Task, result: TaskResult) -> None:
        """Publish a committed result and drain durable observers."""
        task_id = result.task_id
        if result.status in FINAL_STATUSES and self._steering_store is not None:
            try:
                await self._steering_store.mark_missed(task_id)
            except Exception as exc:
                _logger.warning("could not mark pending steering missed for %s: %s", task_id, exc)
        self._running_emitted.discard(task_id)
        await self._emit(task, result.status)

        if self._budgets is not None:
            release = getattr(self._budgets, "release_model_cost", None)
            if release is not None:
                await release(task_id)
            self._budgets.consume_result(task_id, result.usage)
            persist_budget = getattr(self._budgets, "_persist_usage", None)
            if persist_budget is not None:
                await persist_budget(task_id)
        if self._cancellations is not None:
            self._cancellations.reset(task_id)

        pending = await self._finalizations.get(task_id) if self._finalizations else None
        observer_state = dict(pending.observer_state) if pending is not None else {}
        observer_failed = False
        for index, observer in enumerate(self._finalize_observers):
            key = _observer_key(observer, index)
            if observer_state.get(key) == OBSERVER_DONE:
                continue
            if self._finalizations is not None:
                await self._finalizations.set_observer_state(task_id, key, OBSERVER_RUNNING)
            current_observer_failed = False
            try:
                await observer(task, result)
            except Exception as exc:
                # Preserve failure isolation for the terminal task, but keep
                # the observer pending so a restart can replay the durable
                # bookkeeping/projection.  A process crash while RUNNING is
                # likewise retried on startup.
                current_observer_failed = True
                observer_failed = True
                _logger.warning(
                    "finalize observer %s failed for task %s: %s",
                    getattr(observer, "__name__", type(observer).__name__),
                    task_id,
                    exc,
                )
                if self._finalizations is not None:
                    await self._finalizations.set_observer_state(
                        task_id, key, OBSERVER_FAILED, error=str(exc)
                    )
            finally:
                if self._finalizations is not None and not current_observer_failed:
                    await self._finalizations.set_observer_state(task_id, key, OBSERVER_DONE)
        if self._finalizations is not None and not observer_failed:
            await self._finalizations.delete(task_id)

    async def commit_pending_finalization(self, task_id: str) -> TaskResult | None:
        """Commit the exact result retained by the finalization write-ahead."""
        if self._finalizations is None:
            return None
        pending = await self._finalizations.get(task_id)
        if pending is None:
            return None
        task = await self.get(task_id)
        raw = await self._store.get(task_id)
        current = TaskStatus(raw["status"]) if raw is not None else None
        if current in FINAL_STATUSES:
            await self._post_commit(task, pending.result)
            return pending.result
        if current is not TaskStatus.RECOVERY_REQUIRED:
            return None
        await self._finalize_atomically(
            task_id,
            pending.intended_status,
            pending.result,
            recovery_finalization=True,
            commit_pending=True,
        )
        await self._post_commit(task, pending.result)
        return pending.result

    async def reconcile_pending_finalizations(self, resource_finalizer: Any = None) -> int:
        """Recover pending terminal claims before workers are allowed to run."""
        if self._finalizations is None:
            return 0
        recovered = 0
        for pending in await self._finalizations.list_recoverable():
            raw = await self._store.get(pending.task_id)
            if raw is None:
                await self._finalizations.delete(pending.task_id)
                continue
            current = TaskStatus(raw["status"])
            if current in FINAL_STATUSES or pending.phase == COMMITTED:
                if pending.phase != COMMITTED:
                    await self._finalizations.set_phase(pending.task_id, COMMITTED)
                committed = await self.commit_pending_finalization(pending.task_id)
                if committed is not None and await self._finalizations.get(pending.task_id) is None:
                    recovered += 1
                continue
            if current is not TaskStatus.RECOVERY_REQUIRED:
                await self._store.transition(pending.task_id, TaskStatus.RECOVERY_REQUIRED)
                await self._emit(
                    await self.get(pending.task_id),
                    TaskStatus.RECOVERY_REQUIRED,
                    reason="pending terminal result requires resource recovery",
                )
            if resource_finalizer is None:
                continue
            task = await self.get(pending.task_id)
            await resource_finalizer.retry(task, pending.result)
            health = resource_finalizer.health()
            unresolved = getattr(resource_finalizer, "unresolved_for_task", None)
            task_unresolved = (
                unresolved(pending.task_id)
                if callable(unresolved)
                else [
                    item
                    for item in health.get("unresolved", ())
                    if item.get("task_id") == pending.task_id
                ]
            )
            if task_unresolved or health.get("durability_error"):
                continue
            committed = await self.commit_pending_finalization(pending.task_id)
            if committed is not None and await self._finalizations.get(pending.task_id) is None:
                recovered += 1
        return recovered

    async def get_result(self, task_id: str) -> TaskResult | None:
        row = await self._store.get(task_id)
        if row is None:
            return None
        return _decode_result(row)

    async def apply_result(self, task_id: str, result: TaskResult) -> None:
        await self._persist_result(task_id, result)
        if self._budgets is not None:
            release = getattr(self._budgets, "release_model_cost", None)
            if release is not None:
                await release(task_id)
            self._budgets.consume_result(task_id, result.usage)
            persist_budget = getattr(self._budgets, "_persist_usage", None)
            if persist_budget is not None:
                await persist_budget(task_id)

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
            "cost_usd": (str(result.usage.cost_usd) if result.usage.cost_known else None),
            "cost_known": result.usage.cost_known,
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

    async def _emit(self, task: Task, status: TaskStatus, *, reason: str = "") -> None:
        if self._events is None:
            return
        payload: dict[str, Any] = {"status": status.value}
        if reason:
            payload["reason"] = reason
        mission_plan = (task.metadata or {}).get("_athena_mission_plan")
        if isinstance(mission_plan, dict) and mission_plan.get("phase"):
            # Self-host phase is durable mission state, not a renderer guess.
            # Repeating it on lifecycle events lets every projection recover
            # the operator-visible phase after a restart or replay.
            payload["self_host_phase"] = str(mission_plan["phase"])
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
            id=(f"task-lifecycle:{task.id}:{status.value}" if status in FINAL_STATUSES else None),
        )
        # Child lifecycle events are emitted on the parent's stream as well
        # as the child's own TaskCreated/TaskCompleted stream.  This keeps
        # delegation observable in production projections instead of relying
        # on test-only manufactured events.
        if task.parent_task_id and status == TaskStatus.CREATED:
            child_payload = _child_lifecycle_payload(task, status)
            await self._events.append_event(
                "ChildTaskCreated",
                child_payload,
                task_id=task.parent_task_id,
                session_id=task.session_id,
            )
        elif task.parent_task_id and status in TERMINAL_STATUSES:
            child_payload = _child_lifecycle_payload(task, status)
            await self._events.append_event(
                "ChildTaskCompleted",
                child_payload,
                task_id=task.parent_task_id,
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
        TaskStatus.RECOVERY_REQUIRED: "TaskRecoveryRequired",
    }.get(status, "TaskStateChanged")


def _child_lifecycle_payload(task: TaskSpec, status: TaskStatus) -> dict[str, str | None]:
    """Build the bounded cross-stream child lifecycle contract."""
    objective = str(task.objective or "")
    return {
        "child_task_id": task.id,
        "parent_task_id": task.parent_task_id,
        "session_id": task.session_id,
        "status": status.value,
        "objective": objective[:512] + ("…" if len(objective) > 512 else ""),
    }


def _autonomy(spec: TaskSpec) -> str:
    meta = spec.metadata or {}
    val = meta.get("autonomy")
    if callable(val):
        try:
            return str(val)
        except Exception:
            return "supervised"
    return str(val or "supervised")


def _observer_key(observer: Any, index: int) -> str:
    owner = getattr(observer, "__self__", None)
    module = getattr(owner, "__module__", None) or getattr(observer, "__module__", "")
    qualname = getattr(observer, "__qualname__", None) or getattr(
        observer, "__name__", type(observer).__name__
    )
    return f"{module}:{qualname}:{index}"


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
            cost_usd=(
                Decimal(str(usage["cost_usd"]))
                if usage.get("cost_usd") is not None
                else Decimal("0")
            ),
            cost_known=bool(usage.get("cost_known", usage.get("cost_usd") is not None)),
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
