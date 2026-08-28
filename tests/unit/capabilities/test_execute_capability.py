"""Execution capability streaming and input-boundary tests."""

from __future__ import annotations

from athena.capabilities.execute import ExecuteCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus, InvocationContext
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
)
from athena.protocol.tasks import WorkspaceSpec


class _ExecutionManager:
    def available_runtimes(self):
        return ["python"]

    def is_session_owned_by_task(self, session_id, task_id):
        return session_id == "session-a" and task_id == "task-a"

    async def stream(self, request, execution_id):
        yield ExecutionEvent(ExecutionEventType.STDOUT, execution_id, data="hello")
        yield ExecutionEvent(
            ExecutionEventType.STDERR, execution_id, data="warning: degraded"
        )
        yield ExecutionEvent(
            ExecutionEventType.EXITED,
            execution_id,
            exit_status=ExecutionExitStatus.EXITED,
            exit_code=0,
        )


class _Sink:
    def __init__(self):
        self.chunks = []

    async def chunk(self, text, *, stream="stdout"):
        self.chunks.append((stream, text))


async def test_execute_forwards_live_output_to_accumulator(tmp_path):
    capability = ExecuteCapability(_ExecutionManager())
    sink = _Sink()
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="execute", task_id="task-a", call_id="exec-1",
            arguments={"language": "python", "code": "print('hello')"},
        ),
        output_accumulator=sink,
        context=InvocationContext(workspace=WorkspaceSpec(id="repo", root=str(tmp_path))),
    )

    assert result.status is CapabilityResultStatus.OK
    assert sink.chunks == [("stdout", "hello"), ("stderr", "warning: degraded")]
    assert result.metadata["diagnostic_count"] == 1
    assert result.metadata["diagnostics"][0]["severity"] == "warning"


async def test_execute_rejects_unknown_language_without_falling_back(tmp_path):
    capability = ExecuteCapability(_ExecutionManager())
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="execute", task_id="task-a", call_id="exec-2",
            arguments={"language": "made-up", "code": "noop"},
        ),
        context=InvocationContext(workspace=WorkspaceSpec(id="repo", root=str(tmp_path))),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "unsupported language" in (result.error or "")
