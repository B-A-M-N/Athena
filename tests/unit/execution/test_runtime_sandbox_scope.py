from __future__ import annotations

from types import SimpleNamespace

from athena.execution.manager import ExecutionManager
from athena.execution.runtimes.base import BaseRuntime
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionRequest,
)
from athena.protocol.tasks import NetworkPolicy


class _ScopeRuntime(BaseRuntime):
    name = "scope-test"

    def _make_session(self, *, env=None, cwd=None, sandbox_root=None, network_policy=None):
        return SimpleNamespace(
            env=env or {},
            cwd=cwd,
            sandbox_root=sandbox_root,
            network_policy=network_policy,
        )

    def _run(self, session, request, execution_id):
        yield ExecutionEvent(execution_id=execution_id, type=ExecutionEventType.EXITED)

    def _interrupt_session(self, session):
        pass

    def _close_session(self, session):
        pass


async def test_explicit_session_preserves_workspace_and_network_scope(tmp_path):
    runtime = _ScopeRuntime()
    manager = ExecutionManager()
    manager.register_runtime(runtime)

    sid = await manager.create_session(
        task_id="task-scope",
        runtime=runtime.name,
        workspace_root=str(tmp_path),
        network_policy="deny",
    )
    session = runtime._sessions[sid]
    assert session.sandbox_root == str(tmp_path)
    assert session.network_policy == "deny"

    matching = ExecutionRequest(
        runtime=runtime.name,
        source="",
        task_id="task-scope",
        workspace_id="workspace",
        runtime_session_id=sid,
        workspace_root=str(tmp_path),
        network_policy=NetworkPolicy.DENY,
    )
    assert runtime._request_matches_session(matching, session)

    changed = ExecutionRequest(
        runtime=runtime.name,
        source="",
        task_id="task-scope",
        workspace_id="workspace",
        runtime_session_id=sid,
        workspace_root=str(tmp_path),
        network_policy=NetworkPolicy.ALLOW,
    )
    assert not runtime._request_matches_session(changed, session)
