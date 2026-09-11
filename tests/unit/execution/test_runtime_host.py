from __future__ import annotations

import errno

import pytest

from athena.execution.runtime_host import LocalRuntimeSupervisor, SupervisedLocalBackend
from athena.protocol.execution import ExecutionEventType, ExecutionRequest


async def _run(backend, request):
    return [event async for event in backend.execute(request)]


async def test_local_runtime_host_executes_and_reattaches(tmp_path):
    # The managed test runner may launch the detached host under its isolated
    # user; keep the parent traversal directory reachable while the socket
    # and token themselves remain private to the host directory.
    tmp_path.chmod(0o755)
    first = SupervisedLocalBackend(LocalRuntimeSupervisor(str(tmp_path / "host")))
    try:
        session_id = await first.create_session(
            task_id="task-1",
            runtime="python",
            workspace_root=str(tmp_path),
        )
    except (OSError, RuntimeError) as exc:
        if getattr(exc, "errno", None) == errno.EPERM or "Operation not permitted" in str(exc):
            await first.supervisor.stop()
            pytest.skip("managed sandbox disallows detached Unix-socket hosts")
        raise
    initial = await _run(
        first,
        ExecutionRequest(
            runtime="python",
            source="value = 41",
            task_id="task-1",
            workspace_id="root",
            runtime_session_id=session_id,
            workspace_root=str(tmp_path),
            metadata={"__execution_id": "exec-1"},
        ),
    )
    assert initial[-1].type is ExecutionEventType.EXITED

    identity = dict(await first.describe_session(session_id))
    second = SupervisedLocalBackend(LocalRuntimeSupervisor(str(tmp_path / "host")))
    assert (
        await second.reattach_session(
            {
                "id": session_id,
                "task_id": "task-1",
                "runtime": "python",
                "start_identity": identity["start_identity"],
                "process_identity": identity["process_identity"],
                "workspace_identity": identity["workspace_identity"],
                "metadata": {
                    "host_socket": identity["host_socket"],
                    "host_token_fingerprint": identity["host_token_fingerprint"],
                },
            }
        )
        == session_id
    )

    resumed = await _run(
        second,
        ExecutionRequest(
            runtime="python",
            source="print(value + 1)",
            task_id="task-1",
            workspace_id="root",
            runtime_session_id=session_id,
            workspace_root=str(tmp_path),
            metadata={"__execution_id": "exec-2"},
        ),
    )
    assert any(
        event.type is ExecutionEventType.STDOUT and "42" in (event.data or "") for event in resumed
    )
    await second.shutdown()
    await first.supervisor.stop()
