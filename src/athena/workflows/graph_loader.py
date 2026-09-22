"""Resolve declarative workflow graphs from durable workflow storage."""

from __future__ import annotations


class WorkflowGraphLoader:
    """Load one root and its nested workflow dependencies without cycles."""

    def __init__(self, store) -> None:
        self._store = store

    async def load(
        self,
        root,
        *,
        task_id: str | None,
        project_id: str | None,
        user_id: str | None,
    ) -> dict:
        graph = {root.id: root}
        pending = [step.workflow_id for step in root.steps if step.workflow_id]
        while pending:
            workflow_id = pending.pop()
            if workflow_id in graph:
                continue
            workflow = await self._store.get(
                workflow_id,
                task_id=task_id,
                project_id=project_id,
                user_id=user_id,
            )
            if workflow is None:
                continue
            graph[workflow.id] = workflow
            pending.extend(step.workflow_id for step in workflow.steps if step.workflow_id)
        return graph


__all__ = ["WorkflowGraphLoader"]
