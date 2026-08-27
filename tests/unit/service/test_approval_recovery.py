"""Restart recovery for resolved durable approval continuations."""
from __future__ import annotations

import asyncio

import pytest

from athena.protocol.tasks import TaskStatus
from athena.service.service import AthenaService


class _Continuations:
    def __init__(self):
        self.released = False

    async def release_claims_for_restart(self):
        self.released = True

    async def recoverable_task_ids(self):
        return ["task-1"]


class _Tasks:
    async def get(self, task_id):
        assert task_id == "task-1"
        return {"id": task_id, "status": TaskStatus.WAITING_APPROVAL.value}


class _TaskManager:
    def __init__(self):
        self.transitions = []

    async def transition(self, task_id, status, *, reason=""):
        self.transitions.append((task_id, status, reason))


class _Kernel:
    def __init__(self):
        self.ran = asyncio.Event()

    async def run_task(self, task_id):
        assert task_id == "task-1"
        self.ran.set()


@pytest.mark.asyncio
async def test_recover_approved_continuation_requeues_same_task_once():
    service = AthenaService.__new__(AthenaService)
    service._approval_recovery_tasks = set()
    continuations = _Continuations()
    manager = _TaskManager()
    kernel = _Kernel()

    await service._recover_approved_continuations(
        continuations=continuations,
        task_store=_Tasks(),
        task_manager=manager,
        kernel=kernel,
    )
    assert continuations.released is True
    assert manager.transitions == [
        ("task-1", TaskStatus.RUNNING, "resume approved continuation")
    ]
    recovery = next(iter(service._approval_recovery_tasks))
    await kernel.ran.wait()
    await recovery
    await asyncio.sleep(0)
    assert service._approval_recovery_tasks == set()
