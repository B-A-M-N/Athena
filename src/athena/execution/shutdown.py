"""Execution resource shutdown coordination."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from athena.execution.cancellation import CancellationRegistry
from athena.execution.routing import ExecutionRouting

_logger = logging.getLogger("athena.execution")


class ExecutionShutdownCoordinator:
    """Close task-owned, runtime, and backend resources in one bounded pass."""

    def __init__(
        self,
        *,
        registry: CancellationRegistry,
        routing: ExecutionRouting,
        cancel_task: Callable[[str], Awaitable[Any]],
    ) -> None:
        self._registry = registry
        self._routing = routing
        self._cancel_task = cancel_task

    async def close_all(self) -> dict[str, Any]:
        """Shut down every execution resource and retain failures in the result."""
        outcome: dict[str, Any] = {
            "tasks_cancelled": 0,
            "sessions_remaining": [],
            "runtime_failures": [],
            "backend_failures": [],
            "unproven_process_kills": [],
        }
        for task_id in set(self._registry.task_sessions) | set(
            self._registry.pending_runtime_cancellations
        ):
            try:
                result = await self._cancel_task(task_id)
                outcome["tasks_cancelled"] += 1
                if result.unproven_process_kills:
                    outcome["unproven_process_kills"].extend(
                        {"task_id": task_id, **kill} for kill in result.unproven_process_kills
                    )
            except Exception as exc:  # rationale: shutdown must preserve task state
                outcome["runtime_failures"].append({"task_id": task_id, "error": str(exc)})
                _logger.warning("task %s runtime cancellation failed: %s", task_id, exc)

        for runtime in set(self._routing.runtimes.values()):
            close_all = getattr(runtime, "close_all", None)
            if close_all is None:
                continue
            try:
                if asyncio.iscoroutinefunction(close_all):
                    await close_all()
                else:
                    close_all()
            except Exception as exc:  # rationale: shutdown must preserve task state
                outcome["runtime_failures"].append(
                    {"runtime": type(runtime).__name__, "error": str(exc)}
                )
                _logger.warning("runtime %s close_all failed: %s", type(runtime).__name__, exc)

        backends = list(self._routing.backends.values())
        if self._routing.local_backend is not None:
            backends.append(self._routing.local_backend)
        for backend in backends:
            shutdown = getattr(backend, "shutdown", None)
            if shutdown is None:
                continue
            try:
                if asyncio.iscoroutinefunction(shutdown):
                    await shutdown()
                else:
                    shutdown()
            except Exception as exc:  # rationale: shutdown must preserve task state
                outcome["backend_failures"].append(
                    {"backend": getattr(backend, "name", "?"), "error": str(exc)}
                )
                _logger.warning(
                    "backend %s shutdown failed: %s", getattr(backend, "name", "?"), exc
                )

        outcome["sessions_remaining"] = sorted(
            {
                session_id
                for sessions in self._registry.task_sessions.values()
                for _runtime, session_id in sessions
            }
        )
        return outcome

    def live_resource_count(self) -> int:
        """Count resources that must be gone after :meth:`close_all`."""
        live_sessions = sum(len(sessions) for sessions in self._registry.task_sessions.values())
        return (
            live_sessions
            + len(self._registry.pending_runtime_cancellations)
            + len(self._registry.exec_runtimes)
        )


__all__ = ["ExecutionShutdownCoordinator"]
