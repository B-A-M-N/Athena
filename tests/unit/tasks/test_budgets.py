"""Unit tests for BudgetTracker (BUILDSPEC §19)."""

from __future__ import annotations
import pytest

from decimal import Decimal


from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.tasks.budgets import BudgetTracker


def _task(task_id, *, budget=None, parent=None):
    return TaskSpec(
        id=task_id,
        objective="x",
        resource_budget=budget or ResourceBudget(max_agent_iterations=10),
        parent_task_id=parent,
    )


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
