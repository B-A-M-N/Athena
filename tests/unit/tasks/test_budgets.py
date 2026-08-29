"""Unit tests for BudgetTracker (BUILDSPEC §19)."""

from __future__ import annotations
import pytest

from decimal import Decimal
from datetime import timedelta


from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.tasks.budgets import BudgetTracker


def _task(task_id, *, budget=None, parent=None):
    return TaskSpec(
        id=task_id,
        objective="x",
        resource_budget=budget or ResourceBudget(max_agent_iterations=10),
        parent_task_id=parent,
    )


class _TreeStore:
    def __init__(self, tasks):
        self.tasks = {task.id: task for task in tasks}
        self.metadata = {task.id: {} for task in tasks}

    async def get(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            return None
        return {
            "id": task.id,
            "parent_task_id": task.parent_task_id,
            "resource_budget": task.resource_budget,
            "metadata": self.metadata[task.id],
        }

    async def list_children(self, task_id):
        return [{"id": task.id} for task in self.tasks.values() if task.parent_task_id == task_id]

    async def list_descendants(self, task_id):
        result = []
        frontier = [task_id]
        while frontier:
            children = await self.list_children(frontier.pop(0))
            result.extend(children)
            frontier.extend(child["id"] for child in children)
        return result

    async def persist_budget_usage(self, task_id, usage):
        self.metadata[task_id]["_budget_usage"] = dict(usage)


@pytest.mark.athena_claim("BHV-083")
@pytest.mark.athena_evidence("test", "invariant")
async def test_consume_decreases_remaining():
    tracker = BudgetTracker()
    tracker.register(_task("t", budget=ResourceBudget(max_agent_iterations=10)))
    before = await tracker.remaining("t")
    assert before["iterations"] == 10

    tracker.consume("t", iterations=3)
    after = await tracker.remaining("t")
    assert after["iterations"] == 7


@pytest.mark.athena_claim("BHV-084")
@pytest.mark.athena_evidence("test", "invariant")
async def test_exhausted_rolls_up_to_ancestor():
    tracker = BudgetTracker()
    root = _task("root", budget=ResourceBudget(max_agent_iterations=1))
    child = _task("child", parent="root", budget=ResourceBudget(max_agent_iterations=10))
    tracker.register(root)
    tracker.register(child)

    assert await tracker.exhausted("child") is False

    tracker.consume("root", iterations=1)
    # The child's own usage is fine but the root ancestor is exhausted.
    assert await tracker.exhausted("child") is True


@pytest.mark.athena_claim("BHV-083")
@pytest.mark.athena_evidence("test", "invariant")
async def test_remaining_keeps_decimal_cost_precision():
    tracker = BudgetTracker()
    tracker.register(_task("t", budget=ResourceBudget(max_cost_usd=Decimal("1.00"))))
    tracker.consume("t", cost=Decimal("0.1"))
    tracker.consume("t", cost=Decimal("0.1"))
    rem = await tracker.remaining("t")
    assert rem["cost_usd"] == Decimal("0.80")


async def test_artifact_commit_is_owned_once_but_aggregates_to_root():
    root = _task("root", budget=ResourceBudget(max_artifact_bytes=1_000))
    child = _task("child", parent="root", budget=ResourceBudget(max_artifact_bytes=1_000))
    tracker = BudgetTracker(task_store=_TreeStore([root, child]))
    tracker.register(root)
    tracker.register(child)

    await tracker.reserve_artifact("child", 100)
    await tracker.commit_artifact("child", 100)

    assert tracker.own("child").artifact_bytes == 100
    assert tracker.own("root").artifact_bytes == 0
    assert (await tracker.total("child")).artifact_bytes == 100
    assert (await tracker.total("root")).artifact_bytes == 100


async def test_sibling_artifacts_roll_up_without_double_counting():
    root = _task("root", budget=ResourceBudget(max_artifact_bytes=1_000))
    child_a = _task("child-a", parent="root", budget=ResourceBudget(max_artifact_bytes=1_000))
    child_b = _task("child-b", parent="root", budget=ResourceBudget(max_artifact_bytes=1_000))
    tracker = BudgetTracker(task_store=_TreeStore([root, child_a, child_b]))
    for task in (root, child_a, child_b):
        tracker.register(task)

    await tracker.reserve_artifact("child-a", 100)
    await tracker.commit_artifact("child-a", 100)
    await tracker.reserve_artifact("child-b", 150)
    await tracker.commit_artifact("child-b", 150)

    assert (await tracker.total("root")).artifact_bytes == 250


async def test_sibling_reservations_share_root_ceiling():
    root = _task("root", budget=ResourceBudget(max_artifact_bytes=200))
    child_a = _task("child-a", parent="root", budget=ResourceBudget(max_artifact_bytes=200))
    child_b = _task("child-b", parent="root", budget=ResourceBudget(max_artifact_bytes=200))
    tracker = BudgetTracker(task_store=_TreeStore([root, child_a, child_b]))
    for task in (root, child_a, child_b):
        tracker.register(task)

    await tracker.reserve_artifact("child-a", 150)
    with pytest.raises(ValueError, match="artifact budget exceeded.*root"):
        await tracker.reserve_artifact("child-b", 100)


async def test_sibling_execution_leases_share_root_concurrency_limit():
    import asyncio

    root = _task("root", budget=ResourceBudget(max_parallel_executions=1))
    child_a = _task("child-a", parent="root")
    child_b = _task("child-b", parent="root")
    tracker = BudgetTracker(task_store=_TreeStore([root, child_a, child_b]))
    for task in (root, child_a, child_b):
        tracker.register(task)

    active = 0
    maximum = 0

    async def run(task_id: str) -> None:
        nonlocal active, maximum
        async with tracker.execution_lease(task_id):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(run(child_a.id), run(child_b.id))

    assert maximum == 1


async def test_model_reconciliation_releases_phantom_reservation_immediately():
    root = _task("root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child = _task("child", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    tracker = BudgetTracker(task_store=_TreeStore([root, child]))
    tracker.register(root)
    tracker.register(child)

    await tracker.reserve_model_cost("child", Decimal("0.40"))
    await tracker.reconcile_model_cost("child", reserved=Decimal("0.40"), actual=Decimal("0.10"))
    await tracker.reserve_model_cost("child", Decimal("0.60"))

    assert tracker.own("child").cost == Decimal("0.10")
    assert (await tracker.total("root")).cost == Decimal("0.10")
    assert (await tracker.remaining("root"))["cost_usd"] == Decimal("0.30")


async def test_model_costs_from_siblings_aggregate_at_root_once():
    root = _task("root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child_a = _task("child-a", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child_b = _task("child-b", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    tracker = BudgetTracker(task_store=_TreeStore([root, child_a, child_b]))
    for task in (root, child_a, child_b):
        tracker.register(task)

    await tracker.reserve_model_cost("child-a", Decimal("0.40"))
    await tracker.reconcile_model_cost("child-a", reserved=Decimal("0.40"), actual=Decimal("0.10"))
    await tracker.reserve_model_cost("child-b", Decimal("0.40"))
    await tracker.reconcile_model_cost("child-b", reserved=Decimal("0.40"), actual=Decimal("0.15"))

    assert (await tracker.total("root")).cost == Decimal("0.25")
    assert tracker.own("root").cost == Decimal("0")


async def test_failed_model_attempt_releases_reservation_before_fallback():
    root = _task("root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child = _task("child", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    tracker = BudgetTracker(task_store=_TreeStore([root, child]))
    tracker.register(root)
    tracker.register(child)

    await tracker.reserve_model_cost("child", Decimal("0.60"))
    await tracker.release_model_cost("child", Decimal("0.60"))
    await tracker.reserve_model_cost("child", Decimal("0.60"))
    await tracker.reconcile_model_cost("child", reserved=Decimal("0.60"), actual=Decimal("0.20"))

    assert (await tracker.total("root")).cost == Decimal("0.20")
    assert (await tracker.remaining("root"))["cost_usd"] == Decimal("0.80")


async def test_simultaneous_model_reservations_share_root_ceiling():
    import asyncio

    root = _task("root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child_a = _task("child-a", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child_b = _task("child-b", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    tracker = BudgetTracker(task_store=_TreeStore([root, child_a, child_b]))
    for task in (root, child_a, child_b):
        tracker.register(task)

    outcomes = await asyncio.gather(
        tracker.reserve_model_cost("child-a", Decimal("0.60")),
        tracker.reserve_model_cost("child-b", Decimal("0.60")),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1
    assert (await tracker.remaining("root"))["cost_usd"] == Decimal("0.40")


async def test_model_reservation_survives_restart_as_reservation_not_spend():
    root = _task("root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    child = _task("child", parent="root", budget=ResourceBudget(max_cost_usd=Decimal("1.00")))
    store = _TreeStore([root, child])
    first = BudgetTracker(task_store=store)
    first.register(root)
    first.register(child)
    await first.reserve_model_cost("child", Decimal("0.40"))

    restored = BudgetTracker(task_store=store)
    restored.register(root)
    restored.register(child)
    assert (await restored.total("root")).cost == Decimal("0")
    assert (await restored.remaining("root"))["cost_usd"] == Decimal("0.60")
    with pytest.raises(ValueError, match="model cost budget exceeded"):
        await restored.reserve_model_cost("child", Decimal("0.61"))

    await restored.reconcile_model_cost("child", reserved=Decimal("0.40"), actual=Decimal("0.20"))
    assert (await restored.total("root")).cost == Decimal("0.20")
    assert (await restored.remaining("root"))["cost_usd"] == Decimal("0.80")


async def test_fresh_tracker_hydrates_every_descendant_before_root_total():
    root = _task(
        "root",
        budget=ResourceBudget(
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_cost_usd=Decimal("2.00"),
            max_artifact_bytes=1_000,
        ),
    )
    child = _task("child", parent="root")
    grandchild = _task("grandchild", parent="child")
    sibling = _task("sibling", parent="root")
    store = _TreeStore([root, child, grandchild, sibling])
    first = BudgetTracker(task_store=store)
    for task in (root, child, grandchild, sibling):
        first.register(task)

    first.consume(
        "grandchild", input_tokens=120, output_tokens=80, model_calls=1, cost=Decimal("0.40")
    )
    await first._persist_usage("grandchild")
    first.consume("sibling", input_tokens=30, output_tokens=20, model_calls=1, cost=Decimal("0.10"))
    await first._persist_usage("sibling")
    await first.reserve_artifact("grandchild", 250)
    await first.commit_artifact("grandchild", 250)

    restored = BudgetTracker(task_store=store)
    for task in (root, child, grandchild, sibling):
        restored.register(task)

    total = await restored.total("root")
    assert total.input_tokens == 150
    assert total.output_tokens == 100
    assert total.model_calls == 2
    assert total.cost == Decimal("0.50")
    assert total.artifact_bytes == 250
    remaining = await restored.remaining("root")
    assert remaining["input_tokens"] == 850
    assert remaining["output_tokens"] == 900
    assert remaining["cost_usd"] == Decimal("1.50")
    assert remaining["artifact_bytes"] == 750


async def test_wall_time_tracks_active_compute_and_is_root_aware():
    import asyncio

    root = _task("root", budget=ResourceBudget(max_wall_time=timedelta(seconds=1)))
    child = _task("child", parent="root", budget=ResourceBudget())
    tracker = BudgetTracker(task_store=_TreeStore([root, child]))
    tracker.register(root)
    tracker.register(child)

    await tracker.begin_compute("child")
    await asyncio.sleep(0.01)
    remaining = await tracker.remaining("child")
    await tracker.end_compute("child")

    assert (await tracker.total("child")).wall_time_s > 0
    assert 0 < remaining["wall_time_s"] < 1


async def test_inflight_compute_is_not_reset_by_tracker_restart():
    root = _task("root", budget=ResourceBudget(max_wall_time=timedelta(seconds=1)))
    store = _TreeStore([root])
    first = BudgetTracker(task_store=store)
    first.register(root)
    await first.begin_compute("root")

    restored = BudgetTracker(task_store=store)
    restored.register(root)
    total = await restored.total("root")
    assert total.wall_time_s > 0
    assert total.wall_time_s < 1

    await first.end_compute("root")
