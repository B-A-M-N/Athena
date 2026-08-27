"""Durable model-tool compatibility receipts.

Repair receipts are part of the canonical call boundary, not presentation-only
events. The raw candidate and the canonical arguments are stored together so
replay can consume the already-corrected call without applying a future repair
policy to it again.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class ToolRepairStore:
    """Append-only durable repair records keyed by provider call ID."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        task_id: str | None,
        capability_id: str,
        origin: str,
        receipt: Mapping[str, Any],
        original_arguments: Any,
        canonical_arguments: Mapping[str, Any] | None,
    ) -> str:
        """Persist one receipt idempotently, rejecting call-id collisions."""
        call_id = str(receipt.get("call_id") or "")
        if not call_id:
            raise ValueError("tool repair receipt requires call_id")
        encoded_original = json.dumps(original_arguments, sort_keys=True)
        encoded_canonical = (
            json.dumps(dict(canonical_arguments), sort_keys=True)
            if canonical_arguments is not None else None
        )
        existing = await self._db.fetch_one(
            "SELECT * FROM tool_repairs WHERE call_id = ?", (call_id,)
        )
        if existing is not None:
            if (
                existing.get("original_arguments") != encoded_original
                or existing.get("canonical_arguments") != encoded_canonical
            ):
                raise ValueError(f"tool repair call_id collision: {call_id}")
            return str(existing["id"])
        record_id = new_id("repair")
        await self._db.execute(
            "INSERT INTO tool_repairs ("
            "id, call_id, task_id, capability_id, origin, outcome, schema_hash, "
            "repair_policy_version, provider_profile_id, model_id, "
            "original_shape_hash, canonical_shape_hash, original_arguments, "
            "canonical_arguments, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                call_id,
                task_id,
                capability_id,
                origin,
                str(receipt.get("outcome") or "INVALID"),
                receipt.get("schema_hash"),
                str(receipt.get("repair_policy_version") or "unknown"),
                receipt.get("provider_profile_id"),
                receipt.get("model_id"),
                receipt.get("original_shape_hash"),
                receipt.get("repaired_shape_hash"),
                encoded_original,
                encoded_canonical,
                utcnow().isoformat(),
            ),
        )
        return record_id

    async def get(self, call_id: str) -> dict[str, Any] | None:
        """Load a receipt and decode its two argument representations."""
        row = await self._db.fetch_one(
            "SELECT * FROM tool_repairs WHERE call_id = ?", (call_id,)
        )
        if row is None:
            return None
        for key in ("original_arguments", "canonical_arguments"):
            value = row.get(key)
            if value is None:
                continue
            try:
                row[key] = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid stored tool repair {call_id}: {key}") from exc
        return row


__all__ = ["ToolRepairStore"]
