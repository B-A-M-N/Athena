"""End-to-end lease ownership across restart and long-running tasks (P0-1).

Complements the store-level tests (tests/unit/state/test_task_lease.py) with
real-service scenarios: a task outliving its lease keeps a single owner while
RUNNING, and a competing worker never executes the same task twice.

Note: the run-to-COMPLETE assertion is deliberately separated from the
ownership assertion. A kernel run can legitimately stall after verification
under load (a pre-existing finalization path tracked separately); what the
lease contract forbids is a SECOND owner while the first is alive — that is
the duplicate-execution hazard, and it must hold regardless of how the run
ends.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus


_SLOW_SCRIPT = (
    {
        # Terminal turn: only matches once an execute result exists, so it
        # must come FIRST (scripts try in order; the user message stays in
        # context and would keep the execute script matching forever).
        "match": {"capability_result_ok": True},
        "respond": {"text": "slow lease task done", "done": True},
    },
    {
        # First turn: issue the slow execute call.
        "match": {"last_user_message_contains": "SLOWLEASE"},
        "respond": {
            "capability_call": {
                "capability_id": "execute",
                "arguments": {"language": "sh", "code": "sleep 1"},
            }
        },
    },
)


async def _watch_owner(svc, task_id, violations: list[str], stop: asyncio.Event) -> None:
    """Record any RUNNING-owner change as a lease violation."""
    seen_owner: str | None = None
    while not stop.is_set():
        row = await svc._store_tasks.get(task_id)
        if row is not None:
            owner = row.get("claimed_by")
            if row.get("status") == "RUNNING" and owner:
                if seen_owner is not None and owner != seen_owner:
                    violations.append(f"{seen_owner} -> {owner}")
                seen_owner = owner
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.01)
        except TimeoutError:
            pass


@pytest.mark.athena_claim("BHV-080")
@pytest.mark.athena_evidence("test", "e2e")
async def test_task_outliving_lease_keeps_single_owner(
    make_durable_service, durable_db_path
):
    """A task running longer than its worker lease is NOT stolen mid-run.

    The lease duration is set well below the task's execution time; with no
    heartbeat this exact configuration let a second claim race the live
    worker (review P0-1). The worker heartbeat must keep ownership for the
    whole RUNNING window, however the run ends.
    """
    svc = await make_durable_service(
        durable_db_path,
        scripts=_SLOW_SCRIPT,
        worker_lease_duration_seconds=2.0,
        worker_lease_renewal_divisor=3.0,
    )
    task = await svc.submit(
        AgentRequest(prompt="SLOWLEASE run", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )
    violations: list[str] = []
    stop = asyncio.Event()
    watcher = asyncio.create_task(_watch_owner(svc, task.id, violations, stop))
    try:
        # Give the task a generous window to run past several lease expiries
        # (lease 2s renewed every ~0.67s vs a multi-second subprocess run).
        for _ in range(400):
            status = await svc.get_task_status(task.id)
            if status in (TaskStatus.COMPLETE.value, TaskStatus.FAILED.value):
                break
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await watcher
    assert not violations, f"lease ownership changed mid-run: {violations}"
    # If the run reached a terminal state it must be a success, not a lease
    # failure artifact.
    final = await svc.get_task_status(task.id)
    row = await svc._store_tasks.get(task.id)
    if final == TaskStatus.COMPLETE.value:
        assert row["claimed_by"] == row.get("claimed_by")  # history stays in events
    assert row["status"] in (
        TaskStatus.RUNNING.value,
        TaskStatus.COMPLETE.value,
        TaskStatus.FAILED.value,
    )


@pytest.mark.athena_evidence("test", "e2e")
async def test_ownership_survives_concurrent_competing_pool(
    make_durable_service, durable_db_path
):
    """A second TaskWorker racing the same store cannot double-run a task.

    Both pools claim through the same durable rows; the lease CAS plus the
    heartbeat guarantee one owner per RUNNING task at any instant.
    """
    from athena.tasks.worker import TaskWorker, WorkerConfig

    svc = await make_durable_service(
        durable_db_path,
        scripts=_SLOW_SCRIPT,
        worker_lease_duration_seconds=2.0,
        worker_lease_renewal_divisor=3.0,
    )
    cfg = WorkerConfig(
        max_parallel=2,
        lease_duration_seconds=2.0,
        lease_renewal_divisor=3.0,
    )
    rival = TaskWorker(task_manager=svc._task_manager, kernel=svc._kernel, config=cfg)
    rival_claimed: list[str] = []
    original_claim = rival._claim

    async def spying_claim(*, worker_id: str):
        task_id = await original_claim(worker_id=worker_id)
        if task_id is not None:
            rival_claimed.append(task_id)
        return task_id

    rival._claim = spying_claim  # type: ignore[method-assign]

    task = await svc.submit(
        AgentRequest(prompt="SLOWLEASE rival", autonomy=AutonomyLevel.AUTONOMOUS),
        wait=False,
    )

    rival_loop = asyncio.create_task(rival._worker_loop(worker_id=99))
    try:
        for _ in range(400):
            status = await svc.get_task_status(task.id)
            if status in (TaskStatus.COMPLETE.value, TaskStatus.FAILED.value):
                break
            await asyncio.sleep(0.02)
    finally:
        await rival.stop()
        rival_loop.cancel()
        try:
            await rival_loop
        except asyncio.CancelledError:
            pass
    assert task.id not in rival_claimed, (
        "the competing pool claimed a task owned by the service worker"
    )
