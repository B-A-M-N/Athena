"""Durable operator/model steering queue."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow


class SteeringSource(StrEnum):
    OPERATOR = "operator"
    PARENT_TASK = "parent_task"
    SYSTEM = "system"


class TaskSteeringStore:
    QUEUED = "QUEUED"
    DELIVERED = "DELIVERED"
    MISSED = "MISSED"
    REJECTED = "REJECTED"

    def __init__(self, db: Any) -> None:
        self._db = db

    async def enqueue(
        self,
        task_id: str,
        text: str,
        *,
        principal_id: str,
        source_task_id: str | None = None,
        source: SteeringSource | str = SteeringSource.OPERATOR,
    ) -> dict[str, Any]:
        value = str(text).strip()
        if not value or len(value) > 12_000:
            raise ValueError("steering text must contain 1-12000 characters")
        record = {
            "id": new_id("steer"),
            "task_id": str(task_id),
            "source_task_id": str(source_task_id) if source_task_id else None,
            "source": SteeringSource(str(source)).value,
            "principal_id": str(principal_id),
            "text": value,
            "status": self.QUEUED,
            "created_at": utcnow().isoformat(),
            "consumed_at": None,
        }
        await self._db.execute(
            "INSERT INTO task_steers(id, task_id, source_task_id, source, principal_id, text, "
            "status, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(record.values()),
        )
        return record

    async def list_pending(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM task_steers WHERE task_id = ? AND status = 'QUEUED' "
            "ORDER BY created_at, id",
            (str(task_id),),
        )
        return [dict(row) for row in rows]

    async def mark_consumed(self, steer_id: str) -> bool:
        cursor = await self._db.execute(
            "UPDATE task_steers SET status = 'DELIVERED', consumed_at = ? "
            "WHERE id = ? AND status = 'QUEUED'",
            (utcnow().isoformat(), str(steer_id)),
        )
        return int(getattr(cursor, "rowcount", 0)) == 1

    async def mark_missed(self, task_id: str) -> int:
        cursor = await self._db.execute(
            "UPDATE task_steers SET status = 'MISSED' WHERE task_id = ? AND status = 'QUEUED'",
            (str(task_id),),
        )
        return max(0, int(getattr(cursor, "rowcount", 0)))


__all__ = ["SteeringSource", "TaskSteeringStore"]
