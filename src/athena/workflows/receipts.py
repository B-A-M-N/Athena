"""Normalized per-item workflow receipt persistence.

This is a subordinate persistence mechanism owned by :class:`WorkflowRunStore`.
It does not mutate workflow status, authorize execution, or interpret workflow
semantics beyond decoding and storing one step-item receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "WorkflowStepItemReceipts",
    "decode_workflow_value",
    "decode_workflow_item_row",
    "workflow_receipt_outputs",
]


def decode_workflow_value(value: Any) -> Any:
    """Decode a JSON-backed workflow value using the legacy permissive shape."""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def decode_workflow_item_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a normalized receipt into the same shape as the legacy mirror."""
    item = dict(row)
    item["output"] = (
        decode_workflow_value(item.get("output")) if item.get("output") is not None else None
    )
    failures = decode_workflow_value(item.get("failures"))
    item["failures"] = failures if isinstance(failures, list) else []
    item["output_recorded"] = bool(item.get("output_recorded"))
    return item


def workflow_receipt_outputs(records: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Return receipt outputs in deterministic item order."""
    ordered = sorted(
        (record for record in records if isinstance(record, Mapping)),
        key=lambda record: int(record.get("item_index", 0)),
    )
    return [record.get("output") for record in ordered]


class WorkflowStepItemReceipts:
    """Own availability, backfill, and normalized step-item receipt I/O."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._available: bool | None = None
        self._legacy_backfilled = False
        self._available_lock = asyncio.Lock()

    async def ensure_table(self) -> bool:
        if self._available is not None:
            return self._available
        async with self._available_lock:
            if self._available is not None:
                return self._available
            try:
                row = await self._db.fetch_one(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'workflow_step_item_runs'"
                )
            except Exception:  # noqa: BLE001 - legacy facades may predate the table
                self._available = False
                return False
            self._available = row is not None
            if self._available and not self._legacy_backfilled:
                await self._backfill_legacy_items()
                self._legacy_backfilled = True
            return self._available

    async def _backfill_legacy_items(self) -> None:
        """Copy pre-024 JSON receipts into the normalized table once."""
        rows = await self._db.fetch_all(
            "SELECT run_id, step_id, execution_records FROM workflow_step_runs "
            "WHERE execution_records IS NOT NULL AND execution_records != '[]'"
        )
        for row in rows:
            records = decode_workflow_value(row.get("execution_records"))
            if not isinstance(records, list):
                continue
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    continue
                normalized = {
                    **dict(record),
                    "item_index": int(record.get("item_index", index)),
                }
                suffix = hashlib.sha256(
                    f"legacy\x1f{row.get('run_id')}\x1f{row.get('step_id')}\x1f{index}".encode()
                ).hexdigest()[:24]
                normalized.setdefault("execution_id", f"legacy-workflow-exec-{suffix}")
                normalized.setdefault("call_id", f"legacy-workflow-call-{suffix}")
                await self.save(
                    str(row.get("run_id") or ""),
                    str(row.get("step_id") or ""),
                    normalized,
                    create_only=True,
                )

    async def records(
        self,
        run_id: str,
        step_id: str,
        fallback: list[Mapping[str, Any]] | None = None,
        *,
        db: Any | None = None,
    ) -> list[dict[str, Any]]:
        if not await self.ensure_table():
            return [dict(record) for record in (fallback or ())]
        database = db or self._db
        fetch_all = getattr(database, "fetch_all_raw", database.fetch_all)
        rows = await fetch_all(
            "SELECT * FROM workflow_step_item_runs "
            "WHERE run_id = ? AND step_id = ? ORDER BY item_index",
            (run_id, step_id),
        )
        if not rows:
            return [dict(record) for record in (fallback or ())]
        return [decode_workflow_item_row(row) for row in rows]

    async def save(
        self,
        run_id: str,
        step_id: str,
        record: Mapping[str, Any],
        *,
        db: Any | None = None,
        create_only: bool = False,
    ) -> None:
        if self._available is False:
            return
        await self.ensure_table()
        item_index = int(record.get("item_index", 0))
        suffix = hashlib.sha256(
            f"receipt\x1f{run_id}\x1f{step_id}\x1f{item_index}".encode()
        ).hexdigest()[:24]
        execution_id = str(record.get("execution_id") or f"workflow-exec-{suffix}")
        call_id = str(record.get("call_id") or f"workflow-call-{suffix}")
        sql = (
            "INSERT INTO workflow_step_item_runs ("
            "run_id, step_id, item_index, execution_id, call_id, capability_id, "
            "argument_digest, state, output, failures, approval_id, continuation_id, "
            "nested_run_id, external_transaction_id, output_recorded, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            + (
                "ON CONFLICT(run_id, step_id, item_index) DO NOTHING"
                if create_only
                else "ON CONFLICT(run_id, step_id, item_index) DO UPDATE SET "
                "execution_id=excluded.execution_id, call_id=excluded.call_id, "
                "capability_id=excluded.capability_id, argument_digest=excluded.argument_digest, "
                "state=excluded.state, output=excluded.output, failures=excluded.failures, "
                "approval_id=excluded.approval_id, continuation_id=excluded.continuation_id, "
                "nested_run_id=excluded.nested_run_id, "
                "external_transaction_id=excluded.external_transaction_id, "
                "output_recorded=excluded.output_recorded, started_at=excluded.started_at, "
                "completed_at=excluded.completed_at"
            )
        )
        params = (
            run_id,
            step_id,
            item_index,
            execution_id,
            call_id,
            record.get("capability_id"),
            record.get("argument_digest"),
            str(record.get("state") or "PENDING"),
            json.dumps(record.get("output")),
            json.dumps(list(record.get("failures") or ())),
            record.get("approval_id"),
            record.get("continuation_id"),
            record.get("nested_run_id"),
            record.get("external_transaction_id"),
            1 if record.get("output_recorded") else 0,
            record.get("started_at"),
            record.get("completed_at"),
        )
        database = db or self._db
        execute = getattr(database, "execute_raw", database.execute) if db else database.execute
        await execute(sql, params)
