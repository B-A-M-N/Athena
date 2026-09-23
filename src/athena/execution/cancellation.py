"""Execution-owned session and cancellation reconciliation state.

The :class:`ExecutionManager` remains the single execution authority.  This
module owns the mutable execution/session indexes and the close/escalation
reconciliation used by that authority so runtime lifecycle state does not
become another responsibility of the manager coordinator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from athena.concurrency import run_blocking
from athena.execution.process_tree import process_group_id, process_start_identity

_logger = logging.getLogger("athena.execution.cancellation")

PersistSessionClosed = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class RuntimeCancellationResult:
    """Durable evidence returned by execution cancellation."""

    task_id: str
    closed_sessions: tuple[str, ...] = ()
    remaining_sessions: tuple[str, ...] = ()
    pending_runtime_cancellations: tuple[str, ...] = ()
    unproven_process_kills: tuple[dict[str, Any], ...] = ()

    @property
    def confirmed(self) -> bool:
        return (
            not self.remaining_sessions
            and not self.pending_runtime_cancellations
            and not self.unproven_process_kills
        )


class CancellationRegistry:
    """Own execution/session indexes and retryable cancellation state."""

    def __init__(self) -> None:
        self.executions: dict[str, str] = {}
        self.exec_runtimes: dict[str, tuple[Any, str | None]] = {}
        self.task_sessions: dict[str, list[tuple[Any, str]]] = {}
        self.runtime_by_session: dict[str, Any] = {}
        # The runtime object and task-session list are cleanup indexes, not
        # authorization facts. Keep ownership explicit at the same boundary
        # that records every live/adopted session.
        self.session_owners: dict[str, str] = {}
        self.pending_session_persistence: dict[str, tuple[str, Any]] = {}
        self.pending_runtime_cancellations: dict[str, list[Any]] = {}
        self.confirmed_runtime_cancellations: dict[str, set[int]] = {}
        self.cancel_requested_tasks: set[str] = set()

    def has_live_runtime(self, task_id: str) -> bool:
        return (
            bool(self.task_sessions.get(task_id))
            or any(owner == task_id for owner in self.executions.values())
            or bool(self.pending_runtime_cancellations.get(task_id))
        )

    async def destroy_session(
        self,
        runtime_session_id: str,
        *,
        persist_session_closed: PersistSessionClosed,
    ) -> None:
        """Close one session and retain persistence work for retry."""
        pending_persistence = self.pending_session_persistence.get(runtime_session_id)
        runtime = self.runtime_by_session.get(runtime_session_id)
        owner_task_id: str | None = pending_persistence[0] if pending_persistence else None
        if runtime is None and pending_persistence is not None:
            runtime = pending_persistence[1]
        if runtime is None:
            for task_id, sessions in self.task_sessions.items():
                for candidate, sid in sessions:
                    if sid == runtime_session_id:
                        runtime = candidate
                        owner_task_id = task_id
                        break
                if runtime is not None:
                    break
        if runtime is None:
            return
        close = getattr(runtime, "close", None) or getattr(runtime, "destroy_session", None)
        if pending_persistence is None and close is not None:
            if asyncio.iscoroutinefunction(close):
                await close(runtime_session_id)
            else:
                await run_blocking(close, runtime_session_id, _pool="long")
        try:
            await persist_session_closed(runtime_session_id)
        except Exception:  # rationale: retain durable close work without re-closing runtime
            self.pending_session_persistence[runtime_session_id] = (
                owner_task_id or "",
                runtime,
            )
            raise
        self.runtime_by_session.pop(runtime_session_id, None)
        self.session_owners.pop(runtime_session_id, None)
        for task_id, sessions in list(self.task_sessions.items()):
            remaining = [
                (candidate, sid) for candidate, sid in sessions if sid != runtime_session_id
            ]
            if remaining:
                self.task_sessions[task_id] = remaining
            else:
                self.task_sessions.pop(task_id, None)
        if owner_task_id is not None:
            self.pending_session_persistence.pop(runtime_session_id, None)
            if not self.task_sessions.get(
                owner_task_id
            ) and not self.pending_runtime_cancellations.get(owner_task_id):
                self.confirmed_runtime_cancellations.pop(owner_task_id, None)

    async def cancel_task(
        self,
        task_id: str,
        *,
        persist_session_closed: PersistSessionClosed,
    ) -> RuntimeCancellationResult:
        """Interrupt/close every runtime session owned by a task."""
        self.cancel_requested_tasks.add(task_id)
        rooms = list(self.task_sessions.get(task_id, []))
        # Include sessions adopted by executions that have not emitted their
        # final event yet.  Cancellation and late runtime identity adoption
        # therefore share one ownership index.
        seen: set[tuple[int, str]] = {(id(runtime), sid) for runtime, sid in rooms}
        for execution_id, (runtime, sid) in list(self.exec_runtimes.items()):
            if self.executions.get(execution_id) != task_id:
                continue
            if sid and (id(runtime), sid) not in seen:
                rooms.append((runtime, sid))
                seen.add((id(runtime), sid))

        owned_runtimes: list[Any] = []
        owned_runtime_ids: set[int] = set()
        for runtime, _sid in rooms:
            if id(runtime) not in owned_runtime_ids:
                owned_runtime_ids.add(id(runtime))
                owned_runtimes.append(runtime)
        for runtime in self.pending_runtime_cancellations.get(task_id, []):
            if id(runtime) not in owned_runtime_ids:
                owned_runtime_ids.add(id(runtime))
                owned_runtimes.append(runtime)

        remaining: list[tuple[Any, str]] = []
        errors: list[BaseException] = []
        closed_sessions: list[str] = []
        unproven_kills: list[dict[str, Any]] = []
        for runtime, sid in rooms:
            close = getattr(runtime, "close", None) or getattr(runtime, "destroy_session", None)
            try:
                if sid in self.pending_session_persistence:
                    await persist_session_closed(sid)
                    self.pending_session_persistence.pop(sid, None)
                else:
                    if close is not None:
                        session = getattr(runtime, "_sessions", {}).get(sid)
                        process_before = getattr(session, "process", None)
                        if asyncio.iscoroutinefunction(close):
                            await close(sid)
                        else:
                            await run_blocking(close, sid, _pool="long")
                        if (
                            process_before is not None
                            and getattr(process_before, "poll", None) is not None
                            and process_before.poll() is None
                        ):
                            pid = getattr(process_before, "pid", None)
                            unproven_kills.append(
                                {
                                    "session_id": sid,
                                    "pid": pid,
                                    "process_start_identity": (
                                        process_start_identity(pid) if pid is not None else None
                                    ),
                                    "pgid": process_group_id(process_before),
                                }
                            )
                    try:
                        await persist_session_closed(sid)
                    except Exception:  # rationale: retain durable close work for retry
                        # The runtime is already closed; only the durable
                        # close obligation remains for a later retry.
                        self.pending_session_persistence[sid] = (task_id, runtime)
                        raise
            except Exception as exc:  # rationale: retain failed session work for cancellation retry
                remaining.append((runtime, sid))
                errors.append(exc)
                continue
            self.runtime_by_session.pop(sid, None)
            self.session_owners.pop(sid, None)
            closed_sessions.append(sid)

        if remaining:
            self.task_sessions[task_id] = remaining
        else:
            self.task_sessions.pop(task_id, None)

        pending_runtime_ids: set[int] = set()
        for runtime in owned_runtimes:
            cancel = getattr(runtime, "cancel_task", None)
            if cancel is None or id(runtime) in self.confirmed_runtime_cancellations.get(
                task_id, set()
            ):
                continue
            try:
                if asyncio.iscoroutinefunction(cancel):
                    await cancel(task_id)
                else:
                    await run_blocking(cancel, task_id, _pool="long")
                self.confirmed_runtime_cancellations.setdefault(task_id, set()).add(id(runtime))
            except Exception as exc:  # rationale: retain failed runtime cancellation for retry
                errors.append(exc)
                pending_runtime_ids.add(id(runtime))

        if pending_runtime_ids:
            self.pending_runtime_cancellations[task_id] = [
                runtime for runtime in owned_runtimes if id(runtime) in pending_runtime_ids
            ]
        else:
            self.pending_runtime_cancellations.pop(task_id, None)
        if errors:
            raise RuntimeError(
                f"runtime cancellation failed for task {task_id}: "
                + "; ".join(str(error) for error in errors)
            ) from errors[0]
        self.confirmed_runtime_cancellations.pop(task_id, None)
        for entry in unproven_kills:
            _logger.warning(
                "process tree for session %s (pid %s) could not be proven dead after close",
                entry["session_id"],
                entry["pid"],
            )
        if not any(owner == task_id for owner in self.executions.values()):
            self.cancel_requested_tasks.discard(task_id)
        return RuntimeCancellationResult(
            task_id=task_id,
            closed_sessions=tuple(closed_sessions),
            remaining_sessions=tuple(sid for _runtime, sid in remaining),
            pending_runtime_cancellations=tuple(
                type(runtime).__name__
                for runtime in self.pending_runtime_cancellations.get(task_id, [])
            ),
            unproven_process_kills=tuple(unproven_kills),
        )


__all__ = ["CancellationRegistry", "RuntimeCancellationResult"]
