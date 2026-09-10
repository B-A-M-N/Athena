"""Durable provider request/response receipts.

The provider boundary is inherently not part of SQLite's transaction.  This
store closes the useful crash window around it: request identity is recorded
before a provider call, and the normalized response is committed before the
kernel can compile the next prompt.  A restart can therefore replay a
completed response without calling the provider again.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping

from athena.protocol.models import ModelResponse, UsageInfo
from athena.protocol.messages import utcnow
from athena.state.database import Database
from athena.state.sessions import _deserialize_block, _serialize_block


class ModelResponseStore:
    """SQLite-backed request journal and normalized response receipts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def prepare(
        self,
        *,
        task_id: str,
        request_fingerprint: str,
        request_id: str,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        """Create or recover one task-scoped provider request identity."""
        existing = await self._db.fetch_one(
            "SELECT * FROM model_response_receipts WHERE task_id = ? AND request_fingerprint = ?",
            (task_id, request_fingerprint),
        )
        if existing is None:
            await self._db.execute(
                "INSERT OR IGNORE INTO model_response_receipts("
                "task_id, request_fingerprint, request_id, provider, model, status, created_at"
                ") VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
                (
                    task_id,
                    request_fingerprint,
                    request_id,
                    provider,
                    model,
                    utcnow().isoformat(),
                ),
            )
            existing = await self._db.fetch_one(
                "SELECT * FROM model_response_receipts "
                "WHERE task_id = ? AND request_fingerprint = ?",
                (task_id, request_fingerprint),
            )
        if existing is None:
            raise RuntimeError("model response receipt could not be prepared")
        if (
            str(existing.get("provider") or "") != provider
            or str(existing.get("model") or "") != model
        ):
            raise ValueError("model response receipt identity changed for the same request")
        status = str(existing.get("status") or "PENDING")
        if status == "FAILED":
            await self._db.execute(
                "UPDATE model_response_receipts SET request_id = ?, status = 'PENDING', "
                "response = NULL, completed_at = NULL WHERE task_id = ? "
                "AND request_fingerprint = ?",
                (request_id, task_id, request_fingerprint),
            )
            existing = await self._db.fetch_one(
                "SELECT * FROM model_response_receipts "
                "WHERE task_id = ? AND request_fingerprint = ?",
                (task_id, request_fingerprint),
            )
        return dict(existing or {})

    async def complete(
        self,
        *,
        task_id: str,
        request_fingerprint: str,
        response: ModelResponse,
    ) -> bool:
        """Persist a response exactly once and acknowledge the transition."""
        payload = json.dumps(_encode_response(response), sort_keys=True, default=str)
        cursor = await self._db.execute(
            "UPDATE model_response_receipts SET status = 'COMPLETED', response = ?, "
            "completed_at = ? WHERE task_id = ? AND request_fingerprint = ? "
            "AND status IN ('PENDING', 'COMPLETED')",
            (payload, utcnow().isoformat(), task_id, request_fingerprint),
        )
        if cursor.rowcount == 0:
            raise ValueError("model response receipt was not prepared")
        return True

    async def fail(self, *, task_id: str, request_fingerprint: str) -> bool:
        cursor = await self._db.execute(
            "UPDATE model_response_receipts SET status = 'FAILED' "
            "WHERE task_id = ? AND request_fingerprint = ? AND status = 'PENDING'",
            (task_id, request_fingerprint),
        )
        return cursor.rowcount == 1

    @staticmethod
    def response_from_row(row: Mapping[str, Any] | None) -> ModelResponse | None:
        if not row or str(row.get("status") or "") != "COMPLETED":
            return None
        raw = row.get("response")
        if not raw:
            return None
        try:
            return _decode_response(json.loads(str(raw)))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("model response receipt is corrupt") from exc

    async def list_completed(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM model_response_receipts WHERE task_id = ? "
            "AND status = 'COMPLETED' ORDER BY created_at ASC",
            (task_id,),
        )
        return [dict(row) for row in rows]


def _encode_response(response: ModelResponse) -> dict[str, Any]:
    usage = response.usage or UsageInfo()
    return {
        "request_id": response.request_id,
        "model": response.model,
        "provider": response.provider,
        "blocks": [_serialize_block(block) for block in response.blocks],
        "finish_reason": response.finish_reason,
        "usage": asdict(usage),
        "metadata": dict(response.metadata or {}),
    }


def _decode_response(data: Mapping[str, Any]) -> ModelResponse:
    usage_data = dict(data.get("usage") or {})
    return ModelResponse(
        request_id=str(data.get("request_id") or ""),
        model=str(data.get("model") or ""),
        provider=str(data.get("provider") or ""),
        blocks=tuple(
            _deserialize_block(dict(block))
            for block in list(data.get("blocks") or [])
            if isinstance(block, Mapping)
        ),
        finish_reason=data.get("finish_reason"),
        usage=UsageInfo(
            input_tokens=int(usage_data.get("input_tokens") or 0),
            output_tokens=int(usage_data.get("output_tokens") or 0),
            reasoning_tokens=int(usage_data.get("reasoning_tokens") or 0),
            cost_usd=usage_data.get("cost_usd"),
            cache_read_tokens=int(usage_data.get("cache_read_tokens") or 0),
            cache_write_tokens=int(usage_data.get("cache_write_tokens") or 0),
            uncached_input_tokens=usage_data.get("uncached_input_tokens"),
            provider_metadata=dict(usage_data.get("provider_metadata") or {}),
        ),
        metadata=dict(data.get("metadata") or {}),
    )


__all__ = ["ModelResponseStore"]
