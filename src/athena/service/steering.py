"""Task steering admission and durable enqueue mechanism."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from athena.protocol.errors import ServiceNotReady
from athena.protocol.tasks import TaskStatus

__all__ = ["TaskSteeringService"]


class TaskSteeringService:
    """Authorize and enqueue operator/parent steering at a safe boundary."""

    def __init__(
        self,
        *,
        store: Any,
        task_manager: Any,
        get_status: Callable[[str], Any],
        worker: Callable[[], Any],
        principal_id: str,
    ) -> None:
        self._store = store
        self._task_manager = task_manager
        self._get_status = get_status
        self._worker = worker
        self._principal_id = principal_id

    def _resolve(self, value: Any) -> Any:
        return value() if callable(value) else value

    async def steer(
        self,
        task_id: str,
        text: str,
        *,
        principal_id: str | None = None,
        source_task_id: str | None = None,
    ) -> dict[str, Any]:
        store = self._resolve(self._store)
        manager = self._resolve(self._task_manager)
        if store is None or manager is None:
            raise ServiceNotReady("task steering is not initialized")
        task = await manager.get(str(task_id))
        if source_task_id is not None:
            source_id = str(source_task_id)
            try:
                source = await manager.get(source_id)
            except KeyError as exc:
                raise ValueError(f"source task {source_id!r} does not exist") from exc
            cursor = task
            related = False
            visited: set[str] = set()
            while cursor.parent_task_id and cursor.parent_task_id not in visited:
                visited.add(cursor.id)
                if cursor.parent_task_id == source.id:
                    related = True
                    break
                try:
                    cursor = await manager.get(cursor.parent_task_id)
                except KeyError:
                    break
            if not related:
                raise ValueError(
                    f"source task {source_id!r} is not an ancestor of task {task_id!r}"
                )
        status = await self._get_status(str(task_id))
        if status not in {
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
            TaskStatus.INTERRUPTED.value,
        }:
            raise ValueError(f"task {task_id!r} is not steerable in status {status}")
        record = await store.enqueue(
            str(task_id),
            text,
            principal_id=str(principal_id or self._principal_id),
            source_task_id=source_task_id,
            source="operator",
        )
        worker = self._resolve(self._worker)
        if worker is not None:
            worker.notify()
        return record
