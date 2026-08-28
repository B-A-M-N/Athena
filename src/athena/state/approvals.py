from __future__ import annotations

import json
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class ApprovalStore:
    """Approval request lifecycle (§21 approvals; BHV single authority)."""

    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_request(
        self,
        task_id: str | None,
        capability_id: str,
        *,
        arguments: Any = None,
        approval_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        mid = approval_id or new_id("apr")
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO approvals("
            "id, task_id, capability_id, arguments, status, created_at, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mid,
                task_id,
                capability_id,
                json.dumps(arguments) if arguments is not None else None,
                self.PENDING,
                now,
                json.dumps(dict(metadata or {})),
            ),
        )
        return mid

    async def record_grant(
        self,
        approval_id: str,
        resolver: str | None = None,
        *,
        grant_id: str | None = None,
        scope: str | None = None,
        expires_at: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = utcnow().isoformat()
        capability_id = await self._capability_of(approval_id)
        async with self._db.transaction():
            await self._db.execute_raw(
                "UPDATE approvals SET status = ?, resolved_at = ?, resolver = ? WHERE id = ?",
                (self.GRANTED, now, resolver, approval_id),
            )
            await self._db.execute_raw(
                "INSERT INTO approval_grants("
                "id, approval_id, capability_id, scope, expires_at, created_at, metadata"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grant_id or new_id("apr"),
                    approval_id,
                    capability_id,
                    scope,
                    expires_at,
                    now,
                    json.dumps(dict(metadata or {})),
                ),
            )

    async def record_deny(
        self,
        approval_id: str,
        *,
        resolver: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, resolver = ?, "
            "metadata = ? WHERE id = ?",
            (
                self.DENIED,
                now,
                resolver,
                json.dumps(dict(metadata or {})),
                approval_id,
            ),
        )

    async def get(self, approval_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            return None
        return _decode_approval(row)

    async def list_pending(self, task_id: str | None = None) -> list[dict]:
        if task_id is not None:
            rows = await self._db.fetch_all(
                "SELECT * FROM approvals WHERE task_id = ? AND status = ? ORDER BY created_at ASC",
                (task_id, self.PENDING),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at ASC",
                (self.PENDING,),
            )
        return [_decode_approval(r) for r in rows]

    async def list_for_task(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        return [_decode_approval(r) for r in rows]

    async def list_granted(self) -> list[dict]:
        """Return granted approvals with their persisted effective scope."""
        rows = await self._db.fetch_all(
            "SELECT a.*, g.scope AS grant_scope, g.expires_at AS grant_expires_at "
            "FROM approvals AS a LEFT JOIN approval_grants AS g "
            "ON g.approval_id = a.id "
            "WHERE a.status = ? ORDER BY a.resolved_at ASC, g.created_at ASC",
            (self.GRANTED,),
        )
        return [_decode_approval(r) for r in rows]

    async def _capability_of(self, approval_id: str) -> str:
        row = await self._db.fetch_one(
            "SELECT capability_id FROM approvals WHERE id = ?",
            (approval_id,),
        )
        if row is None or not row.get("capability_id"):
            raise KeyError(f"Approval not found: {approval_id}")
        return row["capability_id"]


def _decode_approval(row: dict) -> dict:
    for key in ("arguments", "metadata"):
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row


__all__ = ["ApprovalStore"]
