"""A caller-supplied zero timeout must remain a zero timeout."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from athena.service.task_api import TaskAPI


class SlowService:
    def __init__(self):
        self._task_manager = None
        self._kernel = None

    def _require_task_manager(self):
        return SimpleNamespace(get=self.get_task_row)

    async def get_task_row(self, _task_id):
        return SimpleNamespace(id="task-zero", metadata={"status": "RUNNING"})


@pytest.mark.asyncio
async def test_zero_timeout_returns_immediately():
    api = TaskAPI(SlowService())
    started = time.monotonic()
    task = await api.wait_for("task-zero", timeout=0)
    elapsed = time.monotonic() - started
    assert task.metadata["status"] == "RUNNING"
    assert elapsed < 0.5
