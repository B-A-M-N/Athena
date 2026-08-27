"""Local execution backend (BUILDSPEC 51).

Runs runtimes as subprocesses on the host, owning their process trees through
an ``ExecutionManager``. This is the default backend; a ``container`` backend
exists separately.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Mapping

from athena.execution.backend import ExecutionBackend
from athena.execution.manager import ExecutionManager
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionRequest,
)

__all__ = ["LocalBackend"]


class LocalBackend(ExecutionBackend):
    name = "local"

    def __init__(self, manager: ExecutionManager | None = None) -> None:
        self.manager = manager if manager is not None else ExecutionManager()

    def register_runtime(self, runtime) -> None:
        self.manager.register_runtime(runtime)

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
        network_policy: str | None = None,
    ) -> str:
        return await self.manager.create_session(
            task_id=task_id, runtime=runtime, cwd=cwd,
            env=dict(env) if env else None,
            workspace_root=workspace_root,
            network_policy=network_policy,
        )

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        """Stream ``ExecutionEvent`` items directly from the manager in real
        time (no intermediate buffering), preserving incremental output."""
        async for event in self.manager.stream(request):
            yield event

    async def interrupt(self, execution_id: str) -> None:
        await self.manager.interrupt(execution_id)

    async def destroy_session(self, runtime_session_id: str) -> None:
        for task_id in list(self.manager._task_sessions):  # noqa: SLF001
            await self._destroy_in_task(task_id, runtime_session_id)

    async def _destroy_in_task(self, task_id: str, runtime_session_id: str) -> None:
        sessions = self.manager._task_sessions.get(task_id, [])  # noqa: SLF001
        for rt, sid in list(sessions):
            if sid == runtime_session_id:
                self.manager._runtime_by_session.pop(sid, None)  # noqa: SLF001
                sessions.remove((rt, sid))
                close = getattr(rt, "close", None)
                if close is not None:
                    if asyncio.iscoroutinefunction(close):
                        await close(sid)
                    else:
                        close(sid)

    async def shutdown(self) -> None:
        await self.manager.close_all()
