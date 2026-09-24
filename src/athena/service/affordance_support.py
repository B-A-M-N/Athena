"""Task-scoped affordance cleanup after finalization."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger("athena.service.affordance_support")

__all__ = ["AffordanceSupport"]


class AffordanceSupport:
    """Remove only task-owned overlays and scratch/workflow projections."""

    def __init__(
        self,
        *,
        fabric: Callable[[], Any],
        scratch: Any,
        workflow_store: Callable[[], Any],
    ) -> None:
        self._fabric = fabric
        self._scratch = scratch
        self._workflow_store = workflow_store

    async def cleanup(self, task: Any, _result: Any = None) -> None:
        task_id = getattr(task, "id", None)
        fabric = self._fabric()
        if task_id and fabric is not None:
            fabric.unregister_task(task_id)
        if task_id:
            self._scratch.discard_task(task_id)
            workflow_store = self._workflow_store()
            if workflow_store is not None:
                try:
                    await workflow_store.delete_for_task(task_id)
                except Exception as exc:  # rationale: cleanup failure must not hide terminal result
                    _logger.warning("task workflow cleanup failed for %s: %s", task_id, exc)
