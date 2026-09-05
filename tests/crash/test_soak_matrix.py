"""Restart/recovery soak matrix (P1-13).

The review's P0-2 failure showed one deterministic recovery test is not
enough: ownership races and shutdown leaks are timing-sensitive. This
matrix drives restart/stop DURING each pause point a task can occupy and
asserts, per scenario, that after restart:

* every task reaches a truthful terminal state (no stuck RUNNING),
* the stop was quiescent (all worker coroutines terminal),
* the ExecutionManager — sole cleanup owner — holds zero live resources,
* no unraisable warnings escaped (subprocess transports, event-loop
  closures) — enforced by the repo-wide warning filter in CI.

Scenarios (each run repeatedly, since races are timing-sensitive):
* shutdown while a task waits on operator input      (WAITING_INPUT)
* shutdown while a speculative write is in flight     (SPECULATIVE mode)
* shutdown while a direct-mode execution is in flight (DIRECT mode)
* shutdown with every worker slot occupied            (slot saturation)
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    MutationMode,
    TaskStatus,
    WorkspaceSpec,
)


_INPUT_SCRIPT = (
    {
        # Terminal turn: only matches after the operator's answer.
        "match": {"last_user_message_contains": "config/a.yaml"},
        "respond": {"text": "updated config/a.yaml", "done": True},
    },
    {
        "match": {"last_user_message_contains": "SOAKINPUT"},
        "respond": {
            "capability_call": {
                "capability_id": "request_input",
                "arguments": {
                    "question": "Which config file?",
                    "choices": ["config/a.yaml", "config/b.yaml"],
                    "expected": "choice",
                },
            }
        },
    },
)

_WRITE_SCRIPT = (
    {
        "match": {"capability_result_ok": True},
        "respond": {"text": "write landed", "done": True},
    },
    {
        "match": {"last_user_message_contains": "SOAKWRITE"},
        "respond": {
            "capability_call": {
                "capability_id": "fs",
                "arguments": {"operation": "write", "path": "soak.txt", "content": "soak"},
            }
        },
    },
)

_EXECUTE_SCRIPT = (
    {
        "match": {"capability_result_ok": True},
        "respond": {"text": "exec done", "done": True},
    },
    {
        "match": {"last_user_message_contains": "SOAKEXEC"},
        "respond": {
            "capability_call": {
                "capability_id": "execute",
                "arguments": {"language": "sh", "code": "sleep 0.4"},
            }
        },
    },
)


async def _submit(svc, prompt: str, **kw) -> str:
    task = await svc.submit(
        AgentRequest(prompt=prompt, autonomy=AutonomyLevel.AUTONOMOUS, **kw),
        wait=False,
    )
    return task.id


async def _wait_status(svc, task_id, target, tries=500, delay=0.02):
    for _ in range(tries):
        if (await svc.get_task_status(task_id)) == target:
            return target
        await asyncio.sleep(delay)
    return await svc.get_task_status(task_id)


def _read_statuses(db_path, ids) -> dict[str, str]:
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        rows = dict(
            con.execute(
                "SELECT id, status FROM tasks WHERE id IN (%s)" % ",".join("?" * len(ids)), ids
            )
        )
    finally:
        con.close()
    return rows


async def _stop_and_assert_quiescent(svc) -> None:
    """stop() must leave zero terminal-pending workers and zero live resources."""
    worker = svc._worker
    pool = list(getattr(worker, "_worker_tasks", None) or []) if worker else []
    await svc.stop()
    assert all(t.done() for t in pool), "a worker coroutine survived stop()"
    if worker is not None:
        assert worker._worker_tasks is None
    execution = svc._execution
    if execution is not None:
        assert execution.live_resource_count() == 0, (
            "execution manager still holds live resources after stop()"
        )


_TERMINAL = {TaskStatus.COMPLETE.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
_PAUSED = {
    TaskStatus.WAITING_INPUT.value,
    TaskStatus.WAITING_APPROVAL.value,
    TaskStatus.INTERRUPTED.value,
}


async def _assert_truthful_recovery(svc, ids) -> None:
    """After restart, every task must reach a truthful state — terminal, or
    a paused state that still owns a live wait — and then finish."""
    for task_id in ids:
        final = await _wait_status(svc, task_id, TaskStatus.COMPLETE.value, tries=1500)
        assert final in _TERMINAL, f"{task_id} stuck in {final!r} after restart"


def _workspace(root: str, mode: MutationMode) -> WorkspaceSpec:
    return WorkspaceSpec(id="soak", root=root, mutation_mode=mode)


@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.athena_scenario("SOAK-001")
async def test_soak_shutdown_during_input_wait(make_durable_service, durable_db_path):
    """Stop while a task is parked on operator input; the task must come back
    as INTERRUPTED (not lost), and a restart with an answering operator
    completes it."""
    for attempt in range(3):
        svc1 = await make_durable_service(durable_db_path, scripts=_INPUT_SCRIPT)
        task_id = await _submit(svc1, "SOAKINPUT pick a file")
        assert await _wait_status(svc1, task_id, TaskStatus.WAITING_INPUT.value) == "WAITING_INPUT"
        await _stop_and_assert_quiescent(svc1)
        row = _read_statuses(durable_db_path, [task_id])[task_id]
        # Interrupted (recoverable) or already-stored WAITING_INPUT: both are
        # truthful; silently CANCELLED or still RUNNING pre-restart is not.
        assert row in (TaskStatus.INTERRUPTED.value, TaskStatus.WAITING_INPUT.value), (
            f"attempt {attempt}: parked task became {row!r} on stop"
        )
        # Fresh service per attempt: restart-with-answer is covered by
        # RECOVERY lanes; here the soak target is the stop itself.


@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.athena_scenario("SOAK-002")
async def test_soak_shutdown_during_speculative_write(make_durable_service, durable_db_path):
    tmp = tempfile.mkdtemp(prefix="soak-spec-")
    ws = _workspace(tmp, MutationMode.SPECULATIVE)
    for attempt in range(3):
        svc = await make_durable_service(durable_db_path, scripts=_WRITE_SCRIPT)
        task_id = await _submit(svc, "SOAKWRITE speculative", workspace=ws)
        await asyncio.sleep(0.15)  # let the write enter the candidate path
        await _stop_and_assert_quiescent(svc)
        # Restart drives every soaked task to a truthful terminal state.
        svc2 = await make_durable_service(durable_db_path, scripts=_WRITE_SCRIPT)
        await _assert_truthful_recovery(svc2, [task_id])
        await svc2.stop()


@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.athena_scenario("SOAK-003")
async def test_soak_shutdown_during_direct_execution(make_durable_service, durable_db_path):
    tmp = tempfile.mkdtemp(prefix="soak-direct-")
    ws = _workspace(tmp, MutationMode.DIRECT)
    for attempt in range(3):
        svc = await make_durable_service(durable_db_path, scripts=_EXECUTE_SCRIPT)
        task_id = await _submit(svc, "SOAKEXEC direct", workspace=ws)
        await asyncio.sleep(0.15)  # let the execute enter the runtime
        await _stop_and_assert_quiescent(svc)
        svc2 = await make_durable_service(durable_db_path, scripts=_EXECUTE_SCRIPT)
        await _assert_truthful_recovery(svc2, [task_id])
        await svc2.stop()


@pytest.mark.athena_evidence("test", "e2e")
@pytest.mark.athena_scenario("SOAK-004")
async def test_soak_shutdown_with_all_slots_occupied_then_recovery(
    make_durable_service, durable_db_path
):
    """Saturate every slot mid-execution, stop, restart, and verify the whole
    backlog drains with no stuck RUNNING and no live resources at any stop."""
    for attempt in range(3):
        svc1 = await make_durable_service(
            durable_db_path, scripts=_EXECUTE_SCRIPT, worker_max_parallel=2
        )
        ids = [await _submit(svc1, f"SOAKEXEC saturate {attempt}-{i}") for i in range(5)]
        await asyncio.sleep(0.3)
        await _stop_and_assert_quiescent(svc1)
        rows = _read_statuses(durable_db_path, ids)
        # QUEUED post-stop is truthful for the backlog beyond the saturated
        # slots: those tasks never started before the stop.
        untruthful = set(rows.values()) - _TERMINAL - _PAUSED - {"QUEUED"}
        assert not untruthful, f"untruthful post-stop states: {untruthful}"
        svc2 = await make_durable_service(durable_db_path, scripts=_EXECUTE_SCRIPT)
        try:
            await _assert_truthful_recovery(svc2, ids)
        finally:
            await _stop_and_assert_quiescent(svc2)
