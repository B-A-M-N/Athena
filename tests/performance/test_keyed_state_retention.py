"""Long-lived services must return keyed synchronization state to a bound."""

from __future__ import annotations

import asyncio

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.state.database import Database
from athena.workflows.runs import WorkflowRunStore


async def test_workflow_run_locks_are_ephemeral_across_thousands_of_runs():
    db = Database(":memory:")
    await db._ensure_ready()
    store = WorkflowRunStore(db)
    run_ids = [f"run-{index}" for index in range(5_000)]

    async def touch(run_id: str):
        async with store._lock_for_run(run_id):
            await asyncio.sleep(0)

    await asyncio.gather(*(touch(run_id) for run_id in run_ids))
    assert len(store._locks) == 0
    await db.close()


async def test_neutral_table_removes_locks_after_five_thousand_lifecycles():
    table = ReferenceCountedKeyedLocks()

    async def lifecycle(key: str):
        async with table.lock(key):
            await asyncio.sleep(0)

    await asyncio.gather(*(lifecycle(f"task-{index}") for index in range(5_000)))
    assert len(table) == 0


async def test_kernel_resume_locks_are_ephemeral_across_notification_lifecycles():
    from types import SimpleNamespace

    kernel = SimpleNamespace(
        _resume={},
        _resume_decision={},
        _resume_armed=set(),
        _resume_locks=ReferenceCountedKeyedLocks(),
    )
    task_ids = [f"kernel-task-{index}" for index in range(5_000)]
    for task_id in task_ids:
        kernel._resume[task_id] = asyncio.Event()
        kernel._resume_armed.add(task_id)
        async with kernel._resume_locks.lock(task_id):
            armed = task_id in kernel._resume_armed
            kernel._resume[task_id].set()
        assert armed
    assert len(kernel._resume_locks) == 0


async def test_reality_and_artifact_keyed_locks_return_to_zero():
    from athena.artifacts.store import ArtifactStore
    from athena.reality.gate import RealityGate
    from athena.shadow.engine import ShadowEngine

    shadow = ShadowEngine(
        roots_parent="/tmp/athena-lock-soak-shadows",
        state_root="/tmp/athena-lock-soak-state",
    )
    gate = RealityGate(shadow)
    artifacts = ArtifactStore("/tmp/athena-lock-soak-artifacts")

    async def gate_lifecycle(key: str):
        async with gate.locks.lock(key):
            await asyncio.sleep(0)

    async def artifact_lifecycle(digest: str):
        async with artifacts._meta_lock(digest):
            await asyncio.sleep(0)

    await asyncio.gather(
        *(gate_lifecycle(f"reality-{index}") for index in range(2_500)),
        *(artifact_lifecycle(f"digest-{index}") for index in range(2_500)),
    )
    assert len(gate.locks) == 0
    assert len(artifacts._meta_locks) == 0


async def test_project_index_cache_is_bounded():
    from pathlib import Path

    from athena.project.index.builder import ProjectIndexBuilder
    from athena.project.index.coordinator import ProjectIndexCoordinator

    roots = []
    for index in range(20):
        root = Path(f"/tmp/athena-index-cache-{index}")
        root.mkdir(exist_ok=True)
        (root / "example.txt").write_text(str(index), encoding="utf-8")
        roots.append(str(root))
    coordinator = ProjectIndexCoordinator(None, ProjectIndexBuilder(), cache_limit=4)
    for root in roots:
        await coordinator.current(root)
    assert len(coordinator._cache) == 4
    assert coordinator.status(roots[-1])["cached"] is True
    assert len(coordinator._stale) <= 4
    coordinator.release(roots[-1])
    assert coordinator.status(roots[-1])["cached"] is False
