from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from athena.capabilities.dispatcher import SuspendedCall
from athena.kernel.dispatch import DispatchResult
from athena.kernel.kernel import AgentKernel
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    DispatchDirectives,
)
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec
from athena.workflows import Workflow, WorkflowExecutor, WorkflowStep
from athena.workflows.runs import (
    WorkflowRunIdentityError,
    WorkflowRunRecoveryRequired,
    WorkflowRunStore,
    workflow_definition_hash,
)


class _FastDatabase:
    """Synchronous SQLite facade for workflow receipt tests.

    The production Database is intentionally exercised by service tests. These
    focused workflow tests use a tiny native-SQLite facade so they can verify
    receipt semantics without depending on the host's aiosqlite worker
    thread/runtime configuration.
    """

    def __init__(self) -> None:
        self._connection = sqlite3.connect(":memory:")
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            CREATE TABLE workflow_runs (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT,
                status TEXT NOT NULL, inputs TEXT NOT NULL,
                outputs TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, definition_hash TEXT,
                input_hash TEXT, workspace_identity TEXT,
                workspace_revision TEXT, environment_identity TEXT,
                initial_environment_identity TEXT, parent_call_id TEXT,
                parent_workflow_id TEXT
            );
            CREATE TABLE workflow_step_runs (
                run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                status TEXT NOT NULL, output TEXT,
                failures TEXT NOT NULL DEFAULT '[]', started_at TEXT,
                completed_at TEXT, execution_records TEXT NOT NULL DEFAULT '[]',
                execution_id TEXT, call_id TEXT, argument_digest TEXT,
                capability_id TEXT, state TEXT NOT NULL DEFAULT 'PENDING',
                approval_id TEXT, continuation_id TEXT,
                output_recorded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, step_id)
            );
            CREATE TABLE workflow_step_item_runs (
                run_id TEXT NOT NULL, step_id TEXT NOT NULL,
                item_index INTEGER NOT NULL, execution_id TEXT NOT NULL,
                call_id TEXT NOT NULL, capability_id TEXT,
                argument_digest TEXT, state TEXT NOT NULL, output TEXT,
                failures TEXT NOT NULL DEFAULT '[]', approval_id TEXT,
                continuation_id TEXT, nested_run_id TEXT,
                external_transaction_id TEXT,
                output_recorded INTEGER NOT NULL DEFAULT 0,
                started_at TEXT, completed_at TEXT,
                PRIMARY KEY (run_id, step_id, item_index)
            );
            CREATE UNIQUE INDEX idx_test_workflow_item_call
                ON workflow_step_item_runs(call_id);
            CREATE UNIQUE INDEX idx_test_workflow_item_execution
                ON workflow_step_item_runs(execution_id);
            """
        )

    async def execute(self, sql, params=()):
        cursor = self._connection.execute(sql, params)
        self._connection.commit()
        return cursor

    async def execute_raw(self, sql, params=()):
        return self._connection.execute(sql, params)

    async def fetch_one(self, sql, params=()):
        row = self._connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def fetch_all(self, sql, params=()):
        return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

    async def fetch_one_raw(self, sql, params=()):
        row = self._connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    async def fetch_all_raw(self, sql, params=()):
        return [dict(row) for row in self._connection.execute(sql, params).fetchall()]

    @asynccontextmanager
    async def transaction(self):
        self._connection.execute("BEGIN")
        try:
            yield self
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise


class _Dispatcher:
    def __init__(self) -> None:
        self.requests: list[CapabilityRequest] = []
        self.dispatch_kwargs: list[dict] = []

    async def dispatch(self, request, **kwargs):
        self.requests.append(request)
        self.dispatch_kwargs.append(kwargs)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output=json.dumps(request.arguments),
        )


class _SlowDispatcher(_Dispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def dispatch(self, request, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return await super().dispatch(request, **kwargs)
        finally:
            self.active -= 1


class _SuspendingDispatcher(_Dispatcher):
    async def dispatch(self, request, **kwargs):
        self.requests.append(request)
        self.dispatch_kwargs.append(kwargs)
        return SuspendedCall(
            request.call_id,
            request,
            SimpleNamespace(),
            "approval-1",
        )


class _RecoveryDispatcher(_Dispatcher):
    async def dispatch(self, request, **kwargs):
        self.requests.append(request)
        self.dispatch_kwargs.append(kwargs)
        raise WorkflowRunRecoveryRequired("external outcome is unknown")


class _ExternalFailureDispatcher(_Dispatcher):
    async def dispatch(self, request, **kwargs):
        self.requests.append(request)
        self.dispatch_kwargs.append(kwargs)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.FAILED,
            error="external request outcome is uncertain; recovery required",
            metadata={
                "external_effect": {
                    "transaction_id": "external-tx-1",
                    "task_id": request.task_id,
                    "capability_id": request.capability_id,
                    "status": "RECOVERY_REQUIRED",
                    "response": {},
                },
            },
        )


def _resolver(identifier):
    if identifier == "echo":
        return CapabilityDescriptor(id="echo", description="echo", input_schema={"type": "object"})
    raise KeyError(identifier)


def _nested_recovery_resolver(identifier):
    if identifier == "child":
        return Workflow.create(
            name="child",
            description="child recovery",
            steps=(WorkflowStep(id="write", capability_id="echo"),),
        )
    if identifier == "echo":
        return _resolver(identifier)
    raise KeyError(identifier)


async def test_workflow_supports_conditions_and_bounded_foreach(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="batch",
        description="batch echo",
        steps=(
            WorkflowStep(
                id="echoes",
                capability_id="echo",
                arguments={"value": "$item"},
                if_condition="$run == true",
                foreach="$items",
            ),
        ),
    )
    result = await WorkflowExecutor(
        dispatcher,
        resolver=_resolver,
    ).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"run": True, "items": ["a", "b"]},
    )

    assert result.status == "completed"
    assert [call.arguments["value"] for call in dispatcher.requests] == [
        "a",
        "b",
    ]
    assert result.outputs["echoes"] == [
        '{"value": "a"}',
        '{"value": "b"}',
    ]
    assert {call.session_id for call in dispatcher.requests} == {None}


async def test_workflow_parallel_foreach_is_bounded_and_keeps_output_order(tmp_path):
    dispatcher = _SlowDispatcher()
    workflow = Workflow.create(
        name="parallel-batch",
        description="parallel batch echo",
        steps=(
            WorkflowStep(
                id="echoes",
                capability_id="echo",
                arguments={"value": "$item"},
                foreach="$items",
                parallel=True,
                max_parallel=2,
            ),
        ),
    )

    result = await WorkflowExecutor(dispatcher, resolver=_resolver).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        task_budget=ResourceBudget(max_parallel_executions=3),
        inputs={"items": ["a", "b", "c", "d"]},
    )

    assert result.status == "completed"
    assert dispatcher.max_active == 2
    assert [json.loads(value)["value"] for value in result.outputs["echoes"]] == [
        "a",
        "b",
        "c",
        "d",
    ]
    assert dispatcher.dispatch_kwargs
    assert all(
        kwargs["task_budget"] == ResourceBudget(max_parallel_executions=3)
        for kwargs in dispatcher.dispatch_kwargs
    )


async def test_workflow_propagates_session_scope_to_capability_calls(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="session-aware",
        description="session-aware",
        steps=(WorkflowStep(id="echo", capability_id="echo"),),
    )

    result = await WorkflowExecutor(dispatcher, resolver=_resolver).run(
        workflow,
        task_id="task-1",
        session_id="session-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status == "completed"
    assert dispatcher.requests[0].session_id == "session-1"


async def test_workflow_propagates_task_capability_policy_to_steps(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="policy-aware",
        description="policy-aware",
        steps=(WorkflowStep(id="echo", capability_id="echo"),),
    )
    policy = CapabilityPolicy(allow=("echo",))

    result = await WorkflowExecutor(dispatcher, resolver=_resolver).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        task_policy=policy,
    )

    assert result.status == "completed"
    assert dispatcher.dispatch_kwargs[0]["task_policy"] == policy


async def test_workflow_rejects_unbounded_foreach(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="bounded",
        description="bounded",
        steps=(
            WorkflowStep(
                id="echoes",
                capability_id="echo",
                arguments={"value": "$item"},
                foreach="$items",
                max_iterations=1,
            ),
        ),
    )
    result = await WorkflowExecutor(
        dispatcher,
        resolver=_resolver,
    ).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"items": ["a", "b"]},
    )
    assert result.status == "failed"
    assert "max_iterations" in result.failures[0]
    assert dispatcher.requests == []


async def test_workflow_runs_independent_ready_steps_and_honors_dependencies(tmp_path):
    dispatcher = _SlowDispatcher()
    workflow = Workflow.create(
        name="dag",
        description="dag",
        steps=(
            WorkflowStep(id="a", capability_id="echo", arguments={"value": "a"}),
            WorkflowStep(id="b", capability_id="echo", arguments={"value": "b"}),
            WorkflowStep(
                id="c",
                capability_id="echo",
                arguments={"value": "$outputs.a"},
                depends_on=("a", "b"),
            ),
        ),
    )
    result = await WorkflowExecutor(dispatcher, resolver=_resolver).run(
        workflow,
        task_id="task-dag",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )
    assert result.status == "completed"
    assert dispatcher.max_active == 2
    assert dispatcher.requests[-1].arguments["value"] == '{"value": "a"}'


async def test_workflow_validates_dynamic_input_contract_before_steps(tmp_path):
    dispatcher = _Dispatcher()
    workflow = Workflow.create(
        name="requires-target",
        description="requires a target",
        input_schema={
            "type": "object",
            "required": ["target"],
            "properties": {"target": {"type": "string"}},
            "additionalProperties": False,
        },
        steps=(WorkflowStep(id="echo", capability_id="echo", arguments={"value": "$target"}),),
    )

    result = await WorkflowExecutor(
        dispatcher,
        resolver=_resolver,
    ).run(
        workflow,
        task_id="task-1",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={},
    )

    assert result.status == "invalid"
    assert "target" in result.failures[0]
    assert dispatcher.requests == []


async def test_workflow_resume_is_bound_to_definition_inputs_and_owner(tmp_path):
    dispatcher = _Dispatcher()
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    workflow = Workflow.create(
        name="durable",
        description="durable echo",
        steps=(WorkflowStep(id="echo", capability_id="echo", arguments={"value": "$value"}),),
        input_schema={"type": "object", "required": ["value"]},
    )
    executor = WorkflowExecutor(dispatcher, resolver=_resolver, run_store=store)

    first = await executor.run(
        workflow,
        task_id="task-owner",
        run_id="run-identity",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"value": "one"},
    )
    assert first.status == "completed"
    assert len(dispatcher.requests) == 1

    resumed = await executor.run(
        workflow,
        task_id="task-owner",
        run_id="run-identity",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"value": "one"},
    )
    assert resumed.status == "completed"
    assert len(dispatcher.requests) == 1

    with pytest.raises(WorkflowRunIdentityError, match="input_hash"):
        await executor.run(
            workflow,
            task_id="task-owner",
            run_id="run-identity",
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
            inputs={"value": "two"},
        )

    changed = replace(
        workflow,
        description="changed definition",
        steps=(
            WorkflowStep(
                id="echo",
                capability_id="echo",
                arguments={"value": "$value", "changed": True},
            ),
        ),
    )
    assert workflow_definition_hash(changed) != workflow_definition_hash(workflow)
    with pytest.raises(WorkflowRunIdentityError, match="definition_hash"):
        await executor.run(
            changed,
            task_id="task-owner",
            run_id="run-identity",
            workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
            inputs={"value": "one"},
        )


async def test_workflow_inflight_step_requires_recovery_instead_of_redispatch(tmp_path):
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    run_id, _, _, _ = await store.start(
        workflow_id="workflow-1",
        task_id="task-1",
        inputs={},
        run_id="run-inflight",
        definition_hash="definition",
        workspace=workspace,
        workspace_revision="revision",
        environment_identity="environment",
    )
    await store.prepare_step(
        run_id,
        "write",
        item_index=0,
        capability_id="echo",
        arguments={"value": "side effect"},
    )

    _, _, _, status = await store.start(
        workflow_id="workflow-1",
        task_id="task-1",
        inputs={},
        run_id="run-inflight",
        definition_hash="definition",
        workspace=workspace,
        workspace_revision="revision",
        environment_identity="environment",
    )
    assert status == "recovery_required"


async def test_workflow_item_receipt_is_normalized_and_call_indexed(tmp_path):
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    run_id, _, _, _ = await store.start(
        workflow_id="workflow-1",
        task_id="task-1",
        inputs={},
        run_id="run-item-index",
        definition_hash="definition",
        workspace=workspace,
        workspace_revision="revision",
        environment_identity="environment",
    )
    record = await store.prepare_step(
        run_id,
        "write",
        item_index=0,
        capability_id="echo",
        arguments={"value": "indexed"},
    )

    item = await db.fetch_one(
        "SELECT run_id, step_id, item_index, call_id, execution_id, state "
        "FROM workflow_step_item_runs WHERE call_id = ?",
        (record["call_id"],),
    )
    assert item == {
        "run_id": run_id,
        "step_id": "write",
        "item_index": 0,
        "call_id": record["call_id"],
        "execution_id": record["execution_id"],
        "state": "PREPARED",
    }
    assert await store.complete_call(record["call_id"], output="indexed")
    completed = await db.fetch_one(
        "SELECT state, output, output_recorded FROM workflow_step_item_runs WHERE call_id = ?",
        (record["call_id"],),
    )
    assert completed["state"] == "COMPLETE"
    assert json.loads(completed["output"]) == "indexed"
    assert completed["output_recorded"] == 1


async def test_external_failure_keeps_workflow_item_recoverable(tmp_path):
    dispatcher = _ExternalFailureDispatcher()
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    workflow = Workflow.create(
        name="external-recovery",
        description="retain unknown external outcome",
        steps=(
            WorkflowStep(
                id="write",
                capability_id="network",
                arguments={
                    "operation": "http_transaction",
                    "phase": "apply",
                    "transaction_id": "external-tx-1",
                },
            ),
        ),
    )

    def resolver(identifier):
        if identifier == "network":
            return CapabilityDescriptor(
                id="network",
                description="network",
                input_schema={"type": "object"},
                operation_effects={"http_transaction": frozenset()},
            )
        raise KeyError(identifier)

    outcome = await WorkflowExecutor(
        dispatcher,
        resolver=resolver,
        run_store=store,
    ).run(
        workflow,
        task_id="task-external",
        run_id="run-external",
        workspace=workspace,
    )

    assert outcome.status == "recovery_required"
    row = await db.fetch_one(
        "SELECT execution_records FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
        ("run-external", "write"),
    )
    record = json.loads(row["execution_records"])[0]
    assert record["state"] == "APPLYING"
    assert record["external_transaction_id"] == "external-tx-1"


async def test_verified_external_receipt_resumes_workflow_without_redispatch(tmp_path):
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    run_id, _, _, _ = await store.start(
        workflow_id="workflow-external",
        task_id="task-external",
        inputs={},
        run_id="run-external-verified",
        definition_hash="definition",
        workspace=workspace,
        workspace_revision="revision",
        environment_identity="environment",
    )
    await store.prepare_step(
        run_id,
        "write",
        item_index=0,
        capability_id="network",
        arguments={
            "operation": "http_transaction",
            "phase": "apply",
            "transaction_id": "external-tx-2",
        },
    )
    await store.mark_item_applying(run_id, "write", 0)
    reconciled = await store.reconcile_external_effect(
        run_id,
        workflow_id="workflow-external",
        step_id="write",
        item_index=0,
        transaction_id="external-tx-2",
        receipt={
            "transaction_id": "external-tx-2",
            "task_id": "task-external",
            "capability_id": "network",
            "status": "VERIFIED",
            "phase": "verify",
            "response": {"verified": True},
        },
        resolution="resume",
    )

    assert reconciled["status"] == "resumed"
    _, outputs, completed, status = await store.start(
        workflow_id="workflow-external",
        task_id="task-external",
        inputs={},
        run_id=run_id,
        definition_hash="definition",
        workspace=workspace,
        workspace_revision="revision",
        environment_identity="environment",
    )
    assert status == "running"
    assert completed == {"write"}
    assert json.loads(outputs["write"])["status"] == "VERIFIED"


async def test_workflow_reconciles_applied_receipt_without_redispatch(tmp_path):
    dispatcher = _Dispatcher()
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    workflow = Workflow.create(
        name="applied-receipt",
        description="resume an applied step",
        steps=(
            WorkflowStep(
                id="write",
                capability_id="echo",
                arguments={"value": "done"},
            ),
        ),
    )
    executor = WorkflowExecutor(dispatcher, resolver=_resolver, run_store=store)

    first = await executor.run(
        workflow,
        task_id="task-applied",
        run_id="run-applied",
        workspace=workspace,
    )
    assert first.status == "completed"
    assert len(dispatcher.requests) == 1

    # Recreate the narrow crash window in the normalized receipt: the
    # capability result is durable, but the outer step aggregation/finalization
    # has not happened yet. Leave the legacy mirror at its old COMPLETE value
    # so a restart also proves that migration-024 backfill is create-only.
    await db.execute(
        "UPDATE workflow_step_item_runs SET state = 'APPLIED', output = ?, "
        "output_recorded = 1 WHERE run_id = ? AND step_id = ? AND item_index = 0",
        (json.dumps("known-result"), "run-applied", "write"),
    )
    await db.execute(
        "UPDATE workflow_step_runs SET status = 'running', state = 'APPLIED', "
        "output = NULL WHERE run_id = ? AND step_id = ?",
        ("run-applied", "write"),
    )
    await db.execute(
        "UPDATE workflow_runs SET status = 'running', outputs = '{}' WHERE id = ?",
        ("run-applied",),
    )

    restarted_store = WorkflowRunStore(db)
    resumed = await WorkflowExecutor(dispatcher, resolver=_resolver, run_store=restarted_store).run(
        workflow,
        task_id="task-applied",
        run_id="run-applied",
        workspace=workspace,
    )
    assert resumed.status == "completed"
    assert resumed.outputs["write"] == "known-result"
    assert len(dispatcher.requests) == 1


@pytest.mark.parametrize("parallel", [False, True])
async def test_workflow_recovery_required_is_not_continue_on_error(
    tmp_path,
    parallel,
):
    dispatcher = _RecoveryDispatcher()
    workflow = Workflow.create(
        name="recovery",
        description="unknown effect",
        steps=(
            WorkflowStep(
                id="writes",
                capability_id="echo",
                arguments={"value": "$item"},
                foreach="$items",
                parallel=parallel,
                continue_on_error=True,
            ),
            WorkflowStep(
                id="downstream",
                capability_id="echo",
                arguments={"value": "must-not-run"},
                depends_on=("writes",),
            ),
        ),
    )

    result = await WorkflowExecutor(
        dispatcher,
        resolver=_resolver,
    ).run(
        workflow,
        task_id="task-recovery",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        inputs={"items": ["a", "b"]},
    )

    assert result.status == "recovery_required", result.failures
    requested_values = [request.arguments["value"] for request in dispatcher.requests]
    assert requested_values[0] == "a"
    if parallel:
        assert requested_values == ["a", "b"]
    else:
        assert requested_values == ["a"]
    assert all(request.arguments.get("value") != "must-not-run" for request in dispatcher.requests)


async def test_workflow_recovery_required_item_is_not_marked_complete(tmp_path):
    dispatcher = _RecoveryDispatcher()
    store = WorkflowRunStore(_FastDatabase())
    workflow = Workflow.create(
        name="durable-recovery",
        description="unknown effect",
        steps=(
            WorkflowStep(
                id="write",
                capability_id="echo",
                arguments={"value": "x"},
                continue_on_error=True,
            ),
        ),
    )
    result = await WorkflowExecutor(
        dispatcher,
        resolver=_resolver,
        run_store=store,
    ).run(
        workflow,
        task_id="task-recovery",
        run_id="run-recovery",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status == "recovery_required", result.failures
    row = await store._db.fetch_one(  # noqa: SLF001 - focused receipt assertion
        "SELECT execution_records FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
        ("run-recovery", "write"),
    )
    records = json.loads(row["execution_records"])
    assert records[0]["state"] == "APPLYING"


async def test_nested_workflow_recovery_required_does_not_complete_parent_item(tmp_path):
    dispatcher = _RecoveryDispatcher()
    store = WorkflowRunStore(_FastDatabase())
    workflow = Workflow.create(
        name="nested-recovery",
        description="unknown child effect",
        steps=(WorkflowStep(id="child", workflow_id="child"),),
    )
    result = await WorkflowExecutor(
        dispatcher,
        resolver=_nested_recovery_resolver,
        run_store=store,
    ).run(
        workflow,
        task_id="task-nested-recovery",
        run_id="run-nested-recovery",
        workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
    )

    assert result.status == "recovery_required", result.failures
    row = await store._db.fetch_one(  # noqa: SLF001 - focused receipt assertion
        "SELECT execution_records FROM workflow_step_runs WHERE run_id = ? AND step_id = ?",
        ("run-nested-recovery", "child"),
    )
    records = json.loads(row["execution_records"])
    assert records[0]["state"] == "PREPARED"
    assert records[0]["state"] != "COMPLETE"


async def test_suspended_workflow_does_not_redispatch_before_approval(tmp_path):
    dispatcher = _SuspendingDispatcher()
    store = WorkflowRunStore(_FastDatabase())
    workflow = Workflow.create(
        name="approval",
        description="approval-gated echo",
        steps=(WorkflowStep(id="echo", capability_id="echo"),),
    )
    executor = WorkflowExecutor(dispatcher, resolver=_resolver, run_store=store)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))

    first = await executor.run(
        workflow,
        task_id="task-approval",
        run_id="run-approval",
        workspace=workspace,
    )
    assert first.status == "suspended"
    assert len(dispatcher.requests) == 1

    resumed_too_early = await executor.run(
        workflow,
        task_id="task-approval",
        run_id="run-approval",
        workspace=workspace,
    )
    assert resumed_too_early.status == "suspended"
    assert resumed_too_early.suspended is None
    assert len(dispatcher.requests) == 1


async def test_nested_workflow_approval_wakes_parent_and_resumes_once(tmp_path):
    dispatcher = _SuspendingDispatcher()
    db = _FastDatabase()
    store = WorkflowRunStore(db)
    nested = Workflow.create(
        name="nested-approval",
        description="nested approval",
        steps=(WorkflowStep(id="echo", capability_id="echo"),),
    )
    outer = Workflow.create(
        name="outer-approval",
        description="outer approval",
        steps=(WorkflowStep(id="nested", workflow_id=nested.id),),
    )

    def resolver(identifier):
        if identifier == nested.id:
            return nested
        return _resolver(identifier)

    executor = WorkflowExecutor(dispatcher, resolver=resolver, run_store=store)
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    first = await executor.run(
        outer,
        task_id="task-nested",
        run_id="run-nested",
        workspace=workspace,
    )
    assert first.status == "suspended"
    assert len(dispatcher.requests) == 1

    # This is the reconciliation the kernel performs after the canonical
    # approval continuation completes. It must release both the child and its
    # parent run without issuing a second capability call.
    assert await store.complete_call(
        dispatcher.requests[0].call_id,
        output="approved",
        workspace_root=str(tmp_path),
    )
    parent = await store.get("run-nested")
    assert parent["status"] == "running"

    resumed = await executor.run(
        outer,
        task_id="task-nested",
        run_id="run-nested",
        workspace=workspace,
    )
    assert resumed.status == "completed"
    assert resumed.outputs["nested"] == {"echo": "approved"}
    assert len(dispatcher.requests) == 1


async def test_same_process_approval_replay_keeps_workflow_identity(tmp_path):
    class _ReplayDispatcher:
        def __init__(self):
            self.directives = None

        async def dispatch_many(self, requests, **kwargs):
            self.directives = kwargs["_directives_by_call_id"]
            return [
                CapabilityResult(
                    requests[0].call_id,
                    requests[0].capability_id,
                    CapabilityResultStatus.OK,
                    output="approved",
                )
            ]

    class _RunStore:
        def __init__(self):
            self.completed = []

        async def complete_call(self, call_id, *, output, failures, workspace_root=None):
            self.completed.append((call_id, output, failures))
            return True

    replay = _ReplayDispatcher()
    run_store = _RunStore()
    shim = SimpleNamespace(
        _dispatcher=replay,
        _workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        _profile=None,
    )
    task = SimpleNamespace(id="workflow-task", capability_policy=None)
    state = SimpleNamespace(cancel=asyncio.Event())
    kernel = SimpleNamespace(
        _resume={task.id: asyncio.Event()},
        _resume_decision={task.id: "granted"},
        _dispatch_factory=lambda _task: shim,
        _workflow_run_store=run_store,
        _continuation_store=None,
    )

    async def _transition(_task, _status):
        return None

    async def _emit(*_args):
        return None

    async def _append_results(_task, _results):
        return None

    async def _mark_continuations_consumed(_suspended):
        return None

    async def _reconcile(suspended_call, result, *, workspace_root=None):
        await AgentKernel._reconcile_workflow_suspended(
            kernel,
            suspended_call,
            result,
            workspace_root=workspace_root,
        )

    kernel._transition = _transition
    kernel._emit = _emit
    kernel._append_results = _append_results
    kernel._mark_continuations_consumed = _mark_continuations_consumed
    kernel._reconcile_workflow_suspended = _reconcile

    directives = DispatchDirectives(
        workflow_run_id="run-1",
        workflow_step_id="write",
        workflow_item_index=0,
        workflow_execution_id="execution-1",
    )
    suspended = SuspendedCall(
        "workflow-call-1",
        CapabilityRequest(
            capability_id="fs",
            task_id=task.id,
            call_id="workflow-call-1",
            arguments={"operation": "write"},
        ),
        SimpleNamespace(),
        "approval-1",
        directives,
    )

    async def _grant():
        await asyncio.sleep(0)
        kernel._resume[task.id].set()

    asyncio.create_task(_grant())
    await AgentKernel._approval_path(
        kernel,
        task,
        state,
        DispatchResult(suspended=(suspended,)),
    )

    assert replay.directives == {"workflow-call-1": directives}
    assert run_store.completed == [("workflow-call-1", "approved", ())]


@pytest.mark.asyncio
async def test_workflow_approval_resolves_outer_call_after_child_result(tmp_path):
    """An approved child step must not strand the model's workflow call."""

    class _ReplayDispatcher:
        def __init__(self):
            self.outer = None

        async def dispatch_many(self, requests, **kwargs):
            del kwargs
            return [
                CapabilityResult(
                    requests[0].call_id,
                    requests[0].capability_id,
                    CapabilityResultStatus.OK,
                    output="approved",
                )
            ]

        async def dispatch(self, request, **kwargs):
            del kwargs
            self.outer = request
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output=json.dumps({"workflow_id": "workflow-1", "status": "completed"}),
            )

    class _RunStore:
        async def complete_call(self, *args, **kwargs):
            del args, kwargs
            return True

    replay = _ReplayDispatcher()
    run_store = _RunStore()
    shim = SimpleNamespace(
        _dispatcher=replay,
        _workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        _profile=None,
    )
    task = SimpleNamespace(
        id="workflow-task",
        session_id="session-1",
        capability_policy=None,
        resource_budget=None,
    )
    state = SimpleNamespace(cancel=asyncio.Event())
    kernel = SimpleNamespace(
        _resume={task.id: asyncio.Event()},
        _resume_decision={task.id: "granted"},
        _dispatch_factory=lambda _task: shim,
        _workflow_run_store=run_store,
        _continuation_store=None,
    )

    async def _transition(_task, _status):
        return None

    async def _emit(*_args):
        return None

    appended = []

    async def _append_results(_task, results):
        appended.extend(results)

    async def _mark_continuations_consumed(_suspended):
        return None

    kernel._transition = _transition
    kernel._emit = _emit
    kernel._append_results = _append_results
    kernel._mark_continuations_consumed = _mark_continuations_consumed
    kernel._reconcile_workflow_suspended = lambda suspended_call, result, workspace_root=None: (
        AgentKernel._reconcile_workflow_suspended(
            kernel,
            suspended_call,
            result,
            workspace_root=workspace_root,
        )
    )
    kernel._resume_workflow_parent = lambda *args, **kwargs: AgentKernel._resume_workflow_parent(
        kernel,
        *args,
        **kwargs,
    )

    suspended = SuspendedCall(
        "workflow-child-call",
        CapabilityRequest(
            capability_id="fs",
            task_id=task.id,
            call_id="workflow-child-call",
            arguments={"operation": "write"},
        ),
        SimpleNamespace(),
        "approval-1",
        DispatchDirectives(
            workflow_run_id="run-1",
            workflow_step_id="write",
            workflow_item_index=0,
            workflow_execution_id="execution-1",
        ),
    )
    suspended.workflow_run_id = "run-1"
    suspended.workflow_id = "workflow-1"
    suspended.workflow_parent_request = CapabilityRequest(
        capability_id="workflow",
        task_id=task.id,
        call_id="workflow-outer-call",
        arguments={"operation": "run", "workflow_id": "workflow-1", "inputs": {}},
    )

    async def _grant():
        await asyncio.sleep(0)
        kernel._resume[task.id].set()

    asyncio.create_task(_grant())
    await AgentKernel._approval_path(
        kernel,
        task,
        state,
        DispatchResult(suspended=(suspended,)),
    )

    assert replay.outer is not None
    assert replay.outer.call_id == "workflow-outer-call"
    assert replay.outer.arguments["run_id"] == "run-1"
    assert [block.call_id for block in appended] == [
        "workflow-child-call",
        "workflow-outer-call",
    ]


@pytest.mark.asyncio
async def test_durable_workflow_approval_resume_reconstructs_outer_call(tmp_path):
    class _ContinuationStore:
        async def claim_resolved(self, task_id):
            assert task_id == "workflow-task"
            return {
                "call_id": "workflow-child-call",
                "capability_id": "fs",
                "canonical_arguments": {"operation": "write"},
                "decision": "granted",
                "policy_context": {
                    "workflow_run_id": "run-1",
                    "workflow_parent_call_id": "workflow-outer-call",
                    "workflow_parent_capability_id": "workflow",
                    "workflow_id": "workflow-1",
                },
            }

        async def mark_consumed_for_call(self, call_id):
            self.consumed = call_id

    class _RunStore:
        async def complete_call(self, *args, **kwargs):
            del args, kwargs
            return True

    class _Dispatcher:
        def __init__(self):
            self.outer = None

        async def dispatch(self, request, **kwargs):
            del kwargs
            if request.capability_id == "workflow":
                self.outer = request
                return CapabilityResult(
                    request.call_id,
                    request.capability_id,
                    CapabilityResultStatus.OK,
                    output=json.dumps({"status": "completed"}),
                )
            return CapabilityResult(
                request.call_id,
                request.capability_id,
                CapabilityResultStatus.OK,
                output="approved",
            )

    continuation = _ContinuationStore()
    dispatcher = _Dispatcher()
    run_store = _RunStore()
    shim = SimpleNamespace(
        _dispatcher=dispatcher,
        _workspace=WorkspaceSpec(id="repo", root=str(tmp_path)),
        _profile=None,
    )
    task = SimpleNamespace(
        id="workflow-task",
        session_id="session-1",
        capability_policy=None,
        resource_budget=None,
    )
    appended = []
    kernel = SimpleNamespace(
        _continuation_store=continuation,
        _workflow_run_store=run_store,
        _dispatch_factory=lambda _task: shim,
    )

    async def _append_results(_task, results):
        appended.extend(results)

    async def _reconcile_workflow_continuation(*args, **kwargs):
        del args, kwargs

    async def _consume_durable_call(call_id):
        continuation.consumed = call_id

    async def _release_durable_call(call_id):
        continuation.released = call_id

    kernel._append_results = _append_results
    kernel._reconcile_workflow_continuation = _reconcile_workflow_continuation
    kernel._consume_durable_call = _consume_durable_call
    kernel._release_durable_call = _release_durable_call
    kernel._resume_workflow_parent = lambda *args, **kwargs: AgentKernel._resume_workflow_parent(
        kernel,
        *args,
        **kwargs,
    )

    result = await AgentKernel._resume_durable_continuation(kernel, task)

    assert result is None
    assert dispatcher.outer is not None
    assert dispatcher.outer.call_id == "workflow-outer-call"
    assert dispatcher.outer.arguments == {
        "operation": "run",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
    }
    assert [block.call_id for block in appended] == [
        "workflow-child-call",
        "workflow-outer-call",
    ]
    assert continuation.consumed == "workflow-child-call"
