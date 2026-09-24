"""Pack-hook workflow adapter subordinate to the reasoning kernel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from athena.protocol.tasks import TaskStatus

__all__ = ["PackHookSupport"]


class PackHookSupport:
    """Run a declared pack-hook workflow without model mediation."""

    def __init__(
        self,
        *,
        workflow_runner: Any,
        transition: Callable[..., Awaitable[None]],
        paused_result: Callable[..., Awaitable[Any]],
        finalize: Callable[..., Awaitable[Any]],
    ) -> None:
        self._workflow_runner = workflow_runner
        self._transition = transition
        self._paused_result = paused_result
        self._finalize = finalize

    async def run(self, task: Any, state: Any, invocation: Mapping[str, Any]) -> Any:
        workflow_id = str(invocation.get("workflow_id") or "")
        pack_id = str(invocation.get("pack_id") or "")
        if not workflow_id or not pack_id:
            return await self._finalize(
                task,
                state,
                TaskStatus.FAILED,
                "pack hook workflow invocation is incomplete",
            )
        workspace = task.workspace
        if workspace is None:
            return await self._finalize(
                task, state, TaskStatus.FAILED, "pack hook workflow requires a workspace"
            )
        if self._workflow_runner is None:
            return await self._finalize(
                task, state, TaskStatus.FAILED, "workflow runner is unavailable"
            )
        try:
            outcome = await self._workflow_runner.run_declared(task, dict(invocation))
            if outcome.suspended is not None:
                await self._transition(task, TaskStatus.WAITING_APPROVAL)
                return await self._paused_result(
                    task,
                    state,
                    TaskStatus.WAITING_APPROVAL,
                    "pack hook workflow is awaiting approval",
                )
            if outcome.status == "completed":
                return await self._finalize(
                    task,
                    state,
                    TaskStatus.COMPLETE,
                    f"pack hook workflow {outcome.workflow_id} completed",
                )
            reason = "; ".join(outcome.failures) or f"workflow status: {outcome.status}"
            return await self._finalize(task, state, TaskStatus.FAILED, reason)
        except Exception as exc:  # workflow failures become truthful task results
            return await self._finalize(
                task, state, TaskStatus.FAILED, f"pack hook workflow failed: {exc}"
            )
