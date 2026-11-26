"""Owner-scoped workflow graph loading for workflow admission and execution."""

from __future__ import annotations

from athena.workflows.models import Workflow


async def load_workflow_graph(
    store,
    root: Workflow,
    *,
    task_id: str | None,
    project_id: str | None,
    user_id: str | None,
) -> dict[str, Workflow]:
    """Load every reachable child while preserving store ownership filters."""
    graph = {root.id: root}
    pending = [step.workflow_id for step in root.steps if step.workflow_id]
    while pending:
        workflow_id = pending.pop()
        if workflow_id in graph:
            continue
        workflow = await store.get(
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


__all__ = ["load_workflow_graph"]
