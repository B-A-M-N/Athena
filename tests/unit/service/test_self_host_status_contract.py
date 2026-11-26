"""Self-host continuation must decode canonical persisted task statuses."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from athena.protocol.tasks import TaskStatus
from athena.service.self_host import SelfHostService


class MissionStore:
    def __init__(self, mission):
        self.mission = mission

    async def latest_active(self, _root):
        return self.mission

    async def update(self, *_args, **_kwargs):
        return None


class TaskStore:
    def __init__(self, status):
        self.status = status

    async def get(self, _task_id):
        return {"id": "task-current", "status": self.status}


class TaskManager:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, task_id):
        self.enqueued.append(task_id)


async def _candidate(_task_id):
    return None


def _service(status):
    mission = {
        "id": "mission-1",
        "status": "active",
        "project_root": "/workspace",
        "current_task_id": "task-current",
        "objective": "mission",
        "plan": {},
        "current_base_fingerprint": "",
    }
    tm = TaskManager()
    svc = SimpleNamespace(
        _self_host_missions=MissionStore(mission),
        _require_task_manager=lambda: tm,
        operator_candidate=_candidate,
        _store_tasks=TaskStore(status),
        shadow_engine=lambda: None,
        submit_self_host=None,
    )
    return SelfHostService(svc)


@pytest.mark.asyncio
async def test_paused_statuses_do_not_manufacture_new_work():
    for status in (
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.WAITING_INPUT,
        TaskStatus.BLOCKED,
        TaskStatus.RECOVERY_REQUIRED,
    ):
        result = await _service(status).continue_self_host(workspace_root="/workspace")
        assert result["status"] == "paused"
        assert result["task_id"] == "task-current"


@pytest.mark.asyncio
async def test_interrupted_is_requeued_and_active_is_reported_resumed():
    service = _service(TaskStatus.INTERRUPTED)
    result = await service.continue_self_host(workspace_root="/workspace")
    assert result["status"] == "resumed"
    assert service._ports.require_task_manager().enqueued == ["task-current"]

    service = _service(TaskStatus.RUNNING)
    result = await service.continue_self_host(workspace_root="/workspace")
    assert result["status"] == "resumed"


@pytest.mark.asyncio
async def test_lowercase_legacy_status_does_not_match_active_or_final():
    result = await _service("running").continue_self_host(workspace_root="/workspace")
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_final_status_fails_closed_without_shadow_engine():
    from athena.protocol.tasks import TaskStatus

    service = _service(TaskStatus.COMPLETE)
    result = await service.continue_self_host(workspace_root="/workspace")
    assert result["status"] == "blocked"
    assert "shadow engine is unavailable" in result["error"]
