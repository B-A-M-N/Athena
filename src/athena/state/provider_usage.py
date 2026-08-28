from __future__ import annotations

import json

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database

__all__ = ["ProviderUsageStore"]


class ProviderUsageStore:
    """Records provider/model request history for durable querying.

    Each model attempt (including failed/fallback attempts) is recorded so
    provider usage, tokens, and costs are durably queryable.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_attempt(
        self,
        *,
        provider: str,
        model: str,
        task_id: str | None = None,
        session_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Record a model attempt (successful or failed)."""
        uid = new_id("usage")
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO provider_usage("
            "id, provider, model, task_id, session_id, input_tokens, "
            "output_tokens, cost_usd, started_at, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                provider,
                model,
                task_id,
                session_id,
                input_tokens,
                output_tokens,
                cost_usd,
                now,
                json.dumps(dict(metadata or {})),
            ),
        )
        return uid

    async def record_completion(
        self,
        usage_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Update a recorded attempt with final token/cost counts."""
        now = utcnow().isoformat()
        if metadata is None:
            await self._db.execute(
                "UPDATE provider_usage SET input_tokens = ?, output_tokens = ?, "
                "cost_usd = ?, ended_at = ? WHERE id = ?",
                (input_tokens, output_tokens, cost_usd, now, usage_id),
            )
            return
        await self._db.execute(
            "UPDATE provider_usage SET input_tokens = ?, output_tokens = ?, "
            "cost_usd = ?, ended_at = ?, metadata = ? WHERE id = ?",
            (input_tokens, output_tokens, cost_usd, now, json.dumps(dict(metadata)), usage_id),
        )

    async def list_for_task(self, task_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM provider_usage WHERE task_id = ? ORDER BY started_at ASC",
            (task_id,),
        )
        return [_decode(r) for r in rows]

    async def list_recent(self, limit: int = 100) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM provider_usage ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [_decode(r) for r in rows]


def _decode(row: dict) -> dict:
    val = row.get("metadata")
    if val:
        try:
            row["metadata"] = json.loads(val)
        except (TypeError, ValueError):
            pass
    return row
