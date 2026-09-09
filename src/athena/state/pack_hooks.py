"""Durable outbox for declarative capability-pack hooks."""

from __future__ import annotations

import json
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class PackHookOutbox:
    """Persist hook deliveries before asking task intake to enqueue work."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        pack_id: str,
        hook_id: str,
        event_id: str,
        event_type: str,
        task_id: str | None,
        session_id: str | None,
        payload: Mapping[str, Any],
        depth: int,
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT OR IGNORE INTO pack_hook_outbox("
            "id, pack_id, hook_id, event_id, event_type, task_id, session_id, "
            "payload, depth, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)",
            (
                new_id("hook-delivery"),
                str(pack_id),
                str(hook_id),
                str(event_id),
                str(event_type),
                task_id,
                session_id,
                json.dumps(dict(payload), sort_keys=True, default=str),
                int(depth),
                now,
                now,
            ),
        )
        row = await self._db.fetch_one(
            "SELECT * FROM pack_hook_outbox WHERE hook_id = ? AND event_id = ?",
            (str(hook_id), str(event_id)),
        )
        return dict(row or {})

    async def pending(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM pack_hook_outbox WHERE status != 'DISPATCHED' ORDER BY created_at, id"
        )
        return [dict(row) for row in rows]

    async def mark_dispatched(self, row_id: str, task_id: str | None) -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'DISPATCHED', dispatched_task_id = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (task_id, utcnow().isoformat(), str(row_id)),
        )

    async def mark_failed(self, row_id: str, error: str) -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'PENDING', error = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (str(error)[:2000], utcnow().isoformat(), str(row_id)),
        )


__all__ = ["PackHookOutbox"]
