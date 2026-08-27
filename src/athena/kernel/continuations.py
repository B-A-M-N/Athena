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
from datetime import timedelta
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
            "approval_id TEXT, "
            "provider_profile_id TEXT, "
            "model_id TEXT, "
            "repair_policy_version TEXT, "
            "model_turn INTEGER, "
            "policy_context TEXT, "
            "created_at TEXT NOT NULL, "
            "resolved_at TEXT, "
            "decision TEXT, "
            "claimed_at TEXT, "
            "consumed_at TEXT)"
        )
        # Existing databases need the same durable continuation contract.
        for column, definition in (
            ("approval_id", "TEXT"),
            ("provider_profile_id", "TEXT"),
            ("model_id", "TEXT"),
            ("repair_policy_version", "TEXT"),
            ("model_turn", "INTEGER"),
            ("policy_context", "TEXT"),
            ("decision", "TEXT"),
            ("claimed_at", "TEXT"),
            ("consumed_at", "TEXT"),
        ):
            try:
                await self._db.execute(
                    f"ALTER TABLE continuations ADD COLUMN {column} {definition}"
                )
            except Exception:
                # The column already exists on normal startup. Other schema
                # failures are surfaced by the original CREATE/next operation.
                pass
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
        approval_id: str | None = None,
        provider_profile_id: str | None = None,
        model_id: str | None = None,
        repair_policy_version: str | None = None,
        model_turn: int | None = None,
        policy_context: Mapping[str, Any] | None = None,
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
            "schema_hash, effects, workspace_id, approval_id, "
            "provider_profile_id, model_id, repair_policy_version, model_turn, "
            "policy_context, created_at, resolved_at, decision, claimed_at, consumed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            (
                cid,
                task_id,
                call_id,
                capability_id,
                json.dumps(dict(canonical_arguments or {})),
                schema_hash,
                json.dumps(effects_values),
                workspace_id,
                approval_id,
                provider_profile_id,
                model_id,
                repair_policy_version,
                model_turn,
                json.dumps(dict(policy_context or {})),
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

    async def mark_resolved(self, id: str, decision: str = "granted") -> None:
        await self.ensure_table()
        now = utcnow().isoformat()
        await self._db.execute(
            "UPDATE continuations SET resolved_at = ?, decision = ? WHERE id = ?",
            (now, decision, id),
        )

    async def claim_resolved(self, task_id: str) -> dict | None:
        """Atomically claim one approved continuation for post-restart work.

        Claiming is not consuming. The row remains replayable until the
        canonical capability result is durably appended. A stale claim is
        reclaimable after a crash; this closes the old lost-call window where
        the row was marked consumed before execution began.
        """
        await self.ensure_table()
        now = utcnow()
        stale_before = (now - timedelta(minutes=5)).isoformat()
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT * FROM continuations WHERE task_id = ? "
                "AND resolved_at IS NOT NULL AND consumed_at IS NULL "
                "AND (claimed_at IS NULL OR claimed_at < ?) "
                "ORDER BY resolved_at ASC LIMIT 1",
                (task_id, stale_before),
            )
            if row is None:
                return None
            claimed_at = now.isoformat()
            await self._db.execute_raw(
                "UPDATE continuations SET claimed_at = ? "
                "WHERE id = ? AND consumed_at IS NULL "
                "AND (claimed_at IS NULL OR claimed_at < ?)",
                (claimed_at, row["id"], stale_before),
            )
            row["claimed_at"] = claimed_at
            return _decode_row(row)

    async def mark_consumed_for_call(self, call_id: str) -> None:
        await self.ensure_table()
        await self._db.execute(
            "UPDATE continuations SET consumed_at = COALESCE(consumed_at, ?), "
            "claimed_at = NULL "
            "WHERE call_id = ?",
            (utcnow().isoformat(), call_id),
        )

    async def release_claim(self, call_id: str) -> None:
        """Return an unconsumed claim to the recovery queue after failure."""
        await self.ensure_table()
        await self._db.execute(
            "UPDATE continuations SET claimed_at = NULL "
            "WHERE call_id = ? AND consumed_at IS NULL",
            (call_id,),
        )

    async def release_claims_for_restart(self) -> None:
        """Release unresolved claims left by a process that disappeared.

        Continuation claims are an intra-service coordination primitive. Athena
        currently permits one active service owner for a database, so startup
        is the ownership boundary: any claim that survived the previous
        process must be made recoverable before task recovery begins. The
        canonical call remains durable and is still protected by
        ``consumed_at``.
        """
        await self.ensure_table()
        await self._db.execute(
            "UPDATE continuations SET claimed_at = NULL "
            "WHERE resolved_at IS NOT NULL AND consumed_at IS NULL "
            "AND claimed_at IS NOT NULL"
        )

    async def recoverable_task_ids(self) -> list[str]:
        """Return tasks with resolved, unconsumed approval continuations.

        The result is intentionally task-level. A task can have more than one
        suspended call (for example, a parallel batch); one kernel run consumes
        them in canonical order across successive iterations.
        """
        await self.ensure_table()
        rows = await self._db.fetch_all(
            "SELECT task_id, MIN(resolved_at) AS first_resolved "
            "FROM continuations "
            "WHERE task_id IS NOT NULL AND resolved_at IS NOT NULL "
            "AND consumed_at IS NULL "
            "GROUP BY task_id ORDER BY first_resolved ASC"
        )
        return [str(row["task_id"]) for row in rows if row.get("task_id")]

    async def unconsumed_for_approval(self, approval_id: str) -> list[dict]:
        """Return approved continuations that still need one execution."""
        await self.ensure_table()
        rows = await self._db.fetch_all(
            "SELECT * FROM continuations WHERE approval_id = ? "
            "AND resolved_at IS NOT NULL AND consumed_at IS NULL "
            "ORDER BY resolved_at ASC",
            (approval_id,),
        )
        return [_decode_row(row) for row in rows]


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
