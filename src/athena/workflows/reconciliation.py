"""Durable external-effect reconciliation for :class:`WorkflowRunStore`.

The run store remains the workflow identity and status authority. This helper
owns only the receipt-bound transaction that resolves a paused external effect
into one prepared workflow item.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.messages import utcnow
from athena.workflows.receipts import decode_workflow_value

__all__ = ["WorkflowReconciliation"]


class WorkflowReconciliation:
    """Apply one verified external receipt through the owning run store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def reconcile_external_effect(
        self,
        run_id: str,
        *,
        workflow_id: str,
        step_id: str,
        item_index: int,
        transaction_id: str,
        receipt: Mapping[str, Any],
        resolution: str,
    ) -> dict[str, Any]:
        """Reconcile one verified external effect into a paused workflow."""
        from athena.workflows.runs import (
            WorkflowRunIdentityError,
            WorkflowRunRecoveryRequired,
            _transaction,
        )

        if resolution not in {"resume", "abort"}:
            raise ValueError("external recovery resolution must be resume or abort")
        receipt_id = str(receipt.get("transaction_id") or "")
        if receipt_id != transaction_id:
            raise WorkflowRunIdentityError(
                "external recovery receipt does not match the transaction"
            )
        receipt_status = str(receipt.get("status") or "")
        if resolution == "resume":
            if receipt_status != "VERIFIED":
                raise WorkflowRunRecoveryRequired(
                    "external receipt is not verified; workflow cannot resume"
                )
            failures: tuple[str, ...] = ()
        else:
            if receipt_status != "COMPENSATION_VERIFIED":
                raise WorkflowRunRecoveryRequired(
                    "workflow can abort only after compensation is verified"
                )
            failures = ("external effect was compensated; workflow aborted",)

        store = self._store
        await store._ensure_item_table()
        async with store._lock_for_run(run_id):
            async with _transaction(store._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                run = await fetch_one(
                    "SELECT workflow_id, task_id, status, outputs FROM workflow_runs WHERE id = ?",
                    (run_id,),
                )
                if run is None or str(run.get("workflow_id") or "") != workflow_id:
                    raise WorkflowRunIdentityError(
                        f"workflow run {run_id} does not belong to {workflow_id}"
                    )
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                if row is None:
                    raise WorkflowRunRecoveryRequired(f"workflow step {step_id} was not prepared")
                legacy_records = decode_workflow_value(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await store._receipts.records(run_id, step_id, legacy_records, db=db)
                target = next(
                    (
                        item
                        for item in records
                        if isinstance(item, Mapping)
                        and int(item.get("item_index", -1)) == int(item_index)
                    ),
                    None,
                )
                if target is None:
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id}[{item_index}] was not prepared"
                    )
                expected_transaction = str(target.get("external_transaction_id") or "")
                if not expected_transaction or expected_transaction != transaction_id:
                    raise WorkflowRunIdentityError(
                        "workflow receipt is not bound to this external transaction"
                    )
                if str(receipt.get("capability_id") or "") != str(
                    target.get("capability_id") or ""
                ) or receipt.get("task_id") != run.get("task_id"):
                    raise WorkflowRunIdentityError(
                        "external recovery receipt owner does not match the workflow"
                    )
                if str(target.get("state") or "") == "COMPLETE":
                    return {
                        "run_id": run_id,
                        "step_id": step_id,
                        "item_index": int(item_index),
                        "status": "already_reconciled",
                    }

                output = json.dumps(dict(receipt), sort_keys=True)
                updated_records = []
                for item in records:
                    if not isinstance(item, Mapping):
                        updated_records.append(item)
                        continue
                    updated = dict(item)
                    if int(updated.get("item_index", -1)) == int(item_index):
                        updated.update(
                            {
                                "state": "COMPLETE",
                                "output": output,
                                "failures": list(failures),
                                "output_recorded": True,
                                "reconciled_external_status": receipt_status,
                            }
                        )
                    updated_records.append(updated)
                for item in updated_records:
                    if isinstance(item, Mapping) and int(item.get("item_index", -1)) == int(
                        item_index
                    ):
                        await store._receipts.save(run_id, step_id, item, db=db)
                        break
                now = utcnow().isoformat()
                await execute(
                    "UPDATE workflow_step_runs SET execution_records = ?, state = 'COMPLETE', "
                    "output = ?, failures = ?, output_recorded = 1, completed_at = ? "
                    "WHERE run_id = ? AND step_id = ?",
                    (
                        json.dumps(updated_records, sort_keys=True),
                        output,
                        json.dumps(list(failures)),
                        now,
                        run_id,
                        step_id,
                    ),
                )
                outputs = decode_workflow_value(run.get("outputs"))
                if not isinstance(outputs, dict):
                    outputs = {}
                outputs[step_id] = output
                await execute(
                    "UPDATE workflow_runs SET status = ?, outputs = ?, updated_at = ? WHERE id = ?",
                    (
                        "running" if resolution == "resume" else "failed",
                        json.dumps(outputs),
                        now,
                        run_id,
                    ),
                )
                return {
                    "run_id": run_id,
                    "step_id": step_id,
                    "item_index": int(item_index),
                    "status": "resumed" if resolution == "resume" else "aborted",
                    "external_status": receipt_status,
                }
