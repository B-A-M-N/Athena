from __future__ import annotations

import pytest

from athena.protocol.tasks import TaskStatus
from athena.service.service import AthenaService


class _TaskStore:
    def __init__(self):
        self.rows = {
            TaskStatus.RUNNING: [
                {
                    "id": "needs-pack",
                    "metadata": {"required_packs": ["missing-pack"]},
                },
                {"id": "ordinary", "metadata": {}},
            ],
            TaskStatus.INTERRUPTED: [],
        }

    async def list_by_status(self, status):
        return list(self.rows.get(status, ()))


class _TaskManager:
    def __init__(self):
        self.transitions = []

    async def transition(self, task_id, status, *, reason):
        self.transitions.append((task_id, status, reason))


@pytest.mark.asyncio
async def test_unavailable_required_pack_quarantines_resumable_task():
    service = AthenaService.__new__(AthenaService)
    task_manager = _TaskManager()

    quarantined = await service._quarantine_tasks_for_packs(
        task_store=_TaskStore(),
        task_manager=task_manager,
        unavailable={"missing-pack"},
    )

    assert quarantined == ["needs-pack"]
    assert task_manager.transitions == [
        (
            "needs-pack",
            TaskStatus.RECOVERY_REQUIRED,
            "required capability pack unavailable: missing-pack",
        )
    ]
