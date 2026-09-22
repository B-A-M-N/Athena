"""Workflow continuation reconciliation mechanics.

The run store remains the workflow identity and status authority. This
coordinator only finds a durable continuation receipt, completes its prepared
item, and wakes parent runs after the store has applied the result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from athena.protocol.tasks import WorkspaceSpec
from athena.workflows.receipts import decode_workflow_value

__all__ = ["WorkflowContinuationCoordinator"]


class WorkflowContinuationCoordinator:
    """Reconcile approved workflow continuations through one run store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def complete_call(
        self,
        call_id: str,
        *,
        output: Any = None,
        failures: tuple[str, ...] = (),
        workspace_root: str | None = None,
        workspace: WorkspaceSpec | None = None,
    ) -> bool:
        """Reconcile an approved durable continuation into its workflow step."""
        from athena.workflows.runs import environment_fingerprint_async, workspace_revision_async

        store = self._store
        environment_identity = (
            await environment_fingerprint_async(workspace) if workspace is not None else None
        )
        if await store._receipts.ensure_table():
            rows = await store._db.fetch_all(
                "SELECT run_id, step_id, item_index FROM workflow_step_item_runs WHERE call_id = ?",
                (call_id,),
            )
            for row in rows:
                run_id = str(row.get("run_id") or "")
                step_id = str(row.get("step_id") or "")
                await store.mark_item_complete(
                    run_id,
                    step_id,
                    int(row.get("item_index", 0)),
                    output=output,
                    failures=failures,
                )
                await store._promote_item_output(run_id, step_id, output)
                if workspace_root is not None:
                    revision = await workspace_revision_async(workspace_root)
                    await store.update_workspace_revision(
                        run_id,
                        revision,
                        environment_identity=environment_identity,
                    )
                    await self.wake_parent_runs(run_id, revision, workspace=workspace)
                return True
            return False

        # Compatibility path for databases that predate migration 024. Scan
        # the legacy JSON mirror only when the normalized table is unavailable.
        rows = await store._db.fetch_all("SELECT * FROM workflow_step_runs")
        for row in rows:
            run_id = str(row.get("run_id") or "")
            step_id = str(row.get("step_id") or "")
            records = decode_workflow_value(row.get("execution_records"))
            if not isinstance(records, list):
                continue
            for item in records:
                if not isinstance(item, Mapping) or item.get("call_id") != call_id:
                    continue
                await store.mark_item_complete(
                    run_id,
                    step_id,
                    int(item.get("item_index", 0)),
                    output=output,
                    failures=failures,
                )
                await store._promote_item_output(run_id, step_id, output)
                if workspace_root is not None:
                    revision = await workspace_revision_async(workspace_root)
                    await store.update_workspace_revision(
                        run_id,
                        revision,
                        environment_identity=environment_identity,
                    )
                    await self.wake_parent_runs(run_id, revision, workspace=workspace)
                return True
        return False

    async def wake_parent_runs(
        self,
        child_run_id: str,
        revision: str,
        *,
        workspace: WorkspaceSpec | None = None,
    ) -> None:
        """Release nested workflow parents after a child approval resolves."""
        from athena.workflows.runs import environment_fingerprint_async

        store = self._store
        environment_identity = (
            await environment_fingerprint_async(workspace) if workspace is not None else None
        )
        if await store._receipts.ensure_table():
            rows = await store._db.fetch_all(
                "SELECT DISTINCT run_id FROM workflow_step_item_runs WHERE nested_run_id = ?",
                (child_run_id,),
            )
            for row in rows:
                parent_run_id = str(row.get("run_id") or "")
                await store._touch_run(parent_run_id, status="running")
                await store.update_workspace_revision(
                    parent_run_id,
                    revision,
                    environment_identity=environment_identity,
                )
            return

        # Compatibility path for pre-024 databases.
        rows = await store._db.fetch_all("SELECT * FROM workflow_step_runs")
        for row in rows:
            parent_run_id = str(row.get("run_id") or "")
            records = decode_workflow_value(row.get("execution_records"))
            if not isinstance(records, list):
                continue
            if any(
                isinstance(item, Mapping) and str(item.get("nested_run_id") or "") == child_run_id
                for item in records
            ):
                await store._touch_run(parent_run_id, status="running")
                await store.update_workspace_revision(
                    parent_run_id,
                    revision,
                    environment_identity=environment_identity,
                )
