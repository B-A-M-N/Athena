"""Canonical execution-stream coordination owned by ``ExecutionManager``.

This collaborator normalizes backend/runtime events, adopts runtime sessions,
persists execution receipts, and emits observability events.  It is a
mechanism split, not a second execution authority: routing, cancellation,
session ownership, and durable receipt stores remain manager-owned.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, AsyncIterator

from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
    ExecutionRequest,
)
from athena.protocol.ids import new_id

if TYPE_CHECKING:
    from athena.execution.manager import ExecutionManager


class ExecutionStreamCoordinator:
    """Run one canonical execution stream through manager-owned mechanisms."""

    def __init__(self, manager: ExecutionManager) -> None:
        self._manager = manager

    async def stream(
        self,
        request: ExecutionRequest,
        execution_id: str | None = None,
    ) -> AsyncIterator[ExecutionEvent]:
        manager = self._manager
        execution_id = execution_id or new_id("exec")
        manager._cancellation_registry.executions[execution_id] = request.task_id
        selected_backend = manager._selected_backend(request.backend)
        runtime: Any = selected_backend or manager._resolve(request.runtime)
        backend_request = request
        if selected_backend is not None:
            backend_request = replace(
                request,
                metadata={**dict(request.metadata), "__execution_id": execution_id},
            )
        runtime_session_id: str | None = request.runtime_session_id
        if (
            runtime_session_id
            and runtime_session_id not in manager._cancellation_registry.runtime_by_session
        ):
            manager._cancellation_registry.runtime_by_session[runtime_session_id] = runtime
        exit_status: ExecutionExitStatus | None = None
        exit_code: int | None = None
        execution_metadata: dict[str, Any] = {}
        persisted = False
        await manager._persist_execution_start(
            execution_id,
            task_id=request.task_id,
            runtime_session_id=runtime_session_id,
            source=request.source,
            cwd=request.cwd,
            env=request.env,
        )
        await manager._emit_event(
            "ExecutionStarted",
            {
                "execution_id": execution_id,
                "task_id": request.task_id,
                "runtime": request.runtime,
                "backend": request.backend,
            },
            task_id=request.task_id,
        )
        try:
            if selected_backend is not None:
                event_stream = selected_backend.execute(backend_request)
            else:
                event_stream = runtime.execute(request, execution_id)
            async for event in event_stream:
                metadata = event.metadata or {}
                if event.type == ExecutionEventType.STARTED and metadata:
                    execution_metadata.update(dict(metadata))
                if metadata.get("runtime_session_id"):
                    if request.task_id not in manager._cancellation_registry.cancel_requested_tasks:
                        await manager._sessions.adopt_runtime_session(
                            runtime,
                            metadata["runtime_session_id"],
                            request.task_id,
                            backend=request.backend,
                            runtime_name=request.runtime,
                            cwd=request.cwd,
                            metadata=metadata,
                        )
                    manager._cancellation_registry.exec_runtimes[execution_id] = (
                        runtime,
                        metadata["runtime_session_id"],
                    )
                    if not persisted:
                        persisted = True
                        await manager._update_execution_runtime_session(
                            execution_id, metadata["runtime_session_id"]
                        )
                else:
                    manager._cancellation_registry.exec_runtimes[execution_id] = (
                        runtime,
                        runtime_session_id,
                    )
                if event.type == ExecutionEventType.STDOUT:
                    await manager._emit_event(
                        "StdoutChunk",
                        {"execution_id": execution_id, "data": event.data or ""},
                        task_id=request.task_id,
                    )
                elif event.type == ExecutionEventType.STDERR:
                    await manager._emit_event(
                        "StderrChunk",
                        {"execution_id": execution_id, "data": event.data or ""},
                        task_id=request.task_id,
                    )
                if event.type == ExecutionEventType.EXITED:
                    exit_status = event.exit_status
                    exit_code = event.exit_code
                yield event
            adopted = manager._cancellation_registry.exec_runtimes.get(execution_id)
            if (
                adopted
                and request.task_id not in manager._cancellation_registry.cancel_requested_tasks
            ):
                runtime, session_id = adopted
                if session_id:
                    await manager._sessions.adopt_runtime_session(
                        runtime,
                        session_id,
                        request.task_id,
                        backend=request.backend,
                        runtime_name=request.runtime,
                        cwd=request.cwd,
                        metadata=execution_metadata,
                    )
        finally:
            manager._cancellation_registry.executions.pop(execution_id, None)
            manager._cancellation_registry.exec_runtimes.pop(execution_id, None)
            if not any(
                owner == request.task_id
                for owner in manager._cancellation_registry.executions.values()
            ) and not manager._cancellation_registry.task_sessions.get(request.task_id):
                manager._cancellation_registry.cancel_requested_tasks.discard(request.task_id)
            tracked_exit = exit_status is not None
            final_status = exit_status if exit_status is not None else ExecutionExitStatus.FAILED
            await manager._persist_execution_finish(
                execution_id,
                task_id=request.task_id,
                runtime=request.runtime,
                backend=request.backend,
                cwd=request.cwd,
                exit_status=final_status,
                exit_code=exit_code,
                timed_out=(tracked_exit and exit_status == ExecutionExitStatus.TIMED_OUT),
                interrupted=(tracked_exit and exit_status == ExecutionExitStatus.INTERRUPTED),
                execution_metadata=execution_metadata,
            )
            exit_event_type = "ExecutionExited"
            if final_status == ExecutionExitStatus.TIMED_OUT:
                exit_event_type = "ExecutionTimedOut"
            elif final_status == ExecutionExitStatus.INTERRUPTED:
                exit_event_type = "ExecutionInterrupted"
            await manager._emit_event(
                exit_event_type,
                {
                    "execution_id": execution_id,
                    "task_id": request.task_id,
                    "runtime": request.runtime,
                    "exit_code": exit_code,
                    "exit_status": final_status.value,
                    **({"metadata": execution_metadata} if execution_metadata else {}),
                },
                task_id=request.task_id,
            )


__all__ = ["ExecutionStreamCoordinator"]
