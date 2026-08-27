from __future__ import annotations

import json
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class ScheduleStore:
    """Scheduled job persistence and atomic occurrence claims (§74-77)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_job(
        self,
        job_id: str,
        name: str,
        payload: Any = None,
        *,
        cron: str | None = None,
        trigger_spec: dict | None = None,
        enabled: bool = True,
        next_run: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Idempotent upsert keyed on job id (§77).

        A structured ``trigger_spec`` (a serializable :class:`TriggerSpec` dict)
        is persisted so scheduling info round-trips: the scheduler reloads it to
        reschedule regardless of trigger type. It is stored under a reserved
        ``_trigger_spec`` key in the metadata column; the existing flat ``cron``
        column remains for backward compatibility.
        """
        meta = dict(metadata or {})
        if trigger_spec is not None:
            meta["_trigger_spec"] = trigger_spec
        now = utcnow().isoformat()
        existing = await self._db.fetch_one(
            "SELECT id FROM scheduled_jobs WHERE id = ?", (job_id,)
        )
        if existing is None:
            await self._db.execute(
                "INSERT INTO scheduled_jobs("
                "id, name, cron, payload, enabled, next_run, created_at, updated_at, metadata"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    name,
                    cron,
                    json.dumps(payload) if payload is not None else None,
                    1 if enabled else 0,
                    next_run,
                    now,
                    now,
                    json.dumps(meta),
                ),
            )
        else:
            await self._db.execute(
                "UPDATE scheduled_jobs SET "
                "name = ?, cron = ?, payload = ?, enabled = ?, "
                "next_run = ?, metadata = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    name,
                    cron,
                    json.dumps(payload) if payload is not None else None,
                    1 if enabled else 0,
                    next_run,
                    json.dumps(meta),
                    now,
                    job_id,
                ),
            )
        return await self.get_job(job_id) or {}

    async def get_job(self, job_id: str) -> dict | None:
        return await self.get_job_id(job_id)

    async def get_job_id(self, job_id: str) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)
        )
        if row is None:
            return None
        return _decode_job(row)

    async def list_jobs(self, enabled_only: bool = True) -> list[dict]:
        if enabled_only:
            rows = await self._db.fetch_all(
                "SELECT * FROM scheduled_jobs WHERE enabled = 1 "
                "ORDER BY next_run ASC"
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT * FROM scheduled_jobs ORDER BY next_run ASC"
            )
        return [_decode_job(r) for r in rows]

    async def set_next_run(self, job_id: str, next_run: str | None) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "UPDATE scheduled_jobs SET next_run = ?, updated_at = ? WHERE id = ?",
            (next_run, now, job_id),
        )

    async def set_enabled(self, job_id: str, enabled: bool) -> bool:
        now = utcnow().isoformat()
        cursor = await self._db.execute(
            "UPDATE scheduled_jobs SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now, job_id),
        )
        return bool(cursor.rowcount)

    async def claim_next_due(self, job_id: str, scheduled_for: str) -> dict | None:
        """Atomically claim one occurrence of a job.

        Enforces the (job_id, scheduled_for) uniqueness constraint so a given
        occurrence is claimed exactly once (§77, §86). Returns the claim record
        or None if already claimed.
        """
        if not await self._job_enabled(job_id):
            return None
        claim_id = new_id("job")
        now = self._now_iso()
        try:
            async with self._db.transaction():
                await self._db.execute_raw(
                    "UPDATE scheduled_jobs SET last_run = ? WHERE id = ?",
                    (scheduled_for, job_id),
                )
                exists = await self._db.fetch_one_raw(
                    "SELECT id FROM job_runs WHERE job_id = ? AND scheduled_for = ?",
                    (job_id, scheduled_for),
                )
                if exists is not None:
                    return None
                await self._db.execute_raw(
                    "INSERT INTO job_runs("
                    "id, job_id, scheduled_for, claim_id, started_at, status"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (claim_id, job_id, scheduled_for, claim_id, now, "CLAIMED"),
                )
        except Exception:
            return None
        return {
            "id": claim_id,
            "job_id": job_id,
            "scheduled_for": scheduled_for,
            "claim_id": claim_id,
            "status": "CLAIMED",
        }

    async def mark_fired(self, claim_id: str, task_id: str | None = None) -> None:
        now = self._now_iso()
        await self._db.execute(
            "UPDATE job_runs SET status = ?, task_id = ?, ended_at = ? "
            "WHERE id = ?",
            ("FIRED", task_id, now, claim_id),
        )

    async def complete_claim(
        self,
        claim_id: str,
        job_id: str | None = None,
        task_id: str | None = None,
        *,
        next_run: str | None = None,
        disable: bool = False,
    ) -> None:
        now = self._now_iso()
        await self._db.execute(
            "UPDATE job_runs SET status = ?, task_id = ?, ended_at = ? "
            "WHERE id = ?",
            ("FIRED", task_id, now, claim_id),
        )
        if job_id is not None:
            await self._db.execute(
                "UPDATE scheduled_jobs SET next_run = ?, updated_at = ? WHERE id = ?",
                (next_run, now, job_id),
            )
            if disable:
                await self._db.execute(
                    "UPDATE scheduled_jobs SET enabled = 0 WHERE id = ?",
                    (job_id,),
                )

    async def release_claim(
        self, claim_id: str, job_id: str, scheduled_for: str
    ) -> None:
        """Release a CLAIMED occurrence without marking it fired.

        Deletes the claim row and restores ``next_run`` to the occurrence time so
        the next tick reclaims it. Used when task creation fails transiently, so
        no occurrence is silently lost (§77).
        """
        now = self._now_iso()
        try:
            async with self._db.transaction():
                await self._db.execute_raw(
                    "DELETE FROM job_runs WHERE id = ?", (claim_id,)
                )
                await self._db.execute_raw(
                    "UPDATE scheduled_jobs SET next_run = ?, updated_at = ? "
                    "WHERE id = ?",
                    (scheduled_for, now, job_id),
                )
        except Exception:
            return

    async def mark_failed(
        self, claim_id: str, task_id: str | None = None, error: str | None = None
    ) -> None:
        now = self._now_iso()
        await self._db.execute(
            "UPDATE job_runs SET status = ?, task_id = ?, ended_at = ?, error = ? "
            "WHERE id = ?",
            ("FAILED", task_id, now, error, claim_id),
        )

    async def reconcile_stale_occurrences(self) -> int:
        """Recover CLAIMED occurrences left orphaned by a crash mid-fire.

        Uses json_extract on the explicit _occurrence metadata key for reliable
        matching instead of LIKE substring search on JSON metadata.
        """
        rows = await self._db.fetch_all(
            "SELECT id, job_id, scheduled_for FROM job_runs "
            "WHERE status = 'CLAIMED'"
        )
        reconciled = 0
        for row in rows or []:
            claim_id = row["id"]
            job_id = row["job_id"]
            scheduled_for = row["scheduled_for"]
            if not scheduled_for:
                continue
            occurrence_key = f"{job_id}|{scheduled_for}"
            # Use json_extract for reliable metadata key matching
            task = await self._db.fetch_one(
                "SELECT id FROM tasks "
                "WHERE json_extract(metadata, '$._occurrence') = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (occurrence_key,),
            )
            if task is not None:
                await self.mark_fired(claim_id, task["id"])
            else:
                await self.release_claim(claim_id, job_id, scheduled_for)
            reconciled += 1
        return reconciled

    async def last_run(self, job_id: str) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM job_runs WHERE job_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        )
        return _decode_run(row) if row else None

    async def count_runs(self, job_id: str) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS count FROM job_runs WHERE job_id = ?",
            (job_id,),
        )
        return int(row.get("count", 0)) if row else 0

    async def _job_enabled(self, job_id: str) -> bool:
        row = await self._db.fetch_one(
            "SELECT enabled FROM scheduled_jobs WHERE id = ?", (job_id,)
        )
        return bool(row and row.get("enabled", 0))

    async def _get_claim(self, claim_id: str) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT * FROM job_runs WHERE id = ?", (claim_id,)
        )
        return _decode_run(row) if row else None

    def _now_iso(self) -> str:
        return utcnow().isoformat()

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job and its run history."""
        try:
            async with self._db.transaction():
                existing = await self._db.fetch_one_raw(
                    "SELECT id FROM scheduled_jobs WHERE id = ?", (job_id,)
                )
                if existing is None:
                    return False
                await self._db.execute_raw(
                    "DELETE FROM job_runs WHERE job_id = ?", (job_id,)
                )
                await self._db.execute_raw(
                    "DELETE FROM scheduled_jobs WHERE id = ?", (job_id,)
                )
                return True
        except Exception:
            return False


def _json_like_escape(value: str) -> str:
    """Escape a string for use in a SQLite LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _decode_job(row: dict) -> dict:
    row["enabled"] = bool(row.get("enabled", 0))
    for key in ("payload", "metadata"):
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row


def _decode_run(row: dict) -> dict:
    for key in ("metadata",):
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row
