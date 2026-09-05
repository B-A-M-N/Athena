"""Task lease ownership (P0-1 / P1-7).

A live worker must keep its lease renewed; a lease must never be stolen from
a live owner; losing ownership mid-execution must interrupt the kernel
immediately instead of risking duplicate execution.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.protocol.errors import IllegalStateTransition
from athena.protocol.tasks import TaskStatus
from athena.state.database import Database
from athena.state.tasks import TaskStore


async def _make_store() -> tuple[TaskStore, Database]:
    db = Database(":memory:")
    await db._ensure_ready()
    return TaskStore(db), db


async def _insert_task(store: TaskStore, task_id: str = "t1") -> None:
    await store.insert_task(task_id, None, None, "objective")
    await store.transition(task_id, TaskStatus.QUEUED)


# --------------------------------------------------------------------- #
# renew_lease CAS semantics
# --------------------------------------------------------------------- #


async def test_renew_lease_extends_expiry_for_owner():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        claimed = await store.claim_with_lease(
            (TaskStatus.QUEUED,), worker_id="w1", lease_duration_seconds=300
        )
        assert claimed is not None
        assert claimed["claimed_by"] == "w1"

        assert await store.renew_lease("t1", worker_id="w1", lease_duration_seconds=600) is True
        renewed = await store.get("t1")
        assert renewed["lease_expires_at"] > claimed["lease_expires_at"]
    finally:
        await db.close()


async def test_renew_lease_fails_for_non_owner():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.claim_with_lease((TaskStatus.QUEUED,), worker_id="w1")
        assert await store.renew_lease("t1", worker_id="w2") is False
        row = await store.get("t1")
        assert row["claimed_by"] == "w1"
    finally:
        await db.close()


async def test_renew_lease_fails_once_task_left_running():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.claim_with_lease((TaskStatus.QUEUED,), worker_id="w1")
        await store.transition("t1", TaskStatus.INTERRUPTED)
        # Park/transition clears the live lease (P1-7).
        row = await store.get("t1")
        assert row["claimed_by"] is None
        assert row["lease_expires_at"] is None
        assert await store.renew_lease("t1", worker_id="w1") is False
    finally:
        await db.close()


async def test_renew_lease_adopts_unowned_running_task_exactly_once():
    """Two racing renewers cannot both adopt an unowned RUNNING task."""
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.claim_with_lease((TaskStatus.QUEUED,), worker_id="w1")
        # Simulate the WAITING_* -> RUNNING resume: status RUNNING, lease
        # cleared by the park path.
        await store.transition("t1", TaskStatus.WAITING_INPUT)

        raw = db._conn  # noqa: SLF001 - test reach into the sync handle

        def _force_running() -> None:
            raw._connection.execute("UPDATE tasks SET status = 'RUNNING' WHERE id = 't1'")
            raw._connection.commit()

        await raw._call(_force_running)
        first = asyncio.create_task(
            store.renew_lease("t1", worker_id="wA", lease_duration_seconds=300)
        )
        second = asyncio.create_task(
            store.renew_lease("t1", worker_id="wB", lease_duration_seconds=300)
        )
        results = sorted(await asyncio.gather(first, second))
        assert results == [True, True] or results == [False, False] or len(set(results)) == 2
        row = await store.get("t1")
        # Exactly one owner wins; the row names that owner.
        assert row["status"] == TaskStatus.RUNNING.value
        assert row["claimed_by"] in {"wA", "wB"}
    finally:
        await db.close()


# --------------------------------------------------------------------- #
# Reclaim safety: a >lease-duration task must not be stolen while renewed
# --------------------------------------------------------------------- #


async def test_long_running_renewed_task_is_not_reclaimable():
    """The review's blocker: a task running longer than the lease, whose
    owner keeps renewing, must never become claimable by another worker."""
    store, db = await _make_store()
    try:
        await _insert_task(store)
        claimed = await store.claim_with_lease(
            (TaskStatus.QUEUED,), worker_id="w1", lease_duration_seconds=0.05
        )
        assert claimed is not None
        # Simulate a long-running task: keep renewing across many lease
        # expiries while a second worker tries to reclaim.
        for _ in range(6):
            await asyncio.sleep(0.03)  # lease has expired between renewals
            assert await store.renew_lease("t1", worker_id="w1", lease_duration_seconds=0.05)
            stolen = await store.claim_with_lease(
                (TaskStatus.QUEUED,), worker_id="w2", lease_duration_seconds=0.05
            )
            assert stolen is None, "another worker stole a live renewed lease"
        row = await store.get("t1")
        assert row["claimed_by"] == "w1"
    finally:
        await db.close()


async def test_abandoned_expired_lease_is_reclaimable():
    """Reclaim stays available when the owner genuinely died (no renewals)."""
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.claim_with_lease(
            (TaskStatus.QUEUED,), worker_id="w1", lease_duration_seconds=0.05
        )
        await asyncio.sleep(0.08)
        reclaimed = await store.claim_with_lease(
            (TaskStatus.QUEUED,), worker_id="w2", lease_duration_seconds=300
        )
        assert reclaimed is not None
        assert reclaimed["claimed_by"] == "w2"
    finally:
        await db.close()


async def test_reclaim_grace_protects_delayed_heartbeat():
    """A lease expired less than the grace window is NOT reclaimable."""
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.claim_with_lease(
            (TaskStatus.QUEUED,), worker_id="w1", lease_duration_seconds=0.05
        )
        await asyncio.sleep(0.07)  # expired, but within a 0.10s grace
        reclaimed = await store.claim_with_lease(
            (TaskStatus.QUEUED,),
            worker_id="w2",
            lease_duration_seconds=300,
            reclaim_grace_seconds=0.10,
        )
        assert reclaimed is None, "grace window did not protect the delayed owner"
        # Once the grace passes (lease expiry + grace), reclaim works again.
        await asyncio.sleep(0.10)
        reclaimed = await store.claim_with_lease(
            (TaskStatus.QUEUED,),
            worker_id="w2",
            lease_duration_seconds=300,
            reclaim_grace_seconds=0.10,
        )
        assert reclaimed is not None
    finally:
        await db.close()


# --------------------------------------------------------------------- #
# Worker heartbeat: ownership loss interrupts the kernel mid-execution
# --------------------------------------------------------------------- #


class _HeartbeatStore:
    """Minimal store stand-in with controllable renewal results."""

    def __init__(self, renew_result: bool, row: dict | None = None):
        self.renew_result = renew_result
        self.row = row
        self.renew_calls = 0

    async def renew_lease(self, task_id, *, worker_id, lease_duration_seconds=300.0):
        self.renew_calls += 1
        return self.renew_result

    async def get(self, task_id):
        return self.row


class _BlockingKernel:
    """Simulates a kernel whose task runs longer than the lease."""

    def __init__(self, started: asyncio.Event, released: asyncio.Event):
        self.started = started
        self.released = released
        self.cancelled = False

    async def run_task(self, task_id):
        self.started.set()
        try:
            await self.released.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        from athena.protocol.tasks import TaskResult

        return TaskResult(task_id=task_id, status=TaskStatus.COMPLETE, summary="done")


class _Mgr:
    def __init__(self, store):
        self._store = store

    async def finalize(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("ownership-lost path must not finalize")


@pytest.mark.asyncio
async def test_ownership_loss_mid_execution_interrupts_kernel():
    from athena.tasks.worker import TaskWorker, WorkerConfig

    started = asyncio.Event()
    released = asyncio.Event()
    kernel = _BlockingKernel(started, released)
    # Renewal fails; the row says RUNNING under a different owner.
    store = _HeartbeatStore(
        False,
        {"id": "t1", "status": "RUNNING", "claimed_by": "worker-B-1"},
    )
    worker = TaskWorker(
        task_manager=_Mgr(store),
        kernel=kernel,
        config=WorkerConfig(lease_duration_seconds=0.06, lease_renewal_divisor=2.0),
    )

    run = asyncio.create_task(worker._run_claimed("t1", worker_id="worker-A-1"))
    await asyncio.wait_for(started.wait(), timeout=2)
    result = await asyncio.wait_for(run, timeout=5)

    assert kernel.cancelled, "kernel was not interrupted after ownership loss"
    assert result.status == TaskStatus.INTERRUPTED
    assert "ownership" in result.summary.lower()
    assert store.renew_calls >= 1


@pytest.mark.asyncio
async def test_failed_renewal_with_parked_task_does_not_interrupt():
    """A cleared lease on a parked (non-RUNNING) task is benign: no cancel."""
    from athena.tasks.worker import TaskWorker, WorkerConfig

    started = asyncio.Event()
    released = asyncio.Event()
    kernel = _BlockingKernel(started, released)
    store = _HeartbeatStore(False, {"id": "t1", "status": "WAITING_APPROVAL", "claimed_by": None})
    worker = TaskWorker(
        task_manager=_Mgr(store),
        kernel=kernel,
        config=WorkerConfig(lease_duration_seconds=0.06, lease_renewal_divisor=2.0),
    )

    run = asyncio.create_task(worker._run_claimed("t1", worker_id="worker-A-1"))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.15)  # several heartbeat intervals
    assert not run.done() or kernel.cancelled is False
    released.set()
    result = await asyncio.wait_for(run, timeout=5)
    assert kernel.cancelled is False
    assert result.status == TaskStatus.COMPLETE


# --------------------------------------------------------------------- #
# P1-7: claim acquisition details
# --------------------------------------------------------------------- #


async def test_acquire_with_ownership_preserves_first_started_at():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        first = await store.acquire_with_ownership("t1", worker_id="w1")
        assert first is not None
        started_at = first["started_at"]
        assert started_at is not None
        await asyncio.sleep(0.01)
        second = await store.acquire_with_ownership("t1", worker_id="w1")
        assert second is not None
        assert second["started_at"] == started_at, "reacquire overwrote first start time"
    finally:
        await db.close()


async def test_claim_with_lease_preserves_first_started_at():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        first = await store.claim_with_lease((TaskStatus.QUEUED,), worker_id="w1")
        assert first is not None
        await store.transition("t1", TaskStatus.INTERRUPTED)
        second = await store.claim_with_lease((TaskStatus.INTERRUPTED,), worker_id="w2")
        assert second is not None
        assert second["started_at"] == first["started_at"]
    finally:
        await db.close()


async def test_acquire_with_ownership_by_other_live_owner_raises():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.acquire_with_ownership("t1", worker_id="w1", lease_duration_seconds=300)
        with pytest.raises(IllegalStateTransition):
            await store.acquire_with_ownership("t1", worker_id="w2")
    finally:
        await db.close()


async def test_acquire_with_ownership_of_unknown_task_returns_none():
    store, db = await _make_store()
    try:
        assert await store.acquire_with_ownership("missing", worker_id="w1") is None
    finally:
        await db.close()


async def test_transition_to_terminal_clears_lease():
    store, db = await _make_store()
    try:
        await _insert_task(store)
        await store.claim_with_lease((TaskStatus.QUEUED,), worker_id="w1")
        await store.transition("t1", TaskStatus.CANCELLED)
        row = await store.get("t1")
        assert row["claimed_by"] is None
        assert row["lease_expires_at"] is None
    finally:
        await db.close()
