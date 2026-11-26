"""Durable workflow execution cursors for restartable declarative runs.

The run record is an execution contract, not just a progress counter. A
resume is valid only for the same workflow definition, inputs, task owner,
workspace policy/revision, and execution environment. Each step also gets a
stable call identity before the dispatcher is entered; that is the boundary
that prevents a crash after an effect from becoming a second effect on resume.
"""

from __future__ import annotations

import hashlib
import os
import platform
import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.execution.environment import ProjectEnvironmentFingerprint
from athena.protocol.tasks import WorkspaceSpec
from athena.state.database import Database
from athena.workflows.receipts import WorkflowStepItemReceipts
from athena.workflows.reconciliation import WorkflowReconciliation
from athena.workflows.bootstrap import start_workflow_run
from athena.workflows.continuations import WorkflowContinuationCoordinator
from athena.workflows.identity import canonical_hash
from athena.workflows.step_transactions import WorkflowStepTransactions
from athena.workflows.preparation import WorkflowStepPreparation
from athena.workflows.queries import WorkflowRunQuery
from athena.workflows.status import WorkflowRunStatus


@asynccontextmanager
async def _transaction(db: Any):
    """Use the database transaction when available.

    A few compatibility facades predate ``Database.transaction``. They retain
    the old best-effort behavior, while the production database and current
    test facade both provide the atomic path.
    """
    transaction = getattr(db, "transaction", None)
    if transaction is None:
        yield db
        return
    async with transaction() as active:
        yield active


class WorkflowRunIdentityError(ValueError):
    """A requested resume does not match the immutable run identity."""


class WorkflowRunRecoveryRequired(RuntimeError):
    """A step was in flight but its outcome cannot be proved from durable state."""


class WorkflowRunStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._locks = ReferenceCountedKeyedLocks()
        self._receipts = WorkflowStepItemReceipts(db)
        self._queries = WorkflowRunQuery(db, self._receipts)
        self._reconciliation = WorkflowReconciliation(self)
        self._continuations = WorkflowContinuationCoordinator(self)
        self._status = WorkflowRunStatus(db)
        self._step_transactions = WorkflowStepTransactions(
            db=db,
            receipts=self._receipts,
            transaction=_transaction,
            ensure_item_table=self._ensure_item_table,
            lock_for_run=self._lock_for_run,
            identity_error=WorkflowRunIdentityError,
            recovery_error=WorkflowRunRecoveryRequired,
        )
        self._preparation = WorkflowStepPreparation(
            db=db,
            receipts=self._receipts,
            transaction=_transaction,
            ensure_item_table=self._ensure_item_table,
            lock_for_run=self._lock_for_run,
            touch_run=self._touch_run,
            identity_error=WorkflowRunIdentityError,
            recovery_error=WorkflowRunRecoveryRequired,
        )

    async def _ensure_item_table(self) -> bool:
        """Compatibility accessor for run-owned availability checks."""
        return await self._receipts.ensure_table()

    async def start(
        self,
        *,
        workflow_id: str,
        task_id: str | None,
        inputs: Mapping[str, Any],
        run_id: str | None = None,
        definition_hash: str | None = None,
        workspace: WorkspaceSpec | None = None,
        workspace_revision: str | None = None,
        environment_identity: str | None = None,
        parent_call_id: str | None = None,
        parent_workflow_id: str | None = None,
    ) -> tuple[str, dict[str, Any], set[str], str]:
        return await start_workflow_run(
            self,
            workflow_id=workflow_id,
            task_id=task_id,
            inputs=inputs,
            run_id=run_id,
            definition_hash=definition_hash,
            workspace=workspace,
            workspace_revision=workspace_revision,
            environment_identity=environment_identity,
            parent_call_id=parent_call_id,
            parent_workflow_id=parent_workflow_id,
        )

    def _lock_for_run(self, run_id: str):
        """Own one ephemeral reference-counted lock for this run transaction."""
        return self._locks.lock(run_id)

    async def prepare_step(
        self,
        run_id: str,
        step_id: str,
        *,
        item_index: int,
        capability_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._preparation.prepare_step(
            run_id,
            step_id,
            item_index=item_index,
            capability_id=capability_id,
            arguments=arguments,
        )

    async def bind_nested_run(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        child_run_id: str,
    ) -> None:
        await self._step_transactions.bind_nested_run(
            run_id,
            step_id,
            item_index,
            child_run_id,
        )

    async def mark_item_applying(self, run_id: str, step_id: str, item_index: int) -> None:
        await self._step_transactions.mark_item_applying(run_id, step_id, item_index)

    async def mark_item_applied(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        *,
        output: Any,
        failures: tuple[str, ...] = (),
    ) -> None:
        await self._step_transactions.mark_item_applied(
            run_id,
            step_id,
            item_index,
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
        await self._step_transactions.mark_item_complete(
            run_id,
            step_id,
            item_index,
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
        await self._step_transactions.mark_item_suspended(
            run_id,
            step_id,
            item_index,
            approval_id=approval_id,
            continuation_id=continuation_id,
        )

    async def mark_step(
        self,
        run_id: str,
        step_id: str,
        *,
        status: str,
        output: Any = None,
        failures: tuple[str, ...] = (),
    ) -> None:
        await self._status.mark_step(
            run_id,
            step_id,
            status=status,
            output=output,
            failures=failures,
        )

    async def complete_call(
        self,
        call_id: str,
        *,
        output: Any = None,
        failures: tuple[str, ...] = (),
        workspace_root: str | None = None,
        workspace: WorkspaceSpec | None = None,
    ) -> bool:
        return await self._continuations.complete_call(
            call_id,
            output=output,
            failures=failures,
            workspace_root=workspace_root,
            workspace=workspace,
        )

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
        return await self._reconciliation.reconcile_external_effect(
            run_id,
            workflow_id=workflow_id,
            step_id=step_id,
            item_index=item_index,
            transaction_id=transaction_id,
            receipt=receipt,
            resolution=resolution,
        )

    async def _wake_parent_runs(
        self,
        child_run_id: str,
        revision: str,
        *,
        workspace: WorkspaceSpec | None = None,
    ) -> None:
        await self._continuations.wake_parent_runs(
            child_run_id,
            revision,
            workspace=workspace,
        )

    async def _promote_item_output(self, run_id: str, step_id: str, output: Any) -> None:
        await self._status.promote_item_output(run_id, step_id, output)

    async def _touch_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        db: Any | None = None,
    ) -> None:
        await self._status.touch_run(run_id, status=status, db=db)

    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        outputs: Mapping[str, Any],
        workspace_revision: str | None = None,
    ) -> None:
        await self._status.finish(
            run_id,
            status=status,
            outputs=outputs,
            workspace_revision=workspace_revision,
        )

    async def update_workspace_revision(
        self,
        run_id: str,
        revision: str,
        *,
        environment_identity: str | None = None,
    ) -> None:
        await self._status.update_workspace_revision(
            run_id,
            revision,
            environment_identity=environment_identity,
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return await self._queries.get(run_id)


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


def workflow_external_transaction_id(
    run_id: str,
    step_id: str,
    item_index: int,
    capability_id: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Derive a stable transaction id for legacy mutating capability calls."""
    capability_id = str(capability_id or "")
    operation = str(arguments.get("operation") or "").lower()
    if capability_id not in {"network", "service", "database"}:
        return None
    if str(arguments.get("transaction_id") or "").strip():
        return str(arguments["transaction_id"]).strip()
    mutating = (
        (
            capability_id == "network"
            and operation in {"http_transaction", "http"}
            and str(arguments.get("method") or "GET").upper() not in {"GET", "HEAD", "OPTIONS"}
        )
        or (
            capability_id == "service"
            and (operation == "service_transaction" or operation in _SERVICE_MUTATIONS)
        )
        or (capability_id == "database" and operation in {"database_transaction", "execute"})
    )
    if not mutating:
        return None
    suffix = canonical_hash(
        {
            "run_id": run_id,
            "step_id": step_id,
            "item_index": int(item_index),
            "capability_id": capability_id,
        }
    )[:32]
    return f"workflow-external-tx-{suffix}"


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


def workflow_definition_hash(workflow: Any) -> str:
    return canonical_hash(workflow.to_record())


def workspace_identity(workspace: WorkspaceSpec | None) -> str:
    if workspace is None:
        return ""
    value = {
        "id": workspace.id,
        "root": os.path.realpath(os.path.abspath(workspace.root)),
        "readable": [(rule.path, rule.allow) for rule in workspace.readable],
        "writable": [(rule.path, rule.allow) for rule in workspace.writable],
        "temp_root": workspace.temp_root,
        "execution_backend": workspace.execution_backend or "local",
        "network_policy": getattr(workspace.network_policy, "value", workspace.network_policy),
        "mutation_mode": getattr(workspace.mutation_mode, "value", workspace.mutation_mode),
        "revision": workspace.revision,
    }
    return canonical_hash(value)


def workspace_revision(root: str) -> str:
    """Compute a deterministic content revision for workflow resume checks."""
    digest = hashlib.sha256()
    root_path = Path(os.path.realpath(os.path.abspath(root)))
    if not root_path.is_dir():
        return "missing"
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__"}
    for directory, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in ignored)
        for name in sorted(filenames):
            path = Path(directory) / name
            relative = path.relative_to(root_path).as_posix()
            digest.update(relative.encode("utf-8", errors="replace"))
            try:
                if path.is_symlink():
                    digest.update(b"link:")
                    digest.update(os.readlink(path).encode("utf-8", errors="replace"))
                else:
                    digest.update(b"file:")
                    digest.update(hashlib.sha256(path.read_bytes()).digest())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


async def workspace_revision_async(root: str) -> str:
    """Compute a resume baseline from an async workflow boundary.

    The revision walk is deliberately bounded by the same ignored-directory
    set as :func:`workspace_revision`.  Keep it inline here instead of using
    the default executor: embedded runners may not start that executor, which
    would leave an otherwise healthy workflow suspended forever.
    """
    await asyncio.sleep(0)
    return workspace_revision(root)


def environment_fingerprint(workspace: WorkspaceSpec | None) -> str:
    if workspace is None:
        return canonical_hash({"platform": platform.platform()})
    return ProjectEnvironmentFingerprint().fingerprint(workspace)


async def environment_fingerprint_async(workspace: WorkspaceSpec | None) -> str:
    """Compute the environment identity without blocking the event loop."""
    if workspace is None:
        return canonical_hash({"platform": platform.platform()})
    return await ProjectEnvironmentFingerprint().fingerprint_async(workspace)


def workflow_run_id(
    workflow_id: str,
    task_id: str | None,
    parent_call_id: str,
) -> str:
    """Return the stable outer-run id for one task/call/workflow invocation."""
    suffix = canonical_hash(
        {"workflow_id": workflow_id, "task_id": task_id or "", "parent_call_id": parent_call_id}
    )[:32]
    return f"workflow-run-{suffix}"


__all__ = [
    "WorkflowRunIdentityError",
    "WorkflowRunRecoveryRequired",
    "WorkflowRunStore",
    "canonical_hash",
    "environment_fingerprint",
    "environment_fingerprint_async",
    "workflow_run_id",
    "workflow_external_transaction_id",
    "workspace_identity",
    "workspace_revision",
    "workspace_revision_async",
    "workflow_definition_hash",
]
