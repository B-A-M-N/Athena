"""Durable storage for world-state claims (review items 36+38).

Mirrors the EventStore pattern: thin async wrapper over ``Database``.
Tables are created idempotently here rather than in a numbered migration
because world state is an auxiliary subsystem and the store must work
against both fresh temp databases (tests) and existing deployments.

Path-prefix semantics intentionally match ``core._paths_overlap`` so a
persisted flip agrees with what the in-memory registry would decide.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from athena.state.database import Database

__all__ = ["WorldStateStore"]

def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorldStateStore:
    """SQLite-backed durability layer for :mod:`athena.worldstate.core`."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._migrated = False

    async def _ensure_schema(self) -> None:
        if not self._migrated:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS claims ("
                "id TEXT PRIMARY KEY, task_id TEXT, text TEXT, status TEXT,"
                " evidence TEXT, depends_on_paths TEXT, created_at TEXT)")
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS claim_invalidations ("
                "claim_id TEXT, reason TEXT, ts TEXT)")
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS invariants ("
                "id TEXT PRIMARY KEY, task_id TEXT, description TEXT NOT NULL,"
                " definition TEXT NOT NULL, required INTEGER NOT NULL, created_at TEXT NOT NULL)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS invariant_results ("
                "id TEXT PRIMARY KEY, invariant_id TEXT NOT NULL, task_id TEXT,"
                " passed INTEGER NOT NULL, error TEXT, details TEXT NOT NULL,"
                " checked_at TEXT NOT NULL)"
            )
            self._migrated = True

    # -- writes -------------------------------------------------------------

    async def save_claim(self, record: dict) -> str:
        """Upsert one claim from a record-like dict (Claim or mapping)."""
        await self._ensure_schema()
        claim_id = str(record["id"])
        await self._db.execute(
            "INSERT INTO claims(id, task_id, text, status, evidence,"
            " depends_on_paths, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " text=excluded.text, status=excluded.status,"
            " evidence=excluded.evidence, depends_on_paths=excluded.depends_on_paths",
            (
                claim_id,
                record.get("task_id"),
                record.get("text"),
                record.get("status", "VERIFIED"),
                json.dumps(dict(record.get("evidence") or {})),
                json.dumps(list(record.get("depends_on_paths") or ())),
                _now(),
            ),
        )
        return claim_id

    async def invalidate_for_paths(
        self,
        task_id: str | None,
        paths: list[str],
        *,
        mutation_id: str | None = None,
        mutation_sequence: int | None = None,
        mutation_event_sequence: int | None = None,
    ) -> list[str]:
        """Flip VERIFIED -> STALE for matching claims; return flipped ids."""
        await self._ensure_schema()
        rows = await self._db.fetch_all(
            "SELECT id, text, depends_on_paths FROM claims WHERE status = ?"
            + (" AND task_id = ?" if task_id is not None else ""),
            ("VERIFIED",) if task_id is None else ("VERIFIED", task_id),
        )
        flipped: list[str] = []
        for row in rows:
            try:
                patterns = tuple(json.loads(row.get("depends_on_paths") or "[]"))
            except (TypeError, ValueError):
                patterns = ()
            if not any(_paths_overlap(patterns, p) for p in paths):
                continue
            reason: dict[str, object] = {"paths": sorted(paths)}
            if mutation_id is not None:
                reason["mutation_id"] = mutation_id
            if mutation_sequence is not None:
                reason["mutation_sequence"] = mutation_sequence
            if mutation_event_sequence is not None:
                reason["mutation_event_sequence"] = mutation_event_sequence
            await self._db.execute(
                "UPDATE claims SET status = 'STALE' WHERE id = ?", (row["id"],))
            await self._db.execute(
                "INSERT INTO claim_invalidations(claim_id, reason, ts)"
                " VALUES (?, ?, ?)",
                (row["id"], json.dumps(reason), _now()))
            flipped.append(row["id"])
        return flipped

    async def mark_contradicted(self, claim_id: str, because: str) -> bool:
        """Mark one claim CONTRADICTED; returns False if unknown/not verified."""
        await self._ensure_schema()
        row = await self._db.fetch_one(
            "SELECT status FROM claims WHERE id = ?", (claim_id,))
        if row is None:
            return False
        if row["status"] == "CONTRADICTED":
            return True
        await self._db.execute(
            "UPDATE claims SET status = 'CONTRADICTED' WHERE id = ?", (claim_id,))
        await self._db.execute(
            "INSERT INTO claim_invalidations(claim_id, reason, ts)"
            " VALUES (?, ?, ?)",
            (claim_id, json.dumps({"because": because}), _now()))
        return True

    async def save_invariant(self, record: dict) -> str:
        """Persist a declarative invariant definition."""
        await self._ensure_schema()
        invariant_id = str(record["id"])
        await self._db.execute(
            "INSERT INTO invariants(id, task_id, description, definition, required, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET description=excluded.description,"
            " definition=excluded.definition, required=excluded.required",
            (
                invariant_id,
                record.get("task_id"),
                str(record.get("description") or ""),
                json.dumps(dict(record.get("definition") or {})),
                1 if record.get("required", True) else 0,
                _now(),
            ),
        )
        return invariant_id

    async def record_invariant_result(self, record: dict) -> str:
        """Append one immutable invariant check result."""
        await self._ensure_schema()
        result_id = str(record.get("id") or "")
        if not result_id:
            raise ValueError("invariant result requires id")
        await self._db.execute(
            "INSERT INTO invariant_results(id, invariant_id, task_id, passed, error,"
            " details, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                result_id,
                str(record["invariant_id"]),
                record.get("task_id"),
                1 if record.get("passed", False) else 0,
                record.get("error"),
                json.dumps(dict(record.get("details") or {})),
                str(record.get("checked_at") or _now()),
            ),
        )
        return result_id

    async def invariants_for_task(self, task_id: str) -> list[dict]:
        await self._ensure_schema()
        rows = await self._db.fetch_all(
            "SELECT * FROM invariants WHERE task_id = ? ORDER BY created_at ASC, id ASC",
            (task_id,),
        )
        for row in rows:
            row["required"] = bool(row.get("required", 0))
            try:
                row["definition"] = json.loads(row.get("definition") or "{}")
            except (TypeError, ValueError):
                row["definition"] = {}
        return rows

    async def invariant_results_for_task(self, task_id: str) -> list[dict]:
        await self._ensure_schema()
        rows = await self._db.fetch_all(
            "SELECT * FROM invariant_results WHERE task_id = ? ORDER BY checked_at ASC, id ASC",
            (task_id,),
        )
        for row in rows:
            row["passed"] = bool(row.get("passed", 0))
            try:
                row["details"] = json.loads(row.get("details") or "{}")
            except (TypeError, ValueError):
                row["details"] = {}
        return rows

    # -- reads ----------------------------------------------------------------

    async def claims_for_task(self, task_id: str | None = None) -> list[dict]:
        await self._ensure_schema()
        rows = await self._db.fetch_all(
            "SELECT * FROM claims"
            + (" WHERE task_id = ?" if task_id is not None else "")
            + " ORDER BY created_at ASC, id ASC",
            () if task_id is None else (task_id,),
        )
        out = []
        for r in rows:
            rec = dict(r)
            rec["evidence"] = json.loads(rec.get("evidence") or "{}")
            rec["depends_on_paths"] = tuple(json.loads(rec.get("depends_on_paths") or "[]"))
            invalidations = await self._db.fetch_all(
                "SELECT reason, ts FROM claim_invalidations WHERE claim_id = ?"
                " ORDER BY ts ASC", (rec["id"],))
            rec["invalidated_by"] = [
                {**json.loads(i["reason"]), "ts": i["ts"]} for i in invalidations]
            out.append(rec)
        return out


def _paths_overlap(patterns: tuple[str, ...], path: str) -> bool:
    """Local re-implementation of core._paths_overlap semantics."""
    if not patterns:
        return True  # unscoped claim depends on the whole workspace
    for pat in patterns:
        if not pat or pat == "*":
            return True
        if path == pat or path.startswith(pat.rstrip("/") + "/"):
            return True
    return False
