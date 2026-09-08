from types import SimpleNamespace

import pytest

from athena.protocol.resources import TaskResourceCloseResult
from athena.protocol.tasks import TaskStatus
from athena.service.resource_finalizer import TaskResourceFinalizer


class _Resource:
    def __init__(self, resource_type: str, *, mode: str = "ok") -> None:
        self.resource_type = resource_type
        self.mode = mode
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
