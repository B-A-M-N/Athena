"""Local execution backend (BUILDSPEC 51).

Runs runtimes as subprocesses on the host, owning their process trees through
an ``ExecutionManager``. This is the default backend; a ``container`` backend
exists separately.
"""

from __future__ import annotations

from typing import AsyncIterator, Mapping

from athena.execution.backend import BackendCapabilities, ExecutionBackend
from athena.execution.manager import ExecutionManager
from athena.execution.runtime_host import LocalRuntimeSupervisor, SupervisedLocalBackend
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionRequest,
)

__all__ = ["LocalBackend", "LocalRuntimeSupervisor", "SupervisedLocalBackend"]


class LocalBackend(ExecutionBackend):
    name = "local"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supported_runtimes=tuple(self.manager.available_runtimes()),
            persistent_sessions=True,
            persistent_runtime_state=True,
            reattach=False,
            filesystem_persistence=True,
            network_modes=("allow", "deny", "restricted"),
            secret_materialization=True,
            interactive_stdin=True,
            process_signals=True,
            dependency_installation=("python", "node"),
            runtime_capabilities={
                runtime: {
                    "filesystem_containment": True,
                    "network_containment": True,
                }
                for runtime in ("python", "shell")
            },
        )

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
        resource_limits=None,
    ) -> str:
        return await self.manager.create_session(
            task_id=task_id,
            runtime=runtime,
            cwd=cwd,
            env=dict(env) if env else None,
            workspace_root=workspace_root,
            network_policy=network_policy,
            resource_limits=resource_limits,
        )

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        """Stream ``ExecutionEvent`` items directly from the manager in real
        time (no intermediate buffering), preserving incremental output."""
        async for event in self.manager.stream(request):
            yield event

    async def interrupt(self, execution_id: str) -> None:
        await self.manager.interrupt(execution_id)

    async def destroy_session(self, runtime_session_id: str) -> None:
        await self.manager.destroy_session(runtime_session_id)

    async def shutdown(self) -> None:
        await self.manager.close_all()
