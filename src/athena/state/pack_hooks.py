"""Durable outbox for declarative capability-pack hooks."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Mapping

from athena.protocol.ids import new_id, stable_id
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
        if row is not None and not row.get("hook_task_id"):
            hook_task_id = stable_id("pack-hook-task", hook_id, event_id)
            await self._db.execute(
                "UPDATE pack_hook_outbox SET hook_task_id = ?, updated_at = ? WHERE id = ?",
                (hook_task_id, utcnow().isoformat(), str(row["id"])),
            )
            row = await self._db.fetch_one(
                "SELECT * FROM pack_hook_outbox WHERE id = ?", (str(row["id"]),)
            )
        return dict(row or {})

    async def pending(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM pack_hook_outbox WHERE status NOT IN "
            "('DISPATCHED', 'CANCELLED', 'SUSPENDED') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at, id",
            (utcnow().isoformat(),),
        )
        return [dict(row) for row in rows]

    async def suspend_pack(self, pack_id: str, reason: str = "pack disabled") -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'SUSPENDED', error = ?, "
            "next_attempt_at = NULL, updated_at = ? "
            "WHERE pack_id = ? AND status NOT IN ('DISPATCHED', 'CANCELLED')",
            (str(reason)[:2000], utcnow().isoformat(), str(pack_id)),
        )

    async def resume_pack(self, pack_id: str) -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'PENDING', error = NULL, "
            "next_attempt_at = NULL, updated_at = ? WHERE pack_id = ? AND status = 'SUSPENDED'",
            (utcnow().isoformat(), str(pack_id)),
        )

    async def cancel_pack(self, pack_id: str, reason: str = "pack uninstalled") -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'CANCELLED', error = ?, "
            "next_attempt_at = NULL, updated_at = ? WHERE pack_id = ? "
            "AND status NOT IN ('DISPATCHED', 'CANCELLED')",
            (str(reason)[:2000], utcnow().isoformat(), str(pack_id)),
        )

    async def claim(self, row_id: str) -> dict[str, Any] | None:
        now = utcnow().isoformat()
        cursor = await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'CLAIMED', attempts = attempts + 1, "
            "updated_at = ?, error = NULL WHERE id = ? "
            "AND status IN ('PENDING', 'FAILED', 'CLAIMED') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (now, str(row_id), now),
        )
        if not cursor.rowcount:
            return None
        row = await self._db.fetch_one(
            "SELECT * FROM pack_hook_outbox WHERE id = ?", (str(row_id),)
        )
        return dict(row or {})

    async def mark_dispatched(self, row_id: str, task_id: str | None) -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'DISPATCHED', dispatched_task_id = ?, "
            "hook_task_id = COALESCE(hook_task_id, ?), next_attempt_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (task_id, task_id, utcnow().isoformat(), str(row_id)),
        )

    async def mark_failed(self, row_id: str, error: str, *, attempts: int | None = None) -> None:
        row = await self._db.fetch_one(
            "SELECT attempts FROM pack_hook_outbox WHERE id = ?", (str(row_id),)
        )
        count = int(attempts if attempts is not None else (row.get("attempts", 1) if row else 1))
        delay = min(300.0, 2.0 ** max(0, count - 1))
        retry_at = (utcnow() + timedelta(seconds=delay)).isoformat()
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'FAILED', error = ?, next_attempt_at = ?, "
            "updated_at = ? WHERE id = ?",
            (str(error)[:2000], retry_at, utcnow().isoformat(), str(row_id)),
        )

    async def mark_cancelled(self, row_id: str, reason: str) -> None:
        await self._db.execute(
            "UPDATE pack_hook_outbox SET status = 'CANCELLED', error = ?, "
            "next_attempt_at = NULL, updated_at = ? WHERE id = ?",
            (str(reason)[:2000], utcnow().isoformat(), str(row_id)),
        )


__all__ = ["PackHookOutbox"]
