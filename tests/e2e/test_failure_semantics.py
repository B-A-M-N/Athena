"""Release-facing failure paths must remain observable and recoverable."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from athena.capabilities.maintain import MaintenanceCapability
from athena.capabilities.watch import WatchRegistry, _FileWatch
from athena.protocol.errors import CancellationUncertain, ModelProviderUnconfigured
from athena.protocol.tasks import ResourceBudget, TaskSpec, TaskStatus, WorkspaceSpec
from athena.recovery.manager import RecoveryManager
from athena.scheduler.scheduler import Scheduler
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.cancellation import CancellationManager
from athena.tasks.delegation import DelegationError, DelegationManager
from athena.tasks.manager import TaskManager


class _ClaimStore:
    def __init__(self, *, release_error: Exception | None = None, reconcile_error=None):
        self.released: list[tuple[str, str, str]] = []
        self.completed: list[tuple[tuple, dict]] = []
        self.claimed = False
        self.release_error = release_error
        self.reconcile_error = reconcile_error

    async def list_jobs(self, enabled_only=True):
        return [
            {
                "id": "job-1",
                "name": "release failure path",
                "next_run": "2026-01-01T00:00:00+00:00",
                "payload": {
                    "template": {
                        "objective": "scheduled failure path",
                    },
                    "trigger": {"type": "once", "at": "2026-01-01T00:00:00+00:00"},
                },
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
        return (await self.list_jobs())[0]

    async def release_claim(self, claim_id, job_id, scheduled_for):
        if self.release_error is not None:
            raise self.release_error
        self.released.append((claim_id, job_id, scheduled_for))

    async def count_runs(self, job_id):
        return 0

    async def complete_claim(self, *args, **kwargs):
        self.completed.append((args, kwargs))

    async def reconcile_stale_occurrences(self):
        if self.reconcile_error is not None:
            raise self.reconcile_error


class _FailingTasks:
    async def create(self, spec):
        raise RuntimeError("task creation failed")

    async def enqueue(self, task_id):
        raise AssertionError("failed task must not be enqueued")


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_provider_disappearing_after_schedule_claim_fails_closed():
    store = _ClaimStore()
    created = False

    class Tasks:
        async def create(self, spec):
            nonlocal created
            created = True
            return spec

        async def enqueue(self, task_id):
            raise AssertionError("admission failure must prevent enqueue")

    async def admission(_spec):
        raise ModelProviderUnconfigured(
            "provider disappeared after schedule creation",
            provider_state="request_unavailable",
        )

    scheduler = Scheduler(store, Tasks(), admission=admission)
    with pytest.raises(ModelProviderUnconfigured):
        await scheduler.tick(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert created is False
    assert store.released == [("claim-1", "job-1", "2026-01-01T00:00:00+00:00")]


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_task_creation_failure_releases_claim():
    store = _ClaimStore()
    scheduler = Scheduler(store, _FailingTasks())
    with pytest.raises(RuntimeError, match="task creation failed"):
        await scheduler.tick(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert store.released == [("claim-1", "job-1", "2026-01-01T00:00:00+00:00")]
    assert store.completed == []


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_enqueue_failure_keeps_created_task_claimed_for_reconciliation():
    store = _ClaimStore()
    created = []

    class Tasks:
        async def create(self, spec):
            created.append(spec)
            return spec

        async def enqueue(self, task_id):
            raise RuntimeError("enqueue failed after create")

    scheduler = Scheduler(store, Tasks())
    with pytest.raises(RuntimeError, match="enqueue failed after create"):
        await scheduler.tick(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert len(created) == 1
    assert store.released == []
    assert store.completed == []


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_schedule_store_failure_after_claim_is_not_swallowed():
    store = _ClaimStore(release_error=RuntimeError("schedule store unavailable"))
    scheduler = Scheduler(store, _FailingTasks())
    with pytest.raises(RuntimeError, match="schedule store unavailable"):
        await scheduler.tick(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert store.claimed is True
    assert store.completed == []


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_scheduler_reconciliation_failure_is_health_visible():
    scheduler = Scheduler(
        _ClaimStore(reconcile_error=RuntimeError("reconciliation unavailable")),
        _FailingTasks(),
    )
    with pytest.raises(RuntimeError, match="reconciliation unavailable"):
        await scheduler.start()
    report = scheduler.health()
    assert report["health"] == "failed"
    assert report["reconciliation_failures"] == 1
    assert report["running"] is False


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_watch_poll_failure_is_health_visible(tmp_path, monkeypatch):
    registry = WatchRegistry()
    watch = _FileWatch("watch-1", str(tmp_path), "*", "task-1")
    registry.file_watches[watch.id] = watch

    def fail_poll():
        raise RuntimeError("watch polling unavailable")

    monkeypatch.setattr(watch, "poll", fail_poll)

    async def sink(*args, **kwargs):
        raise AssertionError("failed poll must not emit an observation")

    assert await registry.poll_all(sink) == 0
    report = registry.health()
    assert report["health"] == "degraded"
    assert report["consecutive_poll_errors"] == 1
    assert "watch polling unavailable" in report["last_poll_error"]


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_maintenance_rehydration_failure_is_health_visible(tmp_path, monkeypatch):
    contract = {
        "contract_id": "contract-1",
        "claim": "the file remains present",
        "observe": {
            "kind": "file",
            "path": ".",
            "pattern": "*",
            "watch_id": "watch-1",
        },
        "verify": {"type": "manual"},
        "remediation": {},
        "policy": "supervised",
    }

    class Schedule:
        async def list_jobs(self, **kwargs):
            return [
                {
                    "id": "job-1",
                    "enabled": True,
                    "metadata": {
                        "maintenance_contract": contract,
                        "maintenance_role": "observer",
                    },
                }
            ]

    registry = WatchRegistry()
    capability = MaintenanceCapability(
        Schedule(),
        watch_registry=registry,
        workspace=WorkspaceSpec(id="release", root=str(tmp_path)),
    )

    async def fail_watch(*args, **kwargs):
        raise LookupError("watch state unavailable")

    monkeypatch.setattr(capability, "_ensure_watch", fail_watch)
    assert await capability.rehydrate() == 0
    report = registry.health()
    assert report["failed_rehydrations"] == 1
    assert report["health"] == "degraded"
    assert "watch state unavailable" in report["last_poll_error"]


@pytest.mark.athena_claim("ATHENA-EXT-015")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_watch_rehydration_degradation_survives_poll_success():
    registry = WatchRegistry()
    registry.record_rehydration_failure(
        RuntimeError("observer missing"), watch_id="watch-1", contract_id="contract-1"
    )
    registry.record_poll_success()

    report = registry.health()
    assert report["poll_health"] == "healthy"
    assert report["rehydration_health"] == "degraded"
    assert report["health"] == "degraded"
    assert report["unresolved_rehydrations"][0]["watch_id"] == "watch-1"

    registry.record_rehydration_resolved(watch_id="watch-1")
    assert registry.health()["health"] == "healthy"


@pytest.mark.athena_claim("BHV-091")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_delegation_root_lookup_failure_refuses_creation():
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        tasks = TaskManager(
            task_store=TaskStore(db), events=EventStore(db), sessions=SessionRepository(db)
        )
        parent = SimpleNamespace(
            id="parent",
            status=TaskStatus.CREATED,
            parent_task_id=None,
            resource_budget=ResourceBudget(),
            metadata={},
        )
        lookups = 0

        async def fail_on_root(task_id):
            nonlocal lookups
            lookups += 1
            if lookups == 1:
                return parent
            raise RuntimeError("hierarchy lookup unavailable")

        tasks.get = fail_on_root
        delegation = DelegationManager(task_manager=tasks)
        child = TaskSpec(id="child", objective="child")
        with pytest.raises(DelegationError, match="cannot establish delegation root"):
            await delegation.delegate(parent_task=parent, child_spec=child)
    finally:
        await db.close()


@pytest.mark.athena_claim("BHV-076")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_cancellation_refuses_false_cancel_when_runtime_rejects():
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        sessions = SessionRepository(db)
        store = TaskStore(db)
        manager = TaskManager(task_store=store, events=EventStore(db), sessions=sessions)
        session_id = "cancel-session"
        await sessions.create(session_id)
        task = TaskSpec(id="cancel-task", objective="cancel", session_id=session_id)
        await manager.create(task)

        class RefusingRuntime:
            async def cancel_task(self, task_id):
                return False

        cancellations = CancellationManager(
            task_manager=manager,
            execution_manager=RefusingRuntime(),
            task_store=store,
        )
        with pytest.raises(CancellationUncertain):
            await cancellations.cancel(task.id)
        assert (await manager.get(task.id)).metadata["status"] == TaskStatus.RECOVERY_REQUIRED.value
    finally:
        await db.close()


@pytest.mark.athena_claim("BHV-023")
@pytest.mark.athena_evidence("e2e", "failure-path")
@pytest.mark.asyncio
async def test_recovery_required_task_survives_recovery_pass():
    db = Database(":memory:")
    await db._ensure_ready()
    try:
        store = TaskStore(db)
        await store.insert_task(
            "recovery-task",
            None,
            None,
            "unresolved cancellation",
            status=TaskStatus.RECOVERY_REQUIRED,
        )
        result = await RecoveryManager(task_store=store).recover()
        assert result.status.value == "healthy"
        assert (await store.get("recovery-task"))["status"] == TaskStatus.RECOVERY_REQUIRED.value
    finally:
        await db.close()
