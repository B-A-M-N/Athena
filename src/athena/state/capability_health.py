"""Durable circuit-breaker state for capability health."""

from __future__ import annotations

from typing import Any, Mapping

from athena.state.database import Database


class CapabilityHealthStore:
    """Persist health records without coupling the dispatcher to SQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all("SELECT * FROM capability_health ORDER BY capability_id")
        return [dict(row) for row in rows]

    async def save(self, record: Mapping[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO capability_health (capability_id, status, total_calls, "
            "successes, failures, consecutive_failures, last_failure, "
            "last_failure_at, last_success_at, opened_at, cooldown_seconds, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(capability_id) DO UPDATE SET status=excluded.status, "
            "total_calls=excluded.total_calls, successes=excluded.successes, "
            "failures=excluded.failures, consecutive_failures=excluded.consecutive_failures, "
            "last_failure=excluded.last_failure, last_failure_at=excluded.last_failure_at, "
            "last_success_at=excluded.last_success_at, opened_at=excluded.opened_at, "
            "cooldown_seconds=excluded.cooldown_seconds, updated_at=excluded.updated_at",
            (
                str(record.get("capability_id") or ""),
                str(record.get("status") or "closed"),
                int(record.get("total_calls") or 0),
                int(record.get("successes") or 0),
                int(record.get("failures") or 0),
                int(record.get("consecutive_failures") or 0),
                record.get("last_failure"),
                record.get("last_failure_at"),
                record.get("last_success_at"),
                record.get("opened_at"),
                float(record.get("cooldown_seconds") or 30.0),
            ),
        )

    async def delete(self, capability_id: str) -> None:
        await self._db.execute(
            "DELETE FROM capability_health WHERE capability_id = ?",
            (capability_id,),
        )

    async def clear(self) -> None:
        await self._db.execute("DELETE FROM capability_health")


__all__ = ["CapabilityHealthStore"]
