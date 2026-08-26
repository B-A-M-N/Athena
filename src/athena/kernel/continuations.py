"""Durable approval continuations (review item 19).

When the dispatcher parks a capability call for approval, the in-kernel
continuation (``SuspendedCall`` + resume events) is in-memory only: a process
restart loses the exact call that was awaiting resolution even though the
approval record itself is durable (``ApprovalStore``). This store persists the
continuation — the call identity, canonical arguments, schema hash and
effects — so a restarted service can reconstruct what to re-dispatch once an
approval resolves.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class ContinuationStore:
    """Durable records of parked-for-approval capability calls."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensured = False

    async def ensure_table(self) -> None:
        if self._ensured:
            return
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS continuations("
            "id TEXT PRIMARY KEY, "
            "task_id TEXT, "
            "call_id TEXT, "
            "capability_id TEXT, "
            "canonical_arguments TEXT, "
            "schema_hash TEXT, "
            "effects TEXT, "
            "workspace_id TEXT, "
            "created_at TEXT NOT NULL, "
            "resolved_at TEXT)"
        )
        self._ensured = True

    async def record(
        self,
        *,
        task_id: str | None,
        call_id: str,
        capability_id: str,
        canonical_arguments: Mapping[str, Any] | None,
        schema_hash: str | None = None,
        effects=None,
        workspace_id: str | None = None,
        id: str | None = None,
    ) -> str:
        """Persist one continuation row; returns its id."""
        await self.ensure_table()
        cid = id or new_id("cont")
        now = utcnow().isoformat()
        effects_values = [getattr(e, "value", str(e)) for e in (effects or [])]
        await self._db.execute(
            "INSERT INTO continuations("
            "id, task_id, call_id, capability_id, canonical_arguments, "
            "schema_hash, effects, workspace_id, created_at, resolved_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                cid,
                task_id,
                call_id,
                capability_id,
                json.dumps(dict(canonical_arguments or {})),
                schema_hash,
                json.dumps(effects_values),
                workspace_id,
                now,
            ),
        )
        return cid

    async def pending(self, task_id: str | None = None) -> list[dict]:
        """Unresolved continuation rows, oldest first."""
        await self.ensure_table()
        if task_id is not None:
            rows = await self._db.fetch_all(
                "SELECT * FROM continuations WHERE resolved_at IS NULL AND task_id = ? "
                "ORDER BY created_at ASC",
                (task_id,),
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM continuations WHERE resolved_at IS NULL "
                "ORDER BY created_at ASC"
            )
        return [_decode_row(r) for r in rows]

    async def mark_resolved(self, id: str) -> None:
        await self.ensure_table()
        now = utcnow().isoformat()
        await self._db.execute(
            "UPDATE continuations SET resolved_at = ? WHERE id = ?",
            (now, id),
        )


def _decode_row(row: dict) -> dict:
    for key in ("canonical_arguments", "effects"):
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row


__all__ = ["ContinuationStore"]
