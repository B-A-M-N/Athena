from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from athena.protocol.tasks import TaskStatus
from athena.scheduler.scheduler import Scheduler
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.schedules import ScheduleStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@pytest.mark.asyncio
async def test_reconcile_enqueues_created_occurrence_before_firing_claim() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        schedules = ScheduleStore(db)
        tasks = TaskStore(db)
        manager = TaskManager(task_store=tasks, events=EventStore(db))
        scheduled_for = "2026-01-01T00:00:00+00:00"
        await schedules.upsert_job(
            "job-recovery",
            "recovery",
            payload={"template": {"objective": "recover"}},
            trigger_spec={"type": "once", "at": scheduled_for},
            next_run=scheduled_for,
        )
        claim = await schedules.claim_next_due("job-recovery", scheduled_for)
        assert claim is not None
        await tasks.insert_task(
            "task-recovery",
            None,
            None,
            "recover",
            metadata={"_occurrence": f"job-recovery|{scheduled_for}"},
            status=TaskStatus.CREATED,
        )

        scheduler = Scheduler(schedules, manager)
        await scheduler.reconcile()

        task = await tasks.get("task-recovery")
        run = await schedules.last_run("job-recovery")
        job = await schedules.get_job("job-recovery")
        assert task["status"] == TaskStatus.QUEUED.value
        assert run["status"] == "FIRED"
        assert run["task_id"] == "task-recovery"
        assert job["next_run"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_claim_rolls_back_fired_marker_if_next_run_write_crashes() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        schedules = ScheduleStore(db)
        scheduled_for = "2026-01-02T00:00:00+00:00"
        await schedules.upsert_job(
            "job-atomic-fired",
            "atomic",
            trigger_spec={"type": "once", "at": scheduled_for},
            next_run=scheduled_for,
        )
        claim = await schedules.claim_next_due("job-atomic-fired", scheduled_for)
        assert claim is not None

        def inject(name: str) -> None:
            if name == "schedule-complete-after-fired":
                raise RuntimeError("crash after fired")

        schedules.set_fault_injector(inject)
        with pytest.raises(RuntimeError, match="crash after fired"):
            await schedules.complete_claim(
                claim["id"],
                "job-atomic-fired",
                next_run=None,
                disable=True,
            )

        run = await schedules.last_run("job-atomic-fired")
        job = await schedules.get_job("job-atomic-fired")
        assert run is not None and run["status"] == "CLAIMED"
        assert job is not None
        assert job["next_run"] == scheduled_for
        assert job["enabled"] is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_claim_rolls_back_next_run_write_if_followup_crashes() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        schedules = ScheduleStore(db)
        scheduled_for = "2026-01-03T00:00:00+00:00"
        next_run = "2026-01-04T00:00:00+00:00"
        await schedules.upsert_job(
            "job-atomic-next",
            "atomic",
            trigger_spec={"type": "interval", "seconds": 86400},
            next_run=scheduled_for,
        )
        claim = await schedules.claim_next_due("job-atomic-next", scheduled_for)
        assert claim is not None

        def inject(name: str) -> None:
            if name == "schedule-complete-after-next-run":
                raise RuntimeError("crash after next run")

        schedules.set_fault_injector(inject)
        with pytest.raises(RuntimeError, match="crash after next run"):
            await schedules.complete_claim(
                claim["id"],
                "job-atomic-next",
                next_run=next_run,
            )

        run = await schedules.last_run("job-atomic-next")
        job = await schedules.get_job("job-atomic-next")
        assert run is not None and run["status"] == "CLAIMED"
        assert job is not None and job["next_run"] == scheduled_for
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_atomic_completion_fault_is_recoverable_after_database_restart(tmp_path) -> None:
    path = tmp_path / "atomic-schedule-restart.sqlite"
    first_db = Database(str(path))
    await first_db._ensure_ready()
    scheduled_for = "2026-01-03T00:00:00+00:00"
    next_run = "2026-01-04T00:00:00+00:00"
    try:
        schedules = ScheduleStore(first_db)
        await schedules.upsert_job(
            "job-atomic-restart",
            "atomic restart",
            trigger_spec={"type": "interval", "seconds": 86400},
            next_run=scheduled_for,
        )
        claim = await schedules.claim_next_due("job-atomic-restart", scheduled_for)
        assert claim is not None

        def inject(name: str) -> None:
            if name == "schedule-complete-after-fired":
                raise RuntimeError("crash before schedule advance")

        schedules.set_fault_injector(inject)
        with pytest.raises(RuntimeError, match="crash before schedule advance"):
            await schedules.complete_claim(
                claim["id"],
                "job-atomic-restart",
                next_run=next_run,
            )
    finally:
        await first_db.close()

    # Reopen the durable database as a restarted scheduler would. The
    # transaction rollback leaves the occurrence claimable, never half-FIRED.
    second_db = Database(str(path))
    await second_db._ensure_ready()
    try:
        schedules = ScheduleStore(second_db)
        run = await schedules.last_run("job-atomic-restart")
        job = await schedules.get_job("job-atomic-restart")
        assert run is not None and run["status"] == "CLAIMED"
        assert job is not None and job["next_run"] == scheduled_for

        assert await schedules.reconcile_stale_occurrences() == 1
        assert await schedules.last_run("job-atomic-restart") is None
        claim = await schedules.claim_next_due("job-atomic-restart", scheduled_for)
        assert claim is not None
        await schedules.complete_claim(
            claim["id"],
            "job-atomic-restart",
            next_run=next_run,
        )
        run = await schedules.last_run("job-atomic-restart")
        job = await schedules.get_job("job-atomic-restart")
        assert run is not None and run["status"] == "FIRED"
        assert job is not None and job["next_run"] == next_run
    finally:
        await second_db.close()


@pytest.mark.asyncio
async def test_reconcile_repairs_legacy_fired_run_with_unchanged_next_run() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        schedules = ScheduleStore(db)
        scheduled_for = "2026-01-05T00:00:00+00:00"
        await schedules.upsert_job(
            "job-legacy-fired",
            "legacy",
            trigger_spec={"type": "once", "at": scheduled_for},
            next_run=scheduled_for,
        )
        await db.execute(
            "INSERT INTO job_runs(id, job_id, scheduled_for, claim_id, started_at, ended_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'FIRED')",
            (
                "run-legacy-fired",
                "job-legacy-fired",
                scheduled_for,
                "run-legacy-fired",
                scheduled_for,
                scheduled_for,
            ),
        )

        repaired = await schedules.reconcile_stale_occurrences(
            next_run_resolver=lambda job, occurrence: (None, True)
        )

        job = await schedules.get_job("job-legacy-fired")
        assert repaired == 1
        assert job is not None
        assert job["next_run"] is None
        assert job["enabled"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_nested_event_delivery_is_deferred_until_claim_boundary_releases() -> None:
    scheduler = Scheduler(SimpleNamespace(), task_manager=SimpleNamespace())
    seen: list[str] = []

    async def observe(event) -> int:
        seen.append(event.id)
        return 1

    scheduler._notify_event = observe
    event = SimpleNamespace(id="event-nested")

    async with scheduler._claim_boundary():
        assert await scheduler.notify_event(event) == 0
        assert seen == []

    for _ in range(5):
        await asyncio.sleep(0)
        if seen:
            break
    assert seen == ["event-nested"]
    await scheduler.stop()


@pytest.mark.asyncio
async def test_tick_run_now_and_duplicate_event_delivery_share_claim_boundary() -> None:
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        schedules = ScheduleStore(db)
        scheduled_for = "2026-01-06T00:00:00+00:00"
        await schedules.upsert_job(
            "job-concurrent-once",
            "concurrent once",
            payload={"template": {"objective": "one scheduled task"}},
            trigger_spec={"type": "once", "at": scheduled_for},
            next_run=scheduled_for,
        )
        await schedules.upsert_job(
            "job-concurrent-event",
            "concurrent event",
            payload={"template": {"objective": "one event task"}},
            trigger_spec={"type": "event", "event_name": "ArtifactCreated", "times": 1},
            next_run=None,
        )

        task_store = TaskStore(db)
        sessions = SessionRepository(db)

        class _Tasks:
            def __init__(self) -> None:
                self.created: list[str] = []
                self.enqueued: list[str] = []

            async def create(self, spec):
                await asyncio.sleep(0)
                if await sessions.get(spec.session_id) is None:
                    await sessions.create(spec.session_id)
                await task_store.insert_task(
                    spec.id,
                    spec.session_id,
                    spec.parent_task_id,
                    spec.objective,
                    metadata=dict(spec.metadata),
                    status=TaskStatus.CREATED,
                )
                self.created.append(spec.id)
                return SimpleNamespace(id=spec.id)

            async def enqueue(self, task_id: str):
                await asyncio.sleep(0)
                self.enqueued.append(task_id)

        tasks = _Tasks()
        scheduler = Scheduler(schedules, tasks)
        event = SimpleNamespace(
            id="event-concurrent-1",
            type="ArtifactCreated",
            payload={},
            task_id=None,
            session_id=None,
        )

        results = await asyncio.gather(
            scheduler.tick(datetime(2026, 1, 6, tzinfo=timezone.utc)),
            scheduler.run_now("job-concurrent-once"),
            scheduler.notify_event(event),
            scheduler.notify_event(event),
        )

        # tick/run_now compete for one exhausted once occurrence, while the
        # two event deliveries compete for one event identity. Both pairs
        # must materialize exactly one durable task under one ScheduleStore.
        assert results[0] + int(results[1] is not None) == 1
        assert sum(results[2:]) == 1
        assert await schedules.count_runs("job-concurrent-once") == 1
        assert await schedules.count_runs("job-concurrent-event") == 1
        assert len(tasks.created) == 2
        assert len(tasks.enqueued) == 2

        # A fresh scheduler instance sees the durable identities, so replay
        # after restart cannot create either occurrence again.
        restarted = Scheduler(schedules, tasks)
        assert await restarted.tick(datetime(2026, 1, 6, tzinfo=timezone.utc)) == 0
        assert await restarted.run_now("job-concurrent-once") is None
        assert await restarted.notify_event(event) == 0
        assert await schedules.count_runs("job-concurrent-once") == 1
        assert await schedules.count_runs("job-concurrent-event") == 1
        await scheduler.stop()
        await restarted.stop()
    finally:
        await db.close()
