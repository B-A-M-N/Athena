"""Durable ownership obligations for task-scoped resource teardown."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow


OPEN_STATES = ("PENDING_CLOSE", "UNPROVEN", "RECOVERY_REQUIRED")


class ResourceObligationStore:
    """Persist cleanup ownership until a close proof is durable."""

    def __init__(self, db) -> None:
        self._db = db

    async def record_failure(
        self,
        *,
        task_id: str,
        resource_type: str,
        resource_id: str,
        ownership_identity: Mapping[str, Any] | str | None,
        error: str | None,
        proof: Mapping[str, Any],
        state: str = "UNPROVEN",
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        identity = (
            ownership_identity
            if isinstance(ownership_identity, str)
            else json.dumps(dict(ownership_identity or {}), sort_keys=True)
        )
        existing = await self._db.fetch_one(
            "SELECT * FROM task_resource_obligations "
            "WHERE task_id = ? AND resource_type = ? AND resource_id = ?",
            (str(task_id), str(resource_type), str(resource_id)),
        )
        if existing is None:
            record = {
                "id": new_id("resource-obligation"),
                "task_id": str(task_id),
                "resource_type": str(resource_type),
                "resource_id": str(resource_id),
                "state": str(state),
                "ownership_identity": identity,
                "first_failed_at": now,
                "last_attempt_at": now,
                "attempt_count": 1,
                "last_error": error,
                "proof": dict(proof),
                "resolved_at": None,
                "created_at": now,
                "updated_at": now,
            }
            await self._db.execute(
                "INSERT INTO task_resource_obligations("
                "id, task_id, resource_type, resource_id, state, ownership_identity, "
                "first_failed_at, last_attempt_at, attempt_count, last_error, proof, "
                "resolved_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    record["task_id"],
                    record["resource_type"],
                    record["resource_id"],
                    record["state"],
                    record["ownership_identity"],
                    record["first_failed_at"],
                    record["last_attempt_at"],
                    record["attempt_count"],
                    record["last_error"],
                    json.dumps(record["proof"], default=str),
                    record["resolved_at"],
                    record["created_at"],
                    record["updated_at"],
                ),
            )
            return record

        updated = dict(existing)
        updated.update(
            {
                "state": str(state),
                "ownership_identity": identity,
                "last_attempt_at": now,
                "attempt_count": int(existing.get("attempt_count") or 0) + 1,
                "last_error": error,
                "proof": dict(proof),
                "resolved_at": None,
                "updated_at": now,
            }
        )
        await self._db.execute(
            "UPDATE task_resource_obligations SET state = ?, ownership_identity = ?, "
            "last_attempt_at = ?, attempt_count = ?, last_error = ?, proof = ?, "
            "resolved_at = NULL, updated_at = ? WHERE id = ?",
            (
                updated["state"],
                updated["ownership_identity"],
                updated["last_attempt_at"],
                updated["attempt_count"],
                updated["last_error"],
                json.dumps(updated["proof"], default=str),
                updated["updated_at"],
                updated["id"],
            ),
        )
        return updated

    async def mark_closed(
        self,
        *,
        task_id: str,
        resource_type: str,
        resource_ids: tuple[str, ...] = (),
        proof: Mapping[str, Any] | None = None,
    ) -> int:
        now = utcnow().isoformat()
        params: list[Any] = [now, json.dumps(dict(proof or {}), default=str), now]
        where = "task_id = ? AND resource_type = ? AND state IN (?, ?, ?)"
        params.extend((str(task_id), str(resource_type), *OPEN_STATES))
        if resource_ids:
            placeholders = ",".join("?" for _ in resource_ids)
            where += f" AND resource_id IN ({placeholders})"
            params.extend(str(item) for item in resource_ids)
        cursor = await self._db.execute(
            "UPDATE task_resource_obligations SET state = 'CLOSED', resolved_at = ?, "
            "proof = ?, updated_at = ? WHERE " + where,
            params,
        )
        return max(0, int(getattr(cursor, "rowcount", 0)))

    async def list_open(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in OPEN_STATES)
        rows = await self._db.fetch_all(
            "SELECT * FROM task_resource_obligations "
            f"WHERE state IN ({placeholders}) ORDER BY first_failed_at, id",
            OPEN_STATES,
        )
        return [_decode(row) for row in rows]


def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    try:
        result["proof"] = json.loads(result.get("proof") or "{}")
    except (TypeError, ValueError):
        result["proof"] = {}
    return result


__all__ = ["OPEN_STATES", "ResourceObligationStore"]
