"""Contract: workspace isolation is enforced by the EXECUTOR, not just policy.

  * FilesystemCapability resolves all paths against the task workspace; a
    ``../`` escape is denied by the executor with a FAILED result (no file
    touched).
  * ExecuteCapability resolves ``cwd`` against the task workspace root — a
    path outside it must fall back (not escape) even when the policy engine is
    absent.
"""

from __future__ import annotations

import os

import pytest

from athena.capabilities.execute import ExecuteCapability
from athena.capabilities.fs import FilesystemCapability
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
    InvocationContext,
)
from athena.protocol.execution import ExecutionExitStatus, ExecutionResult
from athena.protocol.tasks import WorkspaceSpec


def _ws(tmp_path, *, writable=None) -> WorkspaceSpec:
    return WorkspaceSpec(id="ws", root=str(tmp_path), writable=tuple(writable or ()))


def _req(op, path, **extra) -> CapabilityRequest:
    args = {"operation": op, "path": path, **extra}
    request = CapabilityRequest("fs", args, "task-1")
    object.__setattr__(request, "call_id", "ws-call")
    return request


def _exec_req(**args) -> CapabilityRequest:
    defaults = {"language": "shell", "code": "echo hi"}
    request = CapabilityRequest("execute", {**defaults, **args}, "task-1")
    object.__setattr__(request, "call_id", "exec-call")
    return request


class _FakeExecManager:
    def __init__(self):
        self.calls = []

    def available_runtimes(self):
        return ["python", "shell"]

    async def execute(self, exec_req, execution_id, *, sink=None):
        self.calls.append(exec_req)
        return ExecutionResult(execution_id, 0, ExecutionExitStatus.EXITED, stdout="ok")

    async def stream(self, exec_req, execution_id):
        self.calls.append(exec_req)
        from athena.protocol.execution import ExecutionEvent, ExecutionEventType, ExecutionExitStatus
        yield ExecutionEvent(type=ExecutionEventType.STDOUT, execution_id=execution_id, data="ok")
        yield ExecutionEvent(type=ExecutionEventType.EXITED, execution_id=execution_id,
                             exit_status=ExecutionExitStatus.EXITED, exit_code=0)


class TestWorkspaceIsolation:
    async def test_write_inside_workspace_ok(self, tmp_path):
        cap = FilesystemCapability()
        ws = _ws(tmp_path)
        result = await cap.invoke(
            _req("write", "inner.txt", content="hello"),
            context=InvocationContext(workspace=ws),
        )
        assert result.status == CapabilityResultStatus.OK
        assert (tmp_path / "inner.txt").exists()

    async def test_escape_is_denied_by_executor(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        if outside.exists():
            outside.unlink()
        cap = FilesystemCapability()
        result = await cap.invoke(
            _req("write", "../outside.txt", content="nope"),
            context=InvocationContext(workspace=_ws(tmp_path)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert "escapes" in (result.error or "").lower()
        assert not outside.exists()

    async def test_absolute_escape_is_denied(self, tmp_path):
        outside = tmp_path.parent / "abs-escape.txt"
        if outside.exists():
            outside.unlink()
        cap = FilesystemCapability()
        result = await cap.invoke(
            _req("write", str(outside), content="x"),
            context=InvocationContext(workspace=_ws(tmp_path)),
        )
        assert result.status == CapabilityResultStatus.FAILED
        assert not outside.exists()

    async def test_symlink_escape_is_denied(self, tmp_path):
        outside = tmp_path.parent / "linked_target"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "trap"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks not permitted")
        cap = FilesystemCapability()
        result = await cap.invoke(
            _req("list", "trap"),
            context=InvocationContext(workspace=_ws(tmp_path)),
        )
        assert result.status == CapabilityResultStatus.FAILED


class TestExecuteCwd:
    async def test_relative_cwd_resolves_inside_workspace(self, tmp_path):
        mgr = _FakeExecManager()
        cap = ExecuteCapability(mgr)
        req = _exec_req(cwd="sub")
        result = await cap.invoke(
            req, context=InvocationContext(workspace=_ws(tmp_path))
        )
        assert result.status == CapabilityResultStatus.OK
        assert mgr.calls
        expected = os.path.realpath(str(tmp_path / "sub"))
        assert mgr.calls[0].cwd == expected

    async def test_escape_cwd_is_rejected(self, tmp_path):
        mgr = _FakeExecManager()
        cap = ExecuteCapability(mgr)
        req = _exec_req(cwd="../escape")
        result = await cap.invoke(
            req, context=InvocationContext(workspace=_ws(tmp_path))
        )
        assert result.status == CapabilityResultStatus.OK
        # cwd falls back to None (runtime default) rather than escaping.
        assert mgr.calls[0].cwd is None