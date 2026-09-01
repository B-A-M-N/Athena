"""Scheduled work must use the same admission boundary as API work."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from athena.protocol.errors import ModelProviderUnconfigured
from athena.scheduler.scheduler import Scheduler


class _Store:
    def __init__(self):
        self.released = []
        self.completed = []
        self.claimed = False

    async def list_jobs(self, enabled_only=True):
        return [
            {
                "id": "job-1",
                "name": "scheduled",
                "next_run": "2026-01-01T00:00:00+00:00",
                "payload": {"objective": "scheduled work"},
                "metadata": {},
                "enabled": True,
            }
        ]

    async def claim_next_due(self, job_id, scheduled_for):
        if self.claimed:
            return None
        self.claimed = True
        return {
            "claim_id": "claim-1",
            "job_id": job_id,
            "scheduled_for": scheduled_for,
        }

    async def get_job_id(self, job_id):
        jobs = await self.list_jobs()
        return next((job for job in jobs if job["id"] == job_id), None)

    async def release_claim(self, claim_id, job_id, scheduled_for):
        self.released.append((claim_id, job_id, scheduled_for))

    async def count_runs(self, job_id):
        return 0

    async def complete_claim(self, *args, **kwargs):
        self.completed.append((args, kwargs))


class _Tasks:
    def __init__(self):
        self.created = 0
        self.enqueued = []

    async def create(self, spec):
        self.created += 1
        return spec

    async def enqueue(self, task_id):
        self.enqueued.append(task_id)


@pytest.mark.asyncio
async def test_provider_admission_failure_releases_scheduled_claim():
    store = _Store()
    tasks = _Tasks()

    async def reject(spec):
        raise ModelProviderUnconfigured(
            "provider disappeared", provider_state="request_unavailable"
        )

    scheduler = Scheduler(store, tasks, admission=reject)
    with pytest.raises(ModelProviderUnconfigured):
        await scheduler.tick(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert tasks.created == 0
    assert store.released == [("claim-1", "job-1", "2026-01-01T00:00:00+00:00")]


@pytest.mark.asyncio
async def test_scheduled_admission_runs_before_task_creation():
    store = _Store()
    tasks = _Tasks()
    order = []

    async def admit(spec):
        order.append("admission")

    original_create = tasks.create

    async def create(spec):
        order.append("create")
        return await original_create(spec)

    tasks.create = create
    scheduler = Scheduler(store, tasks, admission=admit)
    assert await scheduler.tick(datetime(2026, 1, 1, tzinfo=timezone.utc)) == 1
    assert order == ["admission", "create"]
