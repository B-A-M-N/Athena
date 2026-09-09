import asyncio
from types import SimpleNamespace

import pytest

from athena.protocol.resources import TaskResourceCloseResult
from athena.protocol.tasks import TaskStatus
from athena.service.resource_finalizer import (
    TaskResourceFinalizer,
    TaskResourceRetentionPolicy,
)
from athena.state.database import Database
from athena.state.resource_obligations import ResourceObligationStore


class _Resource:
    def __init__(
        self, resource_type: str, *, mode: str = "ok", reconcile_enabled: bool = False
    ) -> None:
        self.resource_type = resource_type
        self.mode = mode
        self.reconcile_enabled = reconcile_enabled
        self.calls = 0

    async def close_task(self, task_id: str) -> TaskResourceCloseResult:
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError(f"{self.resource_type} close exploded")
        if self.mode == "unproven":
            return TaskResourceCloseResult(
                task_id=task_id,
                resource_type=self.resource_type,
                resource_ids=("closed", "survivor"),
                closed_ids=("closed",),
                unproven=({"resource_id": "survivor", "error": "still alive"},),
                errors=({"resource_id": "survivor", "error": "still alive"},),
            )
        return TaskResourceCloseResult(
            task_id=task_id,
            resource_type=self.resource_type,
            resource_ids=("survivor",),
            closed_ids=("survivor",),
        )

    async def reconcile(self, obligation: dict) -> TaskResourceCloseResult:
        if not self.reconcile_enabled:
            return TaskResourceCloseResult(
                task_id=str(obligation["task_id"]),
                resource_type=self.resource_type,
                resource_ids=(str(obligation["resource_id"]),),
                unproven=({"resource_id": obligation["resource_id"], "error": "still alive"},),
            )
        return TaskResourceCloseResult(
            task_id=str(obligation["task_id"]),
            resource_type=self.resource_type,
            resource_ids=(str(obligation["resource_id"]),),
            closed_ids=(str(obligation["resource_id"]),),
        )


@pytest.mark.asyncio
async def test_finalizer_preserves_unresolved_resources_and_retry_clears_them():
    events = []

    async def sink(event):
        events.append(event.type)

    failing = _Resource("terminal", mode="unproven")
    raising = _Resource("browser", mode="raise")
    service = SimpleNamespace(
        _terminals=failing,
        _browser=raising,
        _debugger=None,
        _external_delegate_manager=None,
        _synthesis=None,
        _execution=None,
    )
    finalizer = TaskResourceFinalizer(event_sink=sink)
    finalizer.bind_service(service)
    task = SimpleNamespace(id="task-cleanup")
    result = SimpleNamespace(status=TaskStatus.COMPLETE)

    await finalizer.finalize(task, result)

    health = finalizer.health()
    assert health["state"] == "recovery_required"
    assert health["unresolved_count"] == 2
    assert {item["resource_id"] for item in health["unresolved"]} == {"survivor", "unknown"}
    assert events == ["TaskResourceTeardownFailed"]
    assert failing.calls == raising.calls == 1

    failing.mode = "ok"
    raising.mode = "ok"
    await finalizer.retry(task, result)

    assert finalizer.health()["state"] == "healthy"
    assert finalizer.health()["unresolved_count"] == 0
    assert events[-1] == "TaskResourcesFinalized"
    assert failing.calls == raising.calls == 2


@pytest.mark.asyncio
async def test_finalizer_continues_after_one_resource_exception_without_success_event():
    events = []

    async def sink(event):
        events.append(event.type)

    resources = {
        name: _Resource(name, mode="raise")
        for name in (
            "terminal",
            "debugger",
            "browser",
            "external_delegate",
            "generated_runtime",
            "execution",
        )
    }
    service = SimpleNamespace(
        _terminals=resources["terminal"],
        _debugger=resources["debugger"],
        _browser=resources["browser"],
        _external_delegate_manager=resources["external_delegate"],
        _synthesis=resources["generated_runtime"],
        _execution=resources["execution"],
    )
    finalizer = TaskResourceFinalizer(event_sink=sink)
    finalizer.bind_service(service)

    await finalizer.finalize(
        SimpleNamespace(id="task-many"), SimpleNamespace(status=TaskStatus.FAILED)
    )

    assert all(resource.calls == 1 for resource in resources.values())
    assert events == ["TaskResourceTeardownFailed"]
    assert finalizer.health()["unresolved_count"] == len(resources)


@pytest.mark.asyncio
async def test_quiesce_proof_is_reused_by_post_commit_observer():
    terminal = _Resource("terminal")
    finalizer = TaskResourceFinalizer()
    finalizer.bind_service(
        SimpleNamespace(
            _terminals=terminal,
            _debugger=None,
            _browser=None,
            _external_delegate_manager=None,
            _synthesis=None,
            _execution=None,
        )
    )
    task = SimpleNamespace(id="task-once")
    result = SimpleNamespace(status=TaskStatus.COMPLETE)

    outcome = await finalizer.quiesce(task, result)
    assert outcome["confirmed"] is True
    await finalizer.finalize(task, result)
    await finalizer.finalize(task, result)
    assert terminal.calls == 1


@pytest.mark.asyncio
async def test_resource_obligation_survives_restart_and_reconciles():
    db = Database(":memory:")
    await db._ensure_ready()
    obligations = ResourceObligationStore(db)
    failing = _Resource("terminal", mode="unproven")
    service = SimpleNamespace(
        _terminals=failing,
        _browser=None,
        _debugger=None,
        _external_delegate_manager=None,
        _synthesis=None,
        _execution=None,
    )
    first = TaskResourceFinalizer()
    first.bind_obligation_store(obligations)
    first.bind_service(service)
    await first.finalize(
        SimpleNamespace(id="task-restart"), SimpleNamespace(status=TaskStatus.COMPLETE)
    )
    assert len(await obligations.list_open()) == 1

    restarted_resource = _Resource("terminal", reconcile_enabled=True)
    restarted_service = SimpleNamespace(
        _terminals=restarted_resource,
        _browser=None,
        _debugger=None,
        _external_delegate_manager=None,
        _synthesis=None,
        _execution=None,
    )
    second = TaskResourceFinalizer()
    second.bind_obligation_store(obligations)
    second.bind_service(restarted_service)
    assert await second.load_unresolved() == 1
    assert await second.reconcile_unresolved() == 1
    assert second.health()["unresolved_count"] == 0
    assert await obligations.list_open() == []
    await db.close()


@pytest.mark.asyncio
async def test_parked_retain_policy_releases_after_ttl():
    events = []

    async def sink(event):
        events.append(event.payload)

    terminal = _Resource("terminal")
    finalizer = TaskResourceFinalizer(
        event_sink=sink,
        retention_policy=TaskResourceRetentionPolicy(mode="retain", retain_seconds=0.01),
    )
    finalizer.bind_service(
        SimpleNamespace(
            _terminals=terminal,
            _debugger=None,
            _browser=None,
            _external_delegate_manager=None,
            _synthesis=None,
            _execution=None,
        )
    )

    outcome = await finalizer.release_parked("task-ttl")
    assert outcome["retained"] is True
    assert terminal.calls == 0
    await asyncio.sleep(0.03)
    assert terminal.calls == 1
    assert events[-1]["mode"] == "retain_expired"
    await finalizer.shutdown()
