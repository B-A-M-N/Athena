"""Execution capability streaming and input-boundary tests."""

from __future__ import annotations

from athena.capabilities.execute import ExecuteCapability
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
    InvocationContext,
)
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
)
from athena.protocol.tasks import PathRule, WorkspaceSpec


class _ExecutionManager:
    def __init__(self):
        self.requests = []

    def available_runtimes(self):
        return ["python"]

    def is_session_owned_by_task(self, session_id, task_id):
        return session_id == "session-a" and task_id == "task-a"

    async def stream(self, request, execution_id):
        self.requests.append(request)
        yield ExecutionEvent(ExecutionEventType.STDOUT, execution_id, data="hello")
        yield ExecutionEvent(ExecutionEventType.STDERR, execution_id, data="warning: degraded")
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
            capability_id="execute",
            task_id="task-a",
            call_id="exec-1",
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
            capability_id="execute",
            task_id="task-a",
            call_id="exec-2",
            arguments={"language": "made-up", "code": "noop"},
        ),
        context=InvocationContext(workspace=WorkspaceSpec(id="repo", root=str(tmp_path))),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert "unsupported language" in (result.error or "")


async def test_execute_canonicalizes_relative_workspace_mount_rules(tmp_path):
    execution = _ExecutionManager()
    capability = ExecuteCapability(execution)
    workspace = WorkspaceSpec(
        id="repo",
        root=str(tmp_path),
        writable=(
            # Relative paths must become host paths before a sandbox mount is
            # assembled; passing these strings to bwrap would resolve them
            # against Athena's process cwd instead of this workspace.
            PathRule(path="src", allow=True),
            PathRule(path="secrets", allow=False),
        ),
    )

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="execute",
            task_id="task-a",
            call_id="exec-rules",
            arguments={"language": "python", "code": "print('ok')"},
        ),
        context=InvocationContext(workspace=workspace),
    )

    assert result.status is CapabilityResultStatus.OK
    request = execution.requests[0]
    assert request.writable_paths == (str(tmp_path / "src"),)
    assert request.read_only_paths == (str(tmp_path / "secrets"),)


async def test_execute_adds_candidate_src_without_inheriting_host_pythonpath(tmp_path):
    execution = _ExecutionManager()
    (tmp_path / "src").mkdir()
    capability = ExecuteCapability(execution)

    result = await capability.invoke(
        CapabilityRequest(
            capability_id="execute",
            task_id="task-a",
            call_id="exec-candidate-import",
            arguments={"language": "python", "code": "import athena"},
        ),
        context=InvocationContext(workspace=WorkspaceSpec(id="candidate", root=str(tmp_path))),
    )

    assert result.status is CapabilityResultStatus.OK
    assert execution.requests[0].env["PYTHONPATH"] == str(tmp_path / "src")
    assert execution.requests[0].env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "host/pythonpath" not in execution.requests[0].env


def test_execute_descriptor_does_not_expose_opaque_session_argument():
    properties = ExecuteCapability.descriptor.input_schema["properties"]

    assert "session" not in properties
    assert "Omit session" in ExecuteCapability.descriptor.description


async def test_system_verification_uses_an_isolated_runtime_identity(tmp_path):
    class _ExecutionManager:
        def __init__(self):
            self.requests = []
            self.destroyed = []

        def available_runtimes(self):
            return ["shell"]

        def is_session_owned_by_task(self, _session_id, _task_id):
            return True

        async def stream(self, request, execution_id):
            self.requests.append(request)
            yield ExecutionEvent(
                ExecutionEventType.STARTED,
                execution_id,
                metadata={"runtime_session_id": "shell_verify_task"},
            )
            yield ExecutionEvent(
                ExecutionEventType.EXITED,
                execution_id,
                exit_status=ExecutionExitStatus.EXITED,
                exit_code=0,
            )

        async def destroy_session(self, session_id):
            self.destroyed.append(session_id)

    execution = _ExecutionManager()
    capability = ExecuteCapability(execution)
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="execute",
            task_id="task-a",
            call_id="verify-call",
            arguments={"language": "shell", "code": "true"},
        ),
        context=InvocationContext(
            workspace=WorkspaceSpec(id="candidate", root=str(tmp_path)),
            task_id="task-a",
            verification_call=True,
        ),
    )

    assert result.status is CapabilityResultStatus.OK
    assert execution.requests[0].task_id == "task-a"
    assert execution.requests[0].metadata["__runtime_session_scope"] == (
        "verify:task-a:verify-call"
    )
    assert execution.destroyed == ["shell_verify_task"]
