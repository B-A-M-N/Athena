"""Durable workflow step preparation before capability dispatch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from athena.protocol.messages import utcnow
from athena.workflows.identity import canonical_hash
from athena.workflows.receipts import WorkflowStepItemReceipts, decode_workflow_value

__all__ = ["WorkflowStepPreparation"]


class WorkflowStepPreparation:
    """Persist stable step/call identity before an effect is dispatched."""

    def __init__(
        self,
        *,
        db: Any,
        receipts: WorkflowStepItemReceipts,
        transaction: Callable[..., Any],
        ensure_item_table: Callable[[], Awaitable[bool]],
        lock_for_run: Callable[[str], Any],
        touch_run: Callable[..., Awaitable[None]],
        identity_error: type[Exception],
        recovery_error: type[Exception],
    ) -> None:
        self._db = db
        self._receipts = receipts
        self._transaction = transaction
        self._ensure_item_table = ensure_item_table
        self._lock_for_run = lock_for_run
        self._touch_run = touch_run
        self._identity_error = identity_error
        self._recovery_error = recovery_error

    async def prepare_step(
        self,
        run_id: str,
        step_id: str,
        *,
        item_index: int,
        capability_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a stable execution/call identity before dispatch."""
        await self._ensure_item_table()
        async with self._lock_for_run(run_id):
            async with self._transaction(self._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                legacy_records = (
                    [] if row is None else decode_workflow_value(row.get("execution_records"))
                )
                if not isinstance(legacy_records, list):
                    raise self._recovery_error(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._receipts.records(run_id, step_id, legacy_records, db=db)
                digest = canonical_hash(arguments)
                existing = next(
                    (
                        item
                        for item in records
                        if isinstance(item, Mapping)
                        and int(item.get("item_index", -1)) == int(item_index)
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        str(existing.get("capability_id") or "") != capability_id
                        or str(existing.get("argument_digest") or "") != digest
                    ):
                        raise self._identity_error(
                            f"workflow step {step_id}[{item_index}] arguments changed"
                        )
                    state = str(existing.get("state") or "")
                    if state == "COMPLETE":
                        return {**dict(existing), "replay": True}
                    if state == "SUSPENDED":
                        return {**dict(existing), "replay": False}
                    raise self._recovery_error(
                        f"workflow step {step_id}[{item_index}] outcome is {state or 'unknown'}"
                    )

                suffix = hashlib.sha256(
                    f"{run_id}\x1f{step_id}\x1f{item_index}".encode("utf-8")
                ).hexdigest()[:24]
                record = {
                    "item_index": int(item_index),
                    "execution_id": f"workflow-exec-{suffix}",
                    "call_id": f"workflow-call-{suffix}",
                    "argument_digest": digest,
                    "capability_id": capability_id,
                    "external_transaction_id": _external_transaction_id(
                        capability_id,
                        arguments,
                    ),
                    "state": "PREPARED",
                    "output": None,
                    "output_recorded": False,
                    "failures": [],
                    "approval_id": None,
                    "continuation_id": None,
                    "nested_run_id": None,
                }
                records.append(record)
                encoded = json.dumps(records, sort_keys=True)
                now = utcnow().isoformat()
                if row is None:
                    await execute(
                        "INSERT INTO workflow_step_runs("
                        "run_id, step_id, status, output, failures, started_at, "
                        "completed_at, execution_records, execution_id, call_id, "
                        "argument_digest, capability_id, state) "
                        "VALUES (?, ?, 'running', NULL, '[]', ?, NULL, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            step_id,
                            now,
                            encoded,
                            record["execution_id"],
                            record["call_id"],
                            digest,
                            capability_id,
                            "PREPARED",
                        ),
                    )
                else:
                    await execute(
                        "UPDATE workflow_step_runs SET status = 'running', "
                        "execution_records = ?, execution_id = ?, call_id = ?, "
                        "argument_digest = ?, capability_id = ?, state = 'PREPARED', "
                        "started_at = COALESCE(started_at, ?) WHERE run_id = ? AND step_id = ?",
                        (
                            encoded,
                            record["execution_id"],
                            record["call_id"],
                            digest,
                            capability_id,
                            now,
                            run_id,
                            step_id,
                        ),
                    )
                await self._receipts.save(run_id, step_id, record, db=db)
                await self._touch_run(run_id, status="running", db=db)
                return record


def _external_transaction_id(
    capability_id: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Persist the non-secret transaction pointer needed for recovery."""
    operation = str(arguments.get("operation") or "")
    transaction_id = str(arguments.get("transaction_id") or "").strip()
    if not transaction_id:
        return None
    if capability_id not in {"network", "service", "database"}:
        return None
    if not operation.endswith("_transaction") and not (
        (capability_id == "network" and operation == "http")
        or (capability_id == "service" and operation in _SERVICE_MUTATIONS)
        or (capability_id == "database" and operation == "execute")
    ):
        return None
    return transaction_id


_SERVICE_MUTATIONS = {
    "start",
    "stop",
    "restart",
    "reload",
    "enable",
    "disable",
    "mask",
    "unmask",
}
