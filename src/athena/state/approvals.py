from __future__ import annotations

import json
from datetime import datetime, timezone
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
        """Durably resolve one pending request and insert its grant atomically.

        Approval resolution is a compare-and-set operation.  A missing,
        already-resolved, or expired request must never be treated as a
        successful grant by a caller that is about to wake a task.
        """
        now = utcnow().isoformat()
        expired = False
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            )
            capability_id = self._pending_capability(row, approval_id)
            if _is_expired((row or {}).get("metadata"), now):
                cursor = await self._db.execute_raw(
                    "UPDATE approvals SET status = ?, resolved_at = ?, resolver = ? "
                    "WHERE id = ? AND status = ?",
                    (self.EXPIRED, now, resolver, approval_id, self.PENDING),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Approval already resolved: {approval_id}")
                expired = True
            else:
                cursor = await self._db.execute_raw(
                    "UPDATE approvals SET status = ?, resolved_at = ?, resolver = ? "
                    "WHERE id = ? AND status = ?",
                    (self.GRANTED, now, resolver, approval_id, self.PENDING),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Approval already resolved: {approval_id}")
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
        if expired:
            raise ValueError(f"Approval expired: {approval_id}")

    async def record_deny(
        self,
        approval_id: str,
        *,
        resolver: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Durably deny exactly one still-pending request."""
        now = utcnow().isoformat()
        expired = False
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            )
            self._pending_capability(row, approval_id)
            if _is_expired((row or {}).get("metadata"), now):
                cursor = await self._db.execute_raw(
                    "UPDATE approvals SET status = ?, resolved_at = ?, resolver = ? "
                    "WHERE id = ? AND status = ?",
                    (self.EXPIRED, now, resolver, approval_id, self.PENDING),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Approval already resolved: {approval_id}")
                expired = True
            else:
                old_metadata = _decode_json((row or {}).get("metadata"), {})
                merged_metadata = {**old_metadata, **dict(metadata or {})}
                cursor = await self._db.execute_raw(
                    "UPDATE approvals SET status = ?, resolved_at = ?, resolver = ?, "
                    "metadata = ? WHERE id = ? AND status = ?",
                    (
                        self.DENIED,
                        now,
                        resolver,
                        json.dumps(merged_metadata),
                        approval_id,
                        self.PENDING,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Approval already resolved: {approval_id}")
        if expired:
            raise ValueError(f"Approval expired: {approval_id}")

    async def get(self, approval_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            return None
        return _decode_approval(row)

    async def list_pending(self, task_id: str | None = None) -> list[dict]:
        now = utcnow().isoformat()
        where = "task_id = ? AND status = ?" if task_id is not None else "status = ?"
        params = (task_id, self.PENDING) if task_id is not None else (self.PENDING,)
        async with self._db.transaction():
            rows = await self._db.fetch_all_raw(
                f"SELECT * FROM approvals WHERE {where} ORDER BY created_at ASC", params
            )
            for row in rows:
                if _is_expired(row.get("metadata"), now):
                    await self._db.execute_raw(
                        "UPDATE approvals SET status = ?, resolved_at = ? "
                        "WHERE id = ? AND status = ?",
                        (self.EXPIRED, now, row["id"], self.PENDING),
                    )
            rows = await self._db.fetch_all_raw(
                f"SELECT * FROM approvals WHERE {where} ORDER BY created_at ASC", params
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

    @classmethod
    def _pending_capability(cls, row: dict | None, approval_id: str) -> str:
        if row is None:
            raise KeyError(f"Approval not found: {approval_id}")
        if row.get("status") != cls.PENDING:
            raise ValueError(f"Approval already resolved: {approval_id}")
        if not row.get("capability_id"):
            raise ValueError(f"Approval has no capability: {approval_id}")
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


def _decode_json(value: object, default: dict[str, Any]) -> dict[str, Any]:
    if not value:
        return dict(default)
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return dict(default)
    return dict(parsed) if isinstance(parsed, dict) else dict(default)


def _is_expired(metadata: object, now: str) -> bool:
    values = _decode_json(metadata, {})
    raw = values.get("expires_at")
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    current = datetime.fromisoformat(now)
    return current >= expiry


__all__ = ["ApprovalStore"]
