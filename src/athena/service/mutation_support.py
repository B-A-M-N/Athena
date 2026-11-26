"""Mutation-completion invalidation support for task-scoped world state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger("athena.service.mutation_support")

__all__ = ["MutationSupport"]


class MutationSupport:
    """Invalidate affected claims and project indexes after a canonical mutation."""

    def __init__(
        self,
        *,
        project_index: Callable[[], Any],
        world_states: Callable[[], dict[str, Any]],
        world_state_store: Callable[[], Any],
    ) -> None:
        self._project_index = project_index
        self._world_states = world_states
        self._world_state_store = world_state_store

    async def completed(
        self,
        task_id: str | None,
        resource: str,
        *,
        mutation_id: str | None = None,
        mutation_event_sequence: int | None = None,
        mutation_sequence: int | None = None,
    ) -> None:
        if not resource:
            return
        coordinator = self._project_index()
        if coordinator is not None:
            coordinator.mark_stale_for_paths([resource])
        for world_state in list(self._world_states().values()):
            if task_id is None or world_state.task_id == task_id:
                world_state.claims.invalidate_for_paths(
                    [resource],
                    mutation_id=mutation_id,
                    mutation_sequence=mutation_sequence,
                    mutation_event_sequence=mutation_event_sequence,
                )
        store = self._world_state_store()
        if store is not None and task_id is not None:
            try:
                await store.invalidate_for_paths(
                    task_id,
                    [resource],
                    mutation_id=mutation_id,
                    mutation_sequence=mutation_sequence,
                    mutation_event_sequence=mutation_event_sequence,
                )
            except Exception as exc:
                _logger.warning("durable claim invalidation failed: %s", exc)
