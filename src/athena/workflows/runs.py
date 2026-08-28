"""Durable workflow execution cursors for restartable declarative runs.

The run record is an execution contract, not just a progress counter. A
resume is valid only for the same workflow definition, inputs, task owner,
workspace policy/revision, and execution environment. Each step also gets a
stable call identity before the dispatcher is entered; that is the boundary
that prevents a crash after an effect from becoming a second effect on resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import asyncio
from asyncio import Lock
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from athena.execution.environment import ProjectEnvironmentFingerprint
from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.protocol.tasks import WorkspaceSpec
from athena.state.database import Database


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
        self._locks: dict[str, Lock] = {}
        # The schema is owned by the migration runner.  A few narrow callers
        # use an older in-memory facade, so absence of the table degrades to
        # the legacy JSON mirror rather than making those callers unusable.
        self._item_table: bool | None = None
        self._legacy_items_backfilled = False
        self._item_table_lock = Lock()

    async def _ensure_item_table(self) -> bool:
        if self._item_table is not None:
            return self._item_table
        async with self._item_table_lock:
            if self._item_table is not None:
                return self._item_table
            try:
                row = await self._db.fetch_one(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'workflow_step_item_runs'"
                )
            except Exception:
                self._item_table = False
                return False
            self._item_table = row is not None
            if self._item_table and not self._legacy_items_backfilled:
                await self._backfill_legacy_items()
                self._legacy_items_backfilled = True
            return self._item_table

    async def _backfill_legacy_items(self) -> None:
        """Copy pre-024 JSON receipts into the normalized table once."""
        rows = await self._db.fetch_all(
            "SELECT run_id, step_id, execution_records FROM workflow_step_runs "
            "WHERE execution_records IS NOT NULL AND execution_records != '[]'"
        )
        for row in rows:
            records = _decode(row.get("execution_records"))
            if not isinstance(records, list):
                continue
            for index, record in enumerate(records):
                if isinstance(record, Mapping):
                    record = {
                        **dict(record),
                        "item_index": int(record.get("item_index", index)),
                    }
                    suffix = hashlib.sha256(
                        f"legacy\x1f{row.get('run_id')}\x1f{row.get('step_id')}\x1f{index}".encode()
                    ).hexdigest()[:24]
                    record.setdefault("execution_id", f"legacy-workflow-exec-{suffix}")
                    record.setdefault("call_id", f"legacy-workflow-call-{suffix}")
                    await self._save_item_record(
                        str(row.get("run_id") or ""),
                        str(row.get("step_id") or ""),
                        record,
                        create_only=True,
                    )

    async def _item_records(
        self,
        run_id: str,
        step_id: str,
        fallback: list[Mapping[str, Any]] | None = None,
        *,
        db: Any | None = None,
    ) -> list[dict[str, Any]]:
        if await self._ensure_item_table():
            database = db or self._db
            fetch_all = getattr(database, "fetch_all_raw", database.fetch_all)
            rows = await fetch_all(
                "SELECT * FROM workflow_step_item_runs "
                "WHERE run_id = ? AND step_id = ? ORDER BY item_index",
                (run_id, step_id),
            )
            if rows:
                return [_decode_item_row(row) for row in rows]
        return [dict(record) for record in (fallback or ())]

    async def _save_item_record(
        self,
        run_id: str,
        step_id: str,
        record: Mapping[str, Any],
        *,
        db: Any | None = None,
        create_only: bool = False,
    ) -> None:
        if not self._item_table:
            return
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
            (
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
            ),
        )
        database = db or self._db
        execute = getattr(database, "execute_raw", database.execute) if db else database.execute
        await execute(sql, params[0])

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
        identifier = run_id or (
            workflow_run_id(workflow_id, task_id, parent_call_id)
            if parent_call_id
            else new_id("workflow-run")
        )
        await self._ensure_item_table()
        current_environment = environment_identity
        if current_environment is None:
            current_environment = await environment_fingerprint_async(workspace)
        expected = {
            "definition_hash": definition_hash or "",
            "input_hash": canonical_hash(inputs),
            "workspace_identity": workspace_identity(workspace),
            "workspace_revision": workspace_revision or "",
            "environment_identity": current_environment,
        }
        row = await self._db.fetch_one("SELECT * FROM workflow_runs WHERE id = ?", (identifier,))
        if row is None:
            now = utcnow().isoformat()
            await self._db.execute(
                "INSERT INTO workflow_runs (id, workflow_id, task_id, status, inputs, "
                "outputs, created_at, updated_at, definition_hash, input_hash, "
                "workspace_identity, workspace_revision, environment_identity, "
                "initial_environment_identity, parent_call_id, parent_workflow_id) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    workflow_id,
                    task_id,
                    json.dumps(dict(inputs)),
                    "{}",
                    now,
                    now,
                    expected["definition_hash"],
                    expected["input_hash"],
                    expected["workspace_identity"],
                    expected["workspace_revision"],
                    expected["environment_identity"],
                    expected["environment_identity"],
                    parent_call_id,
                    parent_workflow_id,
                ),
            )
            return identifier, {}, set(), "running"
        if str(row.get("workflow_id") or "") != workflow_id:
            raise WorkflowRunIdentityError(
                f"workflow run {identifier} belongs to a different workflow"
            )
        if row.get("task_id") != task_id:
            raise WorkflowRunIdentityError(f"workflow run {identifier} belongs to a different task")
        if parent_call_id is not None and str(row.get("parent_call_id") or "") != parent_call_id:
            raise WorkflowRunIdentityError(
                f"workflow run {identifier} belongs to a different parent call"
            )
        if (
            parent_workflow_id is not None
            and str(row.get("parent_workflow_id") or "") != parent_workflow_id
        ):
            raise WorkflowRunIdentityError(
                f"workflow run {identifier} belongs to a different parent workflow"
            )
        self._check_identity(identifier, row, expected)
        status = str(row.get("status") or "running")
        outputs = _decode(row.get("outputs"))
        if not isinstance(outputs, dict):
            raise WorkflowRunIdentityError(
                f"workflow run {identifier} has malformed durable outputs"
            )
        steps = await self._db.fetch_all(
            "SELECT * FROM workflow_step_runs WHERE run_id = ? ORDER BY step_id",
            (identifier,),
        )
        completed: set[str] = set()
        recovery = status == "recovery_required"
        normalized_steps: list[tuple[str, list[dict[str, Any]], Any]] = []
        for item in steps:
            step_id = str(item.get("step_id") or "")
            step_output = _decode(item.get("output"))
            step_status = str(item.get("status") or "")
            legacy_records = _decode(item.get("execution_records"))
            if not isinstance(legacy_records, list):
                raise WorkflowRunIdentityError(
                    f"workflow step {step_id} has malformed execution receipts"
                )
            records = await self._item_records(identifier, step_id, legacy_records)
            changed = False
            for index, raw_record in enumerate(records):
                if not isinstance(raw_record, Mapping):
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id} has malformed execution receipt"
                    )
                record = dict(raw_record)
                # APPLIED is the durable handoff after dispatch returned and
                # before the step row/fan-out summary is finalized. It is safe
                # to complete from that stored result; redispatching here
                # would duplicate a capability that may already have mutated.
                if str(record.get("state") or "") == "APPLIED":
                    if not bool(record.get("output_recorded")):
                        recovery = True
                    else:
                        record["state"] = "COMPLETE"
                        changed = True
                records[index] = record
            if step_status in {"completed", "skipped"}:
                completed.add(step_id)
                outputs[step_id] = step_output
            elif (
                records
                and not recovery
                and all(
                    str(record.get("state") or "") in {"COMPLETE", "SKIPPED"} for record in records
                )
            ):
                # The outer step finalization may be the part interrupted.
                # Reconstruct its output from the per-item receipts so
                # dependent steps can continue without calling the capability.
                values = _receipt_outputs(records)
                step_output = values if len(records) > 1 else values[0]
                completed.add(step_id)
                outputs[step_id] = step_output
                changed = True
            if step_status == "running" and not records:
                recovery = True
            if any(
                isinstance(record, Mapping)
                and str(record.get("state") or "")
                in {
                    "PREPARED",
                    "APPLYING",
                }
                for record in records
            ):
                recovery = True
            if changed:
                normalized_steps.append((step_id, records, step_output))
                for record in records:
                    if isinstance(record, Mapping):
                        await self._save_item_record(identifier, step_id, record)
        if recovery and status != "recovery_required":
            await self._db.execute(
                "UPDATE workflow_runs SET status = 'recovery_required', updated_at = ? "
                "WHERE id = ?",
                (utcnow().isoformat(), identifier),
            )
            status = "recovery_required"
        elif normalized_steps:
            now = utcnow().isoformat()
            for step_id, records, step_output in normalized_steps:
                await self._db.execute(
                    "UPDATE workflow_step_runs SET status = 'completed', output = ?, "
                    "execution_records = ?, state = 'COMPLETE', output_recorded = 1, "
                    "completed_at = ? "
                    "WHERE run_id = ? AND step_id = ?",
                    (
                        json.dumps(step_output),
                        json.dumps(records, sort_keys=True),
                        now,
                        identifier,
                        step_id,
                    ),
                )
            await self._db.execute(
                "UPDATE workflow_runs SET outputs = ?, updated_at = ? WHERE id = ?",
                (json.dumps(outputs), now, identifier),
            )
        return identifier, outputs, completed, status

    def _check_identity(
        self,
        run_id: str,
        row: Mapping[str, Any],
        expected: Mapping[str, str],
    ) -> None:
        for field in (
            "definition_hash",
            "input_hash",
            "workspace_identity",
            "workspace_revision",
            "environment_identity",
        ):
            actual = str(row.get(field) or "")
            wanted = str(expected.get(field) or "")
            if not actual or actual != wanted:
                raise WorkflowRunIdentityError(
                    f"workflow run {run_id} {field} does not match immutable identity"
                )

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
        lock = self._locks.setdefault(run_id, Lock())
        async with lock:
            async with _transaction(self._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                # A missing step is the normal first-dispatch case.  Only an
                # existing row with malformed receipts is a recovery failure.
                legacy_records = [] if row is None else _decode(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._item_records(run_id, step_id, legacy_records, db=db)
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
                        raise WorkflowRunIdentityError(
                            f"workflow step {step_id}[{item_index}] arguments changed"
                        )
                    state = str(existing.get("state") or "")
                    if state == "COMPLETE":
                        return {**dict(existing), "replay": True}
                    if state == "SUSPENDED":
                        return {**dict(existing), "replay": False}
                    raise WorkflowRunRecoveryRequired(
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
                    # Keep only the durable lookup key needed to reconcile an
                    # interrupted external effect.  The full arguments stay out
                    # of the receipt because they may contain credentials or
                    # other sensitive request material.
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
                await self._save_item_record(run_id, step_id, record, db=db)
                await self._touch_run(run_id, status="running", db=db)
                return record

    async def bind_nested_run(
        self,
        run_id: str,
        step_id: str,
        item_index: int,
        child_run_id: str,
    ) -> None:
        """Persist the child run that owns a nested workflow step.

        The link is written before the child starts. If approval suspends the
        child, its later continuation can wake the parent without guessing
        which workflow invocation is waiting.
        """
        await self._ensure_item_table()
        lock = self._locks.setdefault(run_id, Lock())
        async with lock:
            async with _transaction(self._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                if row is None:
                    raise WorkflowRunRecoveryRequired(f"workflow step {step_id} was not prepared")
                legacy_records = _decode(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._item_records(run_id, step_id, legacy_records, db=db)
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
                            raise WorkflowRunIdentityError(
                                f"workflow step {step_id}[{item_index}] child run changed"
                            )
                        record["nested_run_id"] = child_run_id
                    updated.append(record)
                if not found:
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id}[{item_index}] was not prepared"
                    )
                for item in updated:
                    if int(item.get("item_index", -1)) == int(item_index):
                        await self._save_item_record(run_id, step_id, item, db=db)
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
        """Persist the returned capability outcome before step aggregation.

        This is the crash-idempotence boundary. If the process stops after a
        capability has acted but before the workflow row is finalized, startup
        can complete from this receipt and must not dispatch the call again.
        """
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
        lock = self._locks.setdefault(run_id, Lock())
        async with lock:
            async with _transaction(self._db) as db:
                fetch_one = getattr(db, "fetch_one_raw", db.fetch_one)
                execute = getattr(db, "execute_raw", db.execute)
                row = await fetch_one(
                    "SELECT * FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
                if row is None:
                    raise WorkflowRunRecoveryRequired(f"workflow step {step_id} was not prepared")
                legacy_records = _decode(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._item_records(run_id, step_id, legacy_records, db=db)
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
                    raise WorkflowRunRecoveryRequired(
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
                    "completed_at = ? "
                    "WHERE run_id = ? AND step_id = ?",
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
                await self._save_item_record(run_id, step_id, top, db=db)
                if run_status is not None:
                    await execute(
                        "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE id = ?",
                        (run_status, utcnow().isoformat(), run_id),
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
        now = utcnow().isoformat()
        state = "COMPLETE" if status in {"completed", "failed", "skipped"} else status.upper()
        await self._db.execute(
            "INSERT INTO workflow_step_runs (run_id, step_id, status, output, failures, "
            "started_at, completed_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, step_id) DO UPDATE SET status=excluded.status, "
            "output=excluded.output, failures=excluded.failures, "
            "completed_at=excluded.completed_at, state=excluded.state",
            (
                run_id,
                step_id,
                status,
                json.dumps(output),
                json.dumps(list(failures)),
                now,
                now if status in {"completed", "failed", "skipped"} else None,
                state,
            ),
        )
        await self._touch_run(run_id, status="running")

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
        environment_identity = (
            await environment_fingerprint_async(workspace) if workspace is not None else None
        )
        if await self._ensure_item_table():
            rows = await self._db.fetch_all(
                "SELECT run_id, step_id, item_index FROM workflow_step_item_runs WHERE call_id = ?",
                (call_id,),
            )
            for row in rows:
                run_id = str(row.get("run_id") or "")
                step_id = str(row.get("step_id") or "")
                await self.mark_item_complete(
                    run_id,
                    step_id,
                    int(row.get("item_index", 0)),
                    output=output,
                    failures=failures,
                )
                await self._promote_item_output(run_id, step_id, output)
                if workspace_root is not None:
                    revision = await workspace_revision_async(workspace_root)
                    await self.update_workspace_revision(
                        run_id,
                        revision,
                        environment_identity=environment_identity,
                    )
                    await self._wake_parent_runs(run_id, revision, workspace=workspace)
                return True
            return False

        # Compatibility path for databases that predate migration 024.  Scan
        # the legacy JSON mirror only when the normalized table is genuinely
        # unavailable; never use substring matching as the primary lookup.
        rows = await self._db.fetch_all("SELECT * FROM workflow_step_runs")
        for row in rows:
            run_id = str(row.get("run_id") or "")
            step_id = str(row.get("step_id") or "")
            records = _decode(row.get("execution_records"))
            if not isinstance(records, list):
                continue
            for item in records:
                if not isinstance(item, Mapping) or item.get("call_id") != call_id:
                    continue
                await self.mark_item_complete(
                    run_id,
                    step_id,
                    int(item.get("item_index", 0)),
                    output=output,
                    failures=failures,
                )
                await self._promote_item_output(run_id, step_id, output)
                if workspace_root is not None:
                    revision = await workspace_revision_async(workspace_root)
                    await self.update_workspace_revision(
                        run_id,
                        revision,
                        environment_identity=environment_identity,
                    )
                    await self._wake_parent_runs(run_id, revision, workspace=workspace)
                return True
        return False

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
        """Reconcile one verified external effect into a paused workflow.

        A workflow never resumes from a caller-supplied result.  The caller
        must present the transaction receipt read from the durable external
        effect store, and only a terminal applied/verified receipt can resume
        the step.  A terminal compensation receipt resolves the run by
        failing it; it must not cause downstream steps to run.
        """
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

        await self._ensure_item_table()
        lock = self._locks.setdefault(run_id, Lock())
        async with lock:
            async with _transaction(self._db) as db:
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
                legacy_records = _decode(row.get("execution_records"))
                if not isinstance(legacy_records, list):
                    raise WorkflowRunRecoveryRequired(
                        f"workflow step {step_id} has malformed execution receipts"
                    )
                records = await self._item_records(run_id, step_id, legacy_records, db=db)
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
                        await self._save_item_record(run_id, step_id, item, db=db)
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
                outputs = _decode(run.get("outputs"))
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

    async def _wake_parent_runs(
        self,
        child_run_id: str,
        revision: str,
        *,
        workspace: WorkspaceSpec | None = None,
    ) -> None:
        """Release nested workflow parents after a child approval resolves."""
        environment_identity = (
            await environment_fingerprint_async(workspace) if workspace is not None else None
        )
        if await self._ensure_item_table():
            rows = await self._db.fetch_all(
                "SELECT DISTINCT run_id FROM workflow_step_item_runs WHERE nested_run_id = ?",
                (child_run_id,),
            )
            for row in rows:
                parent_run_id = str(row.get("run_id") or "")
                await self._touch_run(parent_run_id, status="running")
                await self.update_workspace_revision(
                    parent_run_id,
                    revision,
                    environment_identity=environment_identity,
                )
            return

        # Compatibility path for pre-024 databases.
        rows = await self._db.fetch_all(
            "SELECT * FROM workflow_step_runs",
        )
        for row in rows:
            parent_run_id = str(row.get("run_id") or "")
            records = _decode(row.get("execution_records"))
            if not isinstance(records, list):
                continue
            if any(
                isinstance(item, Mapping) and str(item.get("nested_run_id") or "") == child_run_id
                for item in records
            ):
                await self._touch_run(parent_run_id, status="running")
                await self.update_workspace_revision(
                    parent_run_id,
                    revision,
                    environment_identity=environment_identity,
                )

    async def _promote_item_output(self, run_id: str, step_id: str, output: Any) -> None:
        row = await self._db.fetch_one("SELECT outputs FROM workflow_runs WHERE id = ?", (run_id,))
        outputs = _decode((row or {}).get("outputs"))
        if not isinstance(outputs, dict):
            outputs = {}
        outputs[step_id] = output
        await self._db.execute(
            "UPDATE workflow_runs SET outputs = ?, status = 'running', updated_at = ? WHERE id = ?",
            (json.dumps(outputs), utcnow().isoformat(), run_id),
        )

    async def _touch_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        db: Any | None = None,
    ) -> None:
        database = db or self._db
        execute = getattr(database, "execute_raw", database.execute) if db else database.execute
        if status is None:
            await execute(
                "UPDATE workflow_runs SET updated_at = ? WHERE id = ?",
                (utcnow().isoformat(), run_id),
            )
        else:
            await execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow().isoformat(), run_id),
            )

    async def finish(
        self,
        run_id: str,
        *,
        status: str,
        outputs: Mapping[str, Any],
        workspace_revision: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE workflow_runs SET status = ?, outputs = ?, "
            "workspace_revision = COALESCE(?, workspace_revision), updated_at = ? "
            "WHERE id = ?",
            (
                status,
                json.dumps(dict(outputs)),
                workspace_revision,
                utcnow().isoformat(),
                run_id,
            ),
        )

    async def update_workspace_revision(
        self,
        run_id: str,
        revision: str,
        *,
        environment_identity: str | None = None,
    ) -> None:
        """Advance the run's CAS baseline after a durably completed step batch."""
        await self._db.execute(
            "UPDATE workflow_runs SET workspace_revision = ?, "
            "environment_identity = COALESCE(?, environment_identity), updated_at = ? "
            "WHERE id = ?",
            (revision, environment_identity, utcnow().isoformat(), run_id),
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        await self._ensure_item_table()
        row = await self._db.fetch_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
        if row is None:
            return None
        row["inputs"] = _decode(row.get("inputs"))
        row["outputs"] = _decode(row.get("outputs"))
        row["steps"] = await self._db.fetch_all(
            "SELECT * FROM workflow_step_runs WHERE run_id = ? ORDER BY step_id",
            (run_id,),
        )
        for step in row["steps"]:
            step["output"] = _decode(step.get("output"))
            step["failures"] = _decode(step.get("failures"))
            step["execution_records"] = _decode(step.get("execution_records"))
            if self._item_table:
                item_rows = await self._db.fetch_all(
                    "SELECT * FROM workflow_step_item_runs "
                    "WHERE run_id = ? AND step_id = ? ORDER BY item_index",
                    (run_id, step.get("step_id")),
                )
                step["items"] = [_decode_item_row(item) for item in item_rows]
        return row


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        "execution_backend": workspace.execution_backend,
        "network_policy": getattr(workspace.network_policy, "value", workspace.network_policy),
        "mutation_mode": getattr(workspace.mutation_mode, "value", workspace.mutation_mode),
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


def _decode(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _decode_item_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a normalized receipt into the same shape as the legacy mirror."""
    item = dict(row)
    item["output"] = _decode(item.get("output")) if item.get("output") is not None else None
    failures = _decode(item.get("failures"))
    item["failures"] = failures if isinstance(failures, list) else []
    item["output_recorded"] = bool(item.get("output_recorded"))
    return item


def _receipt_outputs(records: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Return receipt outputs in deterministic item order."""
    ordered = sorted(
        (record for record in records if isinstance(record, Mapping)),
        key=lambda record: int(record.get("item_index", 0)),
    )
    return [record.get("output") for record in ordered]


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
