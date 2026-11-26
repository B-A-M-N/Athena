"""Read-only durable workflow-run projection."""

from __future__ import annotations

from typing import Any

from athena.workflows.receipts import (
    WorkflowStepItemReceipts,
    decode_workflow_item_row,
    decode_workflow_value,
)

__all__ = ["WorkflowRunQuery"]


class WorkflowRunQuery:
    """Project one complete workflow run without owning workflow mutation."""

    def __init__(self, db: Any, receipts: WorkflowStepItemReceipts) -> None:
        self._db = db
        self._receipts = receipts

    async def get(self, run_id: str) -> dict[str, Any] | None:
        await self._receipts.ensure_table()
        row = await self._db.fetch_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if row is None:
            return None
        row["inputs"] = decode_workflow_value(row.get("inputs"))
        row["outputs"] = decode_workflow_value(row.get("outputs"))
        row["steps"] = await self._db.fetch_all(
            "SELECT * FROM workflow_step_runs WHERE run_id = ? ORDER BY step_id",
            (run_id,),
        )
        for step in row["steps"]:
            step["output"] = decode_workflow_value(step.get("output"))
            step["failures"] = decode_workflow_value(step.get("failures"))
            step["execution_records"] = decode_workflow_value(step.get("execution_records"))
            if await self._receipts.ensure_table():
                item_rows = await self._db.fetch_all(
                    "SELECT * FROM workflow_step_item_runs "
                    "WHERE run_id = ? AND step_id = ? ORDER BY item_index",
                    (run_id, step.get("step_id")),
                )
                step["items"] = [decode_workflow_item_row(item) for item in item_rows]
        return row
