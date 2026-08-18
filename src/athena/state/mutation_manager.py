from __future__ import annotations

import logging

from athena.state.mutations import (
    COMPLETED,
    MutationStore,
)

__all__ = ["MutationManager"]

_logger = logging.getLogger("athena.mutations")


class MutationManager:
    """Orchestrates mutation rollback using recorded inverse operations.

    The actual inverse execution is performed by capability executors; this
    manager coordinates persistence so undo is itself an auditable mutation.
    """

    def __init__(self, mutation_store: MutationStore) -> None:
        self._store = mutation_store

    async def rollback(self, mutation_id: str) -> dict:
        """Roll back a completed mutation by executing its inverse.

        Returns a result dict with status and details. The rollback itself
        produces a new auditable mutation record.
        """
        row = await self._store.get(mutation_id)
        if row is None:
            return {"status": "error", "error": f"mutation {mutation_id} not found"}

        if row.get("status") != COMPLETED:
            return {
                "status": "error",
                "error": f"cannot rollback mutation in state {row.get('status')!r}",
            }

        if not row.get("reversible"):
            return {"status": "error", "error": "mutation is not reversible"}

        inverse = row.get("inverse")
        if inverse is None:
            return {"status": "error", "error": "mutation has no inverse recorded"}

        # Mark the original mutation as rolled back
        await self._store.mark_rolled_back(mutation_id)

        # Record the rollback as a new mutation (auditable)
        rollback_id = await self._store.record(
            task_id=row.get("task_id"),
            resource=row.get("resource", ""),
            operation="undo",
            before_state=row.get("after_state"),
            after_state=row.get("before_state"),
            reversible=True,
            metadata={
                "undo_of": mutation_id,
                "original_operation": row.get("operation"),
                "original_inverse": inverse,
            },
        )

        return {
            "status": "ok",
            "rollback_id": rollback_id,
            "original_mutation": mutation_id,
            "inverse": inverse,
        }

    async def get_mutation(self, mutation_id: str) -> dict | None:
        return await self._store.get(mutation_id)

    async def list_for_task(self, task_id: str) -> list[dict]:
        return await self._store.list_for_task(task_id)
