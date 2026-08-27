from __future__ import annotations

import json
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database

PLANNED = "PLANNED"
STARTED = "STARTED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
ROLLED_BACK = "ROLLED_BACK"


class MutationStore:
    """Structured mutation ledger with write-ahead intents (BHV-055, P1-25).

    Each mutation begins as a PLANNED intent persisted BEFORE its side effect
    (write-ahead), then moves STARTED -> COMPLETED with the after-state, or to
    FAILED / RECOVERY_REQUIRED. ``before_ref`` names the immutable artifact
    snapshot that restores the prior state; ``inverse`` describes the undo op.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_intent(
        self,
        task_id: str | None,
        resource: str,
        operation: str,
        *,
        execution_id: str | None = None,
        before_ref: str | None = None,
        inverse: dict | None = None,
        mutation_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Persist a PLANNED intent BEFORE the side effect (write-ahead)."""
        mid = mutation_id or new_id("mut")
        now = utcnow().isoformat()
        async with self._db.transaction() as db:
            sequence = await self._next_sequence(db, task_id)
            await db.execute_raw(
                "INSERT INTO mutations("
                "id, task_id, execution_id, resource, operation, sequence, reversible, "
                "before_state, after_state, status, before_ref, inverse, created_at, metadata"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mid,
                    task_id,
                    execution_id,
                    resource,
                    operation,
                    sequence,
                    0,
                    json.dumps(before_ref) if before_ref is not None else None,
                    None,
                    PLANNED,
                    before_ref,
                    json.dumps(inverse) if inverse is not None else None,
                    now,
                    json.dumps(dict(metadata or {})),
                ),
            )
        return mid

    async def mark_started(self, mutation_id: str) -> None:
        await self._update_status(mutation_id, STARTED)

    async def complete(
        self,
        mutation_id: str,
        *,
        after_hash: str | None = None,
        reversible: bool = False,
        inverse: dict | None = None,
    ) -> None:
        """FINALIZE a mutation as COMPLETED with its after-state."""
        await self._db.execute(
            "UPDATE mutations SET after_state = ?, reversible = ?, status = ?, inverse = ? "
            "WHERE id = ?",
            (
                after_hash,
                1 if reversible else 0,
                COMPLETED,
                json.dumps(inverse) if inverse is not None else None,
                mutation_id,
            ),
        )

    async def mark_failed(self, mutation_id: str, error: str | None = None) -> None:
        """Mark FAILED, merging the error into existing metadata (not replacing)."""
        row = await self._db.fetch_one(
            "SELECT metadata FROM mutations WHERE id = ?", (mutation_id,)
        )
        try:
            meta = json.loads((row or {}).get("metadata") or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if error is not None:
            meta["error"] = error
        await self._db.execute(
            "UPDATE mutations SET status = ?, metadata = ? WHERE id = ?",
            (FAILED, json.dumps(meta), mutation_id),
        )

    async def mark_recovery_required(self, mutation_id: str) -> None:
        await self._update_status(mutation_id, RECOVERY_REQUIRED)

    async def mark_rolled_back(self, mutation_id: str) -> None:
        await self._update_status(mutation_id, ROLLED_BACK)

    async def _update_status(self, mutation_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE mutations SET status = ? WHERE id = ?", (status, mutation_id)
        )

    async def record(
        self,
        task_id: str | None,
        resource: str,
        operation: str,
        *,
        execution_id: str | None = None,
        before_state: Any = None,
        after_state: Any = None,
        reversible: bool = False,
        mutation_id: str | None = None,
        metadata: dict | None = None,
        before_ref: str | None = None,
        inverse: dict | None = None,
    ) -> str:
        """Record a completed mutation in one step (backward-compatible).

        Models the intent and its completion atomically so callers that never
        had a separate write-ahead phase still produce a complete, accurate
        record.
        """
        mid = mutation_id or new_id("mut")
        now = utcnow().isoformat()
        async with self._db.transaction() as db:
            sequence = await self._next_sequence(db, task_id)
            await db.execute_raw(
                "INSERT INTO mutations("
                "id, task_id, execution_id, resource, operation, sequence, reversible, "
                "before_state, after_state, status, before_ref, inverse, created_at, metadata"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mid,
                    task_id,
                    execution_id,
                    resource,
                    operation,
                    sequence,
                    1 if reversible else 0,
                    json.dumps(before_ref) if before_ref is not None else before_state,
                    json.dumps(after_state) if after_state is not None else None,
                    COMPLETED,
                    before_ref,
                    json.dumps(inverse) if inverse is not None else None,
                    now,
                    json.dumps(dict(metadata or {})),
                ),
            )
        return mid

    @staticmethod
    async def _next_sequence(db: Database, task_id: str | None) -> int:
        """Allocate a monotonic sequence inside the mutation transaction."""
        if task_id is None:
            row = await db.fetch_one_raw(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM mutations WHERE task_id IS NULL"
            )
        else:
            row = await db.fetch_one_raw(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM mutations WHERE task_id = ?", (task_id,)
            )
        return int((row or {}).get("sequence") or 1)

    async def mark_reversible(self, mutation_id: str, reversible: bool = True) -> None:
        await self._db.execute(
            "UPDATE mutations SET reversible = ? WHERE id = ?",
            (1 if reversible else 0, mutation_id),
        )

    async def get(self, mutation_id: str) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM mutations WHERE id = ?", (mutation_id,)
        )
        if row is None:
            return None
        return _decode_mutation(row)

    async def sequence_for(self, mutation_id: str) -> int | None:
        row = await self._db.fetch_one(
            "SELECT sequence FROM mutations WHERE id = ?", (mutation_id,)
        )
        if row is None or row.get("sequence") is None:
            return None
        return int(row["sequence"])

    async def list_for_task(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM mutations WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        return [_decode_mutation(r) for r in rows]

    async def list_by_status(self, status: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM mutations WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
        return [_decode_mutation(r) for r in rows]

    async def list_recent(self, *, limit: int = 25) -> list[dict]:
        """Most recent mutations across all tasks (operator /diff view)."""
        rows = await self._db.fetch_all(
            "SELECT * FROM mutations ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        return [_decode_mutation(r) for r in rows]


def _decode_mutation(row: dict) -> dict:
    row["reversible"] = bool(row.get("reversible", 0))
    for key in ("before_state", "after_state", "inverse", "metadata"):
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row


__all__ = ["MutationStore", "PLANNED", "STARTED", "COMPLETED", "FAILED",
           "RECOVERY_REQUIRED"]
