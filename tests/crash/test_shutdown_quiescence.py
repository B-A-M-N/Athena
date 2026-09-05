"""Quiescent shutdown stress (P0-2).

Shutdown must be bounded, concurrent, and leave no terminal-pending
coroutines behind: every worker reaches terminal state before stop() returns,
and the ExecutionManager — the sole cleanup owner — reports zero live
resources. The review's original failure (sequential 5s-per-worker waits,
cancelled-but-never-awaited tasks, subprocess transports outliving the loop)
is exercised here repeatedly, including with all worker slots occupied.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus


_EXECUTE_SCRIPT = (
    {
        # Terminal turn first: matches only once an execute result exists.
        "match": {"capability_result_ok": True},
        "respond": {"text": "shutdown probe done", "done": True},
    },
    {
        "match": {"last_user_message_contains": "SHUTDOWNPROBE"},
        "respond": {
            "capability_call": {
                "capability_id": "execute",
                "arguments": {"language": "sh", "code": "sleep 0.4"},
            }
        },
    },
)


async def _submit_some(svc, n: int) -> list[str]:
    ids = []
    for i in range(n):
        task = await svc.submit(
            AgentRequest(prompt=f"SHUTDOWNPROBE {i}", autonomy=AutonomyLevel.AUTONOMOUS),
            wait=False,
        )
        ids.append(task.id)
    return ids


@pytest.mark.athena_evidence("test", "e2e")
async def test_stop_is_quiescent_with_all_worker_slots_occupied(
    make_durable_service, durable_db_path
):
    """stop() returns only after every worker coroutine is terminal, even when
    every slot is mid-run. The execution manager must own zero live resources
    afterwards."""
    svc = await make_durable_service(
        durable_db_path, scripts=_EXECUTE_SCRIPT, worker_max_parallel=2
    )
    await _submit_some(svc, 4)  # more tasks than slots: slots stay occupied

    # Give the workers time to claim and be inside kernel runs.
    await asyncio.sleep(0.3)
    worker = svc._worker
    assert worker is not None
    pool = list(getattr(worker, "_worker_tasks", None) or [])
    assert pool, "worker pool was not running before stop"

    await svc.stop()

    # Every pooled worker coroutine reached terminal state.
    assert all(t.done() for t in pool), "a worker coroutine survived stop()"
    assert worker._worker_tasks is None

    # The execution manager is the sole cleanup owner and must be empty.
    execution = svc._execution
    if execution is not None:
        assert execution.live_resource_count() == 0, (
            "execution manager still holds live resources after stop()"
        )


@pytest.mark.athena_evidence("test", "e2e")
async def test_repeated_restart_leaves_no_live_workers_or_resources(
    make_durable_service, durable_db_path
):
    """Cycle start → load → stop five times on one DB; each stop must be
    quiescent, and the final restart must recover cleanly to a terminal state
    for every task that ran."""
    ids: list[str] = []
    for cycle in range(5):
        svc = await make_durable_service(
            durable_db_path,
            scripts=_EXECUTE_SCRIPT if cycle % 2 == 0 else None,
            worker_max_parallel=2,
        )
        if cycle == 0:
            ids = await _submit_some(svc, 3)
            await asyncio.sleep(0.2)
        else:
            await asyncio.sleep(0.05)
        worker = svc._worker
        pool = list(getattr(worker, "_worker_tasks", None) or [])
        await svc.stop()
        assert all(t.done() for t in pool), f"cycle {cycle}: worker survived stop()"
        assert worker._worker_tasks is None

    # Final service: everything either completed or failed truthfully — no
    # task may be stuck RUNNING after five clean restart cycles.
    svc = await make_durable_service(durable_db_path, scripts=_EXECUTE_SCRIPT)
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            statuses = [await svc.get_task_status(tid) for tid in ids]
            if all(s in (TaskStatus.COMPLETE.value, TaskStatus.FAILED.value) for s in statuses):
                break
            await asyncio.sleep(0.05)
        for tid in ids:
            status = await svc.get_task_status(tid)
            assert status in (TaskStatus.COMPLETE.value, TaskStatus.FAILED.value), (
                f"task {tid} stuck in {status} after repeated restarts"
            )
    finally:
        await svc.stop()
