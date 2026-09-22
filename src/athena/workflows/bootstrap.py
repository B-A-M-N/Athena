"""Workflow run start/resume bootstrap and identity recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.messages import utcnow
from athena.protocol.tasks import WorkspaceSpec
from athena.workflows.identity import WorkflowIdentity, canonical_hash
from athena.workflows.receipts import decode_workflow_value, workflow_receipt_outputs


async def start_workflow_run(
    store: Any,
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
    """Create or recover a run while validating its immutable identity."""
    from athena.workflows.runs import (
        WorkflowRunIdentityError,
        WorkflowRunRecoveryRequired,
        environment_fingerprint_async,
        workspace_identity,
    )

    identifier = run_id or WorkflowIdentity.new_run(workflow_id, task_id, parent_call_id)
    await store._ensure_item_table()
    current_environment = environment_identity
    if current_environment is None:
        current_environment = await environment_fingerprint_async(workspace)
    expected = WorkflowIdentity.expected(
        definition_hash=definition_hash,
        input_hash=canonical_hash(inputs),
        workspace_identity=workspace_identity(workspace),
        workspace_revision=workspace_revision,
        environment_identity=current_environment,
    )
    row = await store._db.fetch_one("SELECT * FROM workflow_runs WHERE id = ?", (identifier,))
    if row is None:
        now = utcnow().isoformat()
        await store._db.execute(
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
        raise WorkflowRunIdentityError(f"workflow run {identifier} belongs to a different workflow")
    if row.get("task_id") != task_id:
        raise WorkflowRunIdentityError(f"workflow run {identifier} belongs to a different task")
    prior_status = str(row.get("status") or "running")
    # A completed run is an immutable replay receipt. A new caller may
    # present the same explicit run_id after restart, so the original
    # parent call is no longer an identity constraint. In-progress runs
    # remain bound to their initiating call to prevent two continuations
    # from racing the same step receipt.
    if (
        parent_call_id is not None
        and str(row.get("parent_call_id") or "") != parent_call_id
        and prior_status not in {"completed", "failed", "aborted"}
    ):
        raise WorkflowRunIdentityError(
            f"workflow run {identifier} belongs to a different parent call"
        )
    if (
        parent_workflow_id is not None
        and str(row.get("parent_workflow_id") or "") != parent_workflow_id
        and prior_status not in {"completed", "failed", "aborted"}
    ):
        raise WorkflowRunIdentityError(
            f"workflow run {identifier} belongs to a different parent workflow"
        )
    try:
        WorkflowIdentity.validate(identifier, row, expected)
    except ValueError as exc:
        raise WorkflowRunIdentityError(str(exc)) from exc
    status = prior_status
    outputs = decode_workflow_value(row.get("outputs"))
    if not isinstance(outputs, dict):
        raise WorkflowRunIdentityError(f"workflow run {identifier} has malformed durable outputs")
    steps = await store._db.fetch_all(
        "SELECT * FROM workflow_step_runs WHERE run_id = ? ORDER BY step_id",
        (identifier,),
    )
    completed: set[str] = set()
    recovery = status == "recovery_required"
    normalized_steps: list[tuple[str, list[dict[str, Any]], Any]] = []
    for item in steps:
        step_id = str(item.get("step_id") or "")
        step_output = decode_workflow_value(item.get("output"))
        step_status = str(item.get("status") or "")
        legacy_records = decode_workflow_value(item.get("execution_records"))
        if not isinstance(legacy_records, list):
            raise WorkflowRunIdentityError(
                f"workflow step {step_id} has malformed execution receipts"
            )
        records = await store._receipts.records(identifier, step_id, legacy_records)
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
            and all(str(record.get("state") or "") in {"COMPLETE", "SKIPPED"} for record in records)
        ):
            # The outer step finalization may be the part interrupted.
            # Reconstruct its output from the per-item receipts so
            # dependent steps can continue without calling the capability.
            values = workflow_receipt_outputs(records)
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
                    await store._receipts.save(identifier, step_id, record)
    if recovery and status != "recovery_required":
        await store._db.execute(
            "UPDATE workflow_runs SET status = 'recovery_required', updated_at = ? WHERE id = ?",
            (utcnow().isoformat(), identifier),
        )
        status = "recovery_required"
    elif normalized_steps:
        now = utcnow().isoformat()
        for step_id, records, step_output in normalized_steps:
            await store._db.execute(
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
        await store._db.execute(
            "UPDATE workflow_runs SET outputs = ?, updated_at = ? WHERE id = ?",
            (json.dumps(outputs), now, identifier),
        )
    return identifier, outputs, completed, status
