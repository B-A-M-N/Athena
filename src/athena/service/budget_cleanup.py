"""Terminal budget-family cleanup observer.

A family can be released only when every task is terminal and the budget
authority reports no active compute or outstanding lease/reservation. The
observer is idempotent: each terminal event checks whether the root family
became fully terminal, then asks the tracker to perform the guarded release.
"""

from __future__ import annotations

import logging
from typing import Any

from athena.protocol.tasks import FINAL_STATUSES

_logger = logging.getLogger("athena.service.budget_cleanup")

__all__ = ["BudgetFamilyCleanupObserver"]


class BudgetFamilyCleanupObserver:
    """Inspect finalized tasks and release quiescent families exactly once."""

    def __init__(self, budgets: Any, task_store: Any | None = None) -> None:
        self._budgets = budgets
        self._task_store = task_store

    def _root_for(self, task) -> str:
        parent = getattr(task, "parent_task_id", None)
        if parent:
            return str(parent)
        return str(getattr(task, "id", "") or "")

    async def _family_terminal(self, root_id: str) -> bool:
        store = self._task_store
        if store is None or not hasattr(store, "list_descendants"):
            return False
        try:
            descendants = await store.list_descendants(root_id)
        except Exception as exc:  # noqa: BLE001 - cleanup cannot break finalize
            _logger.warning("budget family lookup failed for %s: %s", root_id, exc)
            return False
        root_row = await store.get(root_id) if hasattr(store, "get") else None
        if root_row is None:
            root_row = {"id": root_id}
        rows = [root_row, *descendants]
        for row in rows:
            status = str((row or {}).get("status") or "")
            if status not in {status.value for status in FINAL_STATUSES}:
                return False
        return True

    async def __call__(self, task: Any, _result: Any) -> None:
        if self._budgets is None:
            return
        status = getattr(getattr(task, "status", None), "value", None) or str(
            getattr(task, "status", "")
        )
        if status not in {item.value for item in FINAL_STATUSES}:
            return
        root_id = self._root_for(task)
        if root_id and await self._family_terminal(root_id):
            can_release = getattr(self._budgets, "can_release_family", None)
            release = getattr(self._budgets, "release_task_family", None)
            if not callable(release):
                return
            if callable(can_release) and not can_release(root_id):
                return
            release(root_id)
