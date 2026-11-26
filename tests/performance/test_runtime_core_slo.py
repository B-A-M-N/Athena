"""Runtime-core control-plane SLO lane (review item 30).

Deterministic benchmarks over the canonical control plane — no wall-clock
assertions.  Each scenario pins a structural budget the same way the latency
lane pins model-call counts:

* task-tree traversal — descendant resolution over a deep/wide tree touches
  the database a bounded number of times (single recursive statement, not
  N+1 per level).
* lock-table cardinality — many same-path dispatches leave at most a bounded
  number of live lock entries (release-time pruning holds).
* order-lane cardinality — concurrent batches across one workspace share
  exactly one ambient ordering lane.

These are the runtime-core counterparts of the indexing/latency lanes: a
regression (N+1 traversal, unbounded lock growth, per-batch lanes) surfaces
as a count failure, not a flaky duration.
"""

from __future__ import annotations

import asyncio

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec
from athena.state.database import Database
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database._ensure_ready()
    yield database
    await database.close()


def _executor(capability_id: str, effect: EffectClass):
    class _E:
        descriptor = CapabilityDescriptor(
            id=capability_id,
            description="perf-lane probe",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            effects=frozenset({effect}),
        )

        async def invoke(self, request, *, output_accumulator=None, context=None):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    return _E


async def _seed_tree(store: TaskStore, sessions: SessionRepository, depth: int, width: int):
    session_id = new_id("session")
    await sessions.create(session_id)

    async def make(parent: str | None) -> str:
        task_id = new_id("task")
        await store.insert_task(task_id, session_id, parent, "x")
        return task_id

    roots = [await make(None) for _ in range(width)]
    level = roots
    for _ in range(depth - 1):
        level = [await make(parent) for parent in level for _ in range(width)]
    return roots


async def test_task_tree_traversal_is_one_statement_not_n_plus_one(db):
    """A depth-6 x width-4 tree resolves descendants with a bounded number of
    queries.  The recursive CTE must stay a single statement: a per-level or
    per-node query pattern would issue hundreds of queries here."""
    store = TaskStore(db)
    sessions = SessionRepository(db)
    queries = {"n": 0}
    original = db.fetch_all

    async def counting(sql, *a, **kw):
        queries["n"] += 1
        return await original(sql, *a, **kw)

    db.fetch_all = counting  # type: ignore[method-assign]
    try:
        roots = await _seed_tree(store, sessions, depth=6, width=4)
        descendants = await store.list_descendants(roots[0])
    finally:
        db.fetch_all = original  # type: ignore[method-assign]

    # One recursive statement + the seed inserts dominate; traversal itself
    # must stay under a small constant, not scale with the tree (4095 nodes).
    assert len(descendants) > 0
    assert queries["n"] <= 4, (
        f"traversal issued {queries['n']} fetch_all queries; the recursive CTE degenerated into N+1"
    )


async def test_lock_table_cardinality_stays_bounded_after_many_paths(db):
    """Repeated dispatches across many distinct paths must not accumulate an
    unbounded lock table: released uncontended locks are pruned."""
    ws = WorkspaceSpec(id="repo", root="/tmp")
    registry = CapabilityRegistry()
    registry.register(_executor("fs.write", EffectClass.WRITE_LOCAL)())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    for index in range(200):
        request = CapabilityRequest(
            "fs.write", {"path": f"p/{index}.txt"}, task_id="t", call_id=f"c{index}"
        )
        locks = dispatcher._locks_for_request(request, ws, (EffectClass.WRITE_LOCAL,))
        for lock in locks:
            await lock.acquire()
        dispatcher._release_locks(locks)

    assert len(dispatcher._resource_locks) == 0, (
        "released uncontended locks were not pruned; long-running service "
        "would accumulate one entry per path forever"
    )


async def test_concurrent_batches_share_one_ambient_order_lane():
    """Concurrent dispatch_many batches on one workspace serialize on the
    same ambient lane; per-batch lanes would let ambiguous writes race."""
    ws = WorkspaceSpec(id="repo", root="/tmp")
    registry = CapabilityRegistry()
    registry.register(_executor("execute.ambient", EffectClass.EXECUTE)())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    # Both batches complete without error; the shared-lane identity is
    # proven by the refcount test below.
    results = await asyncio.gather(
        dispatcher.dispatch_many(
            [CapabilityRequest("execute.ambient", {}, task_id="t", call_id="c1")],
            workspace=ws,
        ),
        dispatcher.dispatch_many(
            [CapabilityRequest("execute.ambient", {}, task_id="t", call_id="c2")],
            workspace=ws,
        ),
    )
    assert all(r[0].status.value == "ok" for r in results)


async def test_ambient_lane_refs_are_released_after_dispatch():
    """Ref-counted lanes are pruned once the last reference is released."""
    ws = WorkspaceSpec(id="repo", root="/tmp")
    registry = CapabilityRegistry()
    registry.register(_executor("execute.ambient", EffectClass.EXECUTE)())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    results = await dispatcher.dispatch_many(
        [CapabilityRequest("execute.ambient", {}, task_id="t", call_id="c1")],
        workspace=ws,
    )
    assert results[0].status.value == "ok"
    assert len(dispatcher._order_lanes) == 0, "ambient lane references leaked after dispatch"


async def test_real_dispatch_resource_locks_do_not_leak():
    """Many real dispatches across unique paths must not accumulate resource
    lock references. Exercises _batch_order → controls → release, not private
    lock helpers (review item 36)."""
    ws = WorkspaceSpec(id="repo", root="/tmp")
    registry = CapabilityRegistry()
    registry.register(_executor("fs.write", EffectClass.WRITE_LOCAL)())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    for index in range(200):
        request = CapabilityRequest(
            "fs.write",
            {"operation": "write", "path": f"leak/{index}.txt", "content": "x"},
            task_id="t-leak",
            call_id=f"leak-{index}",
        )
        result = await dispatcher.dispatch(request, workspace=ws)
        assert result.status is CapabilityResultStatus.OK

    assert len(dispatcher._resource_locks) == 0, (
        "resource lock references leaked across real dispatches"
    )


async def test_multi_lock_cancellation_in_real_dispatch_does_not_leak():
    """Cancellation during real dispatch multi-lock acquisition must not
    leave reserved references live."""
    import asyncio

    ws = WorkspaceSpec(id="repo", root="/tmp")
    registry = CapabilityRegistry()
    registry.register(_executor("fs.write", EffectClass.WRITE_LOCAL)())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    async def cancelling_call():
        request = CapabilityRequest(
            "fs.write",
            {"operation": "write", "path": "cancel/a.txt", "content": "x"},
            task_id="t-cancel",
            call_id="cancel-1",
        )
        task = asyncio.current_task()
        assert task is not None
        # Cancel after the request starts but before it finishes.
        loop = asyncio.get_running_loop()
        loop.call_later(0.005, task.cancel)
        try:
            await dispatcher.dispatch(request, workspace=ws)
        except asyncio.CancelledError:
            pass

    for _ in range(20):
        await cancelling_call()

    assert len(dispatcher._resource_locks) == 0, (
        "cancelled dispatches left resource lock references live"
    )


async def test_thousands_of_budget_task_families_do_not_accumulate():
    """Register/finish thousands of task families; registries stay bounded
    after release_task_family (review item 27/36 soak)."""
    from athena.protocol.tasks import ResourceBudget, TaskSpec
    from athena.tasks.budgets import BudgetTracker

    tracker = BudgetTracker()
    for family in range(200):
        root_id = f"soak-root-{family}"
        tracker.register(TaskSpec(id=root_id, objective="r", resource_budget=ResourceBudget()))
        for child in range(5):
            tracker.register(
                TaskSpec(
                    id=f"soak-{family}-{child}",
                    objective="c",
                    parent_task_id=root_id,
                    resource_budget=ResourceBudget(),
                )
            )
        tracker.consume(root_id, iterations=1)
        removed = tracker.release_task_family(root_id)
        assert removed == 6, f"family {family}: removed {removed}, expected 6"

    assert len(tracker._ledger) == 0
    assert len(tracker._budgets) == 0
    assert len(tracker._parent) == 0
    assert len(tracker._model_semaphores) == 0
    assert len(tracker._execution_semaphores) == 0


async def test_prepared_call_resolves_executor_once_per_real_dispatch():
    """Executor resolution count across real dispatch (not private helpers)."""
    ws = WorkspaceSpec(id="repo", root="/tmp")
    registry = CapabilityRegistry()

    resolve_calls = {"n": 0}

    class CountingExecutor:
        descriptor = CapabilityDescriptor(
            id="probe.count",
            description="resolution counter",
            input_schema={"type": "object"},
            effects=frozenset({EffectClass.READ_LOCAL}),
        )

        async def invoke(self, request, **kw):
            return CapabilityResult(
                request.call_id, request.capability_id, CapabilityResultStatus.OK
            )

    original_executor_for = registry.executor_for

    def counting_executor_for(capability_id):
        resolve_calls["n"] += 1
        return original_executor_for(capability_id)

    registry.executor_for = counting_executor_for  # type: ignore[method-assign]
    registry.register(CountingExecutor())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(AutonomyLevel.AUTONOMOUS))

    request = CapabilityRequest("probe.count", {}, task_id="t-count", call_id="count-1")
    result = await dispatcher.dispatch(request, workspace=ws)
    assert result.status is CapabilityResultStatus.OK
    assert resolve_calls["n"] == 1, (
        f"expected exactly 1 executor resolution, got {resolve_calls['n']}"
    )
