"""Budget family cleanup is deterministic and lease-owner safe."""

from __future__ import annotations

from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.tasks.budgets import BudgetTracker


def _task(task_id: str, parent: str | None = None) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        objective="cleanup",
        parent_task_id=parent,
        resource_budget=ResourceBudget(),
    )


async def test_release_family_removes_quiescent_tree():
    tracker = BudgetTracker()
    tracker.register(_task("root"))
    tracker.register(_task("child", parent="root"))
    assert tracker.can_release_family("root") is True
    assert tracker.release_task_family("root") == 2
    assert tracker.current("root").model_calls == 0
    # Released family has no durable registration left to release.
    assert tracker.release_task_family("root") == 0


async def test_release_family_refuses_active_compute():
    tracker = BudgetTracker()
    tracker.register(_task("root-active"))
    await tracker.begin_compute("root-active")
    assert tracker.can_release_family("root-active") is False
    assert tracker.release_task_family("root-active") == 0


async def test_release_family_refuses_outstanding_model_lease():
    tracker = BudgetTracker()
    tracker.register(_task("root-model"))
    async with tracker.model_call_lease("root-model"):
        assert tracker.can_release_family("root-model") is False
        assert tracker.release_task_family("root-model") == 0
    assert tracker.can_release_family("root-model") is True
    assert tracker.release_task_family("root-model") == 1


async def test_release_family_refuses_outstanding_execution_lease():
    tracker = BudgetTracker()
    tracker.register(_task("root-exec"))
    async with tracker.execution_lease("root-exec"):
        assert tracker.can_release_family("root-exec") is False
        assert tracker.release_task_family("root-exec") == 0
    assert tracker.can_release_family("root-exec") is True
    assert tracker.release_task_family("root-exec") == 1


async def test_release_family_refuses_outstanding_reservation():
    tracker = BudgetTracker()
    tracker.register(_task("root-reservation"))
    await tracker.reserve_model_cost("root-reservation", 10)
    assert tracker.can_release_family("root-reservation") is False
    assert tracker.release_task_family("root-reservation") == 0
    await tracker.reconcile_model_cost("root-reservation", reserved=10, actual=0)
    assert tracker.can_release_family("root-reservation") is True
