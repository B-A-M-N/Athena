"""Terminal family cleanup observes task state before budget release."""

from __future__ import annotations

from types import SimpleNamespace


from athena.protocol.tasks import TaskStatus
from athena.service.budget_cleanup import BudgetFamilyCleanupObserver


class _Store:
    def __init__(self, rows):
        self.rows = rows

    async def list_descendants(self, task_id):
        return [row for row in self.rows if row["id"] != task_id]

    async def get(self, task_id):
        return next((row for row in self.rows if row["id"] == task_id), None)


class _Budgets:
    def __init__(self, *, can_release=True):
        self.calls = []
        self._can_release = can_release

    def can_release_family(self, root_id):
        self.calls.append(("can", root_id))
        return self._can_release

    def release_task_family(self, root_id):
        self.calls.append(("release", root_id))
        return 1


async def test_terminal_family_calls_guarded_release():
    budgets = _Budgets(can_release=True)
    store = _Store([{"id": "root", "status": "COMPLETE"}, {"id": "child", "status": "COMPLETE"}])
    observer = BudgetFamilyCleanupObserver(budgets, task_store=store)
    task = SimpleNamespace(id="child", parent_task_id="root", status=TaskStatus.COMPLETE)
    await observer(task, None)
    assert budgets.calls == [("can", "root"), ("release", "root")]


async def test_nonterminal_family_does_not_release():
    budgets = _Budgets()
    store = _Store([{"id": "root", "status": "COMPLETE"}, {"id": "child", "status": "RUNNING"}])
    observer = BudgetFamilyCleanupObserver(budgets, task_store=store)
    task = SimpleNamespace(id="root", parent_task_id=None, status=TaskStatus.COMPLETE)
    await observer(task, None)
    assert budgets.calls == []


async def test_nonterminal_task_does_not_lookup_family():
    budgets = _Budgets()
    observer = BudgetFamilyCleanupObserver(budgets, task_store=_Store([]))
    task = SimpleNamespace(id="root", parent_task_id=None, status=TaskStatus.RUNNING)
    await observer(task, None)
    assert budgets.calls == []
