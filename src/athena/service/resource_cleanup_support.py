"""Operator retry for durable task-resource cleanup."""

from __future__ import annotations

from typing import Any

from athena.protocol.errors import ServiceNotReady
from athena.protocol.tasks import TaskResult, TaskStatus

__all__ = ["ResourceCleanupSupport"]


class ResourceCleanupSupport:
    """Retry cleanup and commit a completed pending finalization when proven."""

    def __init__(
        self,
        *,
        finalizer: Any,
        task_manager: Any,
        pending_store: Any,
        startup_health: dict[str, Any],
    ) -> None:
        self._finalizer = finalizer
        self._task_manager = task_manager
        self._pending_store = pending_store
        self._startup_health = startup_health

    async def retry(self, task_id: str) -> dict[str, Any]:
        finalizer = self._finalizer
        manager = self._task_manager
        if finalizer is None or manager is None:
            raise ServiceNotReady("resource finalization is not initialized")
        task = await manager.get(str(task_id))
        pending_store = self._pending_store
        pending = await pending_store.get(str(task_id)) if pending_store is not None else None
        result = pending.result if pending is not None else await manager.get_result(str(task_id))
        if result is None:
            result = TaskResult(
                task_id=str(task_id),
                status=TaskStatus.RECOVERY_REQUIRED,
                summary="resource cleanup recovery",
            )
        await finalizer.retry(task, result)
        health = finalizer.health()
        unresolved = finalizer.unresolved_for_task(str(task_id))
        if pending is not None and not unresolved and not health.get("durability_error"):
            recovered = await manager.commit_pending_finalization(str(task_id))
            if recovered is not None:
                health = {
                    **health,
                    "pending_finalization": {
                        "status": "committed",
                        "result_status": recovered.status.value,
                    },
                }
        check = self._startup_health.get("checks", {}).get("resource_obligations")
        if isinstance(check, dict):
            check.update(
                {
                    "status": "ok" if health["unresolved_count"] == 0 else "degraded",
                    "blocking": health["unresolved_count"] > 0,
                    "unresolved_count": health["unresolved_count"],
                }
            )
        return health
