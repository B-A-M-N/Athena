"""Durable workflow step-item transitions and nested-run binding."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from athena.protocol.messages import utcnow
from athena.workflows.receipts import WorkflowStepItemReceipts, decode_workflow_value

__all__ = ["WorkflowStepTransactions"]


class WorkflowStepTransactions:
    """Own receipt-backed step transitions beneath the workflow run store."""

    def __init__(
        self,
        *,
        db: Any,
        receipts: WorkflowStepItemReceipts,
        transaction: Callable[..., Any],
        ensure_item_table: Callable[[], Awaitable[bool]],
        lock_for_run: Callable[[str], Any],
        identity_error: type[Exception],
        recovery_error: type[Exception],
    ) -> None:
        self._db = db
        self._receipts = receipts
        self._transaction = transaction
        self._ensure_item_table = ensure_item_table
        self._lock_for_run = lock_for_run
        self._identity_error = identity_error
        self._recovery_error = recovery_error

    async def bind_nested_run(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        child_run_id: str,
    ) -> None:
        """Persist the child run that owns a nested workflow step."""
        await self._ensure_item_table()
        async with self._lock_for_run(run_id):
            async with self._transaction(self._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                if row is None:
                    raise self._recovery_error(f"workflow step {step_id} was not prepared")
                legacy_records = decode_workflow_value(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise self._recovery_error(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._receipts.records(run_id, step_id, legacy_records, db=db)
                found = False
                updated: list[dict[str, Any]] = []
                for item in records:
                    if not isinstance(item, Mapping):
                        continue
                    record = dict(item)
                    if int(record.get("item_index", -1)) == int(item_index):
                        found = True
                        previous = record.get("nested_run_id")
                        if previous is not None and str(previous) != child_run_id:
                            raise self._identity_error(
                                f"workflow step {step_id}[{item_index}] child run changed"
                            )
                        record["nested_run_id"] = child_run_id
                    updated.append(record)
                if not found:
                    raise self._recovery_error(
                        f"workflow step {step_id}[{item_index}] was not prepared"
                    )
                for item in updated:
                    if int(item.get("item_index", -1)) == int(item_index):
                        await self._receipts.save(run_id, step_id, item, db=db)
                        break
                await execute(
                    "UPDATE workflow_step_runs SET execution_records = ? "
                    "WHERE run_id = ? AND step_id = ?",
                    (json.dumps(updated, sort_keys=True), run_id, step_id),
                )

    async def mark_item_applying(self, run_id: str, step_id: str, item_index: int) -> None:
        await self._update_item(run_id, step_id, item_index, state="APPLYING")

    async def mark_item_applied(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        *,
        output: Any,
        failures: tuple[str, ...] = (),
    ) -> None:
        await self._update_item(
            run_id,
            step_id,
            item_index,
            state="APPLIED",
            output=output,
            failures=failures,
        )

    async def mark_item_complete(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        *,
        output: Any,
        failures: tuple[str, ...] = (),
    ) -> None:
        await self._update_item(
            run_id,
            step_id,
            item_index,
            state="COMPLETE",
            output=output,
            failures=failures,
        )

    async def mark_item_suspended(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        *,
        approval_id: str | None,
        continuation_id: str | None = None,
    ) -> None:
        await self._update_item(
            run_id,
            step_id,
            item_index,
            state="SUSPENDED",
            approval_id=approval_id,
            continuation_id=continuation_id,
            run_status="suspended",
        )

    async def _update_item(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        *,
        state: str,
        output: Any = None,
        failures: tuple[str, ...] = (),
        approval_id: str | None = None,
        continuation_id: str | None = None,
        run_status: str | None = None,
    ) -> None:
        await self._ensure_item_table()
        async with self._lock_for_run(run_id):
            async with self._transaction(self._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                if row is None:
                    raise self._recovery_error(f"workflow step {step_id} was not prepared")
                legacy_records = decode_workflow_value(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise self._recovery_error(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._receipts.records(run_id, step_id, legacy_records, db=db)
                found = False
                updated: list[dict[str, Any]] = []
                for item in records:
                    if not isinstance(item, Mapping):
                        continue
                    record = dict(item)
                    if int(record.get("item_index", -1)) == int(item_index):
                        found = True
                        record["state"] = state
                        if state in {"APPLIED", "COMPLETE"}:
                            record["output"] = output
                            record["failures"] = list(failures)
                            record["output_recorded"] = True
                        if approval_id is not None:
                            record["approval_id"] = approval_id
                        if continuation_id is not None:
                            record["continuation_id"] = continuation_id
                    updated.append(record)
                if not found:
                    raise self._recovery_error(
                        f"workflow step {step_id}[{item_index}] was not prepared"
                    )
                top = next(
                    (
                        item
                        for item in updated
                        if int(item.get("item_index", -1)) == int(item_index)
                    ),
                    {},
                )
                await execute(
                    "UPDATE workflow_step_runs SET execution_records = ?, state = ?, "
                    "approval_id = ?, continuation_id = ?, execution_id = ?, call_id = ?, "
                    "argument_digest = ?, capability_id = ?, output_recorded = ?, "
                    "completed_at = ? WHERE run_id = ? AND step_id = ?",
                    (
                        json.dumps(updated, sort_keys=True),
                        state,
                        top.get("approval_id"),
                        top.get("continuation_id"),
                        top.get("execution_id"),
                        top.get("call_id"),
                        top.get("argument_digest"),
                        top.get("capability_id"),
                        1 if top.get("output_recorded") else 0,
                        utcnow().isoformat() if state == "COMPLETE" else None,
                        run_id,
                        step_id,
                    ),
                )
                await self._receipts.save(run_id, step_id, top, db=db)
                if run_status is not None:
                    await execute(
                        "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE id = ?",
                        (run_status, utcnow().isoformat(), run_id),
                    )
