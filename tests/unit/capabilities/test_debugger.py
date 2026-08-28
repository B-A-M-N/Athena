"""Debugger capability integration contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.capabilities.debugger import DebuggerCapability, _DEBUGGER_AVAILABILITY
from athena.protocol.capabilities import (
    Availability,
    CapabilityRequest,
    CapabilityResultStatus,
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import NetworkPolicy


def _request(op: str, **args) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="debugger",
        arguments={"operation": op, **args},
        task_id="t-debug",
        call_id=new_id("call"),
    )


def test_descriptor_tracks_optional_debugpy_installation():
    assert DebuggerCapability.descriptor.availability is _DEBUGGER_AVAILABILITY


async def test_unavailable_debugpy_refuses_without_side_effects(tmp_path):
    if _DEBUGGER_AVAILABILITY is Availability.AVAILABLE:
        pytest.skip("debugpy is installed in this test environment")
    cap = DebuggerCapability()
    script = tmp_path / "prog.py"
    script.write_text("print('hi')\n")
    result = await cap.invoke(_request("launch", script=str(script)))
    assert result.status is CapabilityResultStatus.FAILED
    assert "unavailable" in (result.error or "")
    assert cap._sessions == {}


async def test_launch_requires_execution_manager(tmp_path):
    if _DEBUGGER_AVAILABILITY is Availability.UNAVAILABLE:
        pytest.skip("debugpy is not installed in this test environment")
    cap = DebuggerCapability()
    script = tmp_path / "prog.py"
    script.write_text("print('hi')\n")
    result = await cap.invoke(_request("launch", script=str(script)))
    assert result.status is CapabilityResultStatus.FAILED
    assert "ExecutionManager" in (result.error or "")


async def test_session_ownership_is_enforced():
    if _DEBUGGER_AVAILABILITY is Availability.UNAVAILABLE:
        pytest.skip("debugpy is not installed in this test environment")
    cap = DebuggerCapability(execution_manager=object())
    cap._sessions["dbg_x"] = {"task_id": "t-other", "session_id": "dbg_x"}
    result = await cap.invoke(_request("status", session="dbg_x"))
    assert result.status is CapabilityResultStatus.FAILED
    assert "unowned" in (result.error or "")


@pytest.mark.asyncio
async def test_launch_and_breakpoint_use_governed_runtime(monkeypatch, tmp_path):
    if _DEBUGGER_AVAILABILITY is Availability.UNAVAILABLE:
        pytest.skip("debugpy is not installed in this test environment")

    class FakeDAP:
        def __init__(self):
            self.events = []
            self.requests = []

        def request(self, command, arguments=None):
            self.requests.append((command, arguments or {}))
            return {"breakpoints": [{"verified": True}]} if command == "setBreakpoints" else {}

        def close(self):
            return None

    class FakeExecution:
        def __init__(self):
            self.created = None
            self.destroyed = None

        def has_runtime(self, name):
            return name == "python"

        async def create_session(self, **kwargs):
            self.created = kwargs
            return "python-session"

        async def execute(self, request, execution_id=None):
            return SimpleNamespace(status="complete")

        async def interrupt(self, execution_id):
            return None

        async def destroy_session(self, session_id):
            self.destroyed = session_id

    root = tmp_path
    script = root / "prog.py"
    script.write_text("x = 1\n")
    workspace = SimpleNamespace(
        id="workspace-1",
        root=str(root),
        execution_backend="local",
        network_policy=NetworkPolicy.ALLOW,
    )
    execution = FakeExecution()
    cap = DebuggerCapability(execution_manager=execution)
    dap = FakeDAP()
    monkeypatch.setattr("athena.capabilities.debugger._free_port", lambda: 43123)

    async def fake_connect(_port):
        return dap

    monkeypatch.setattr(cap, "_connect", fake_connect)
    context = SimpleNamespace(workspace=workspace)
    launched = await cap.invoke(_request("launch", script=str(script)), context=context)
    assert launched.status is CapabilityResultStatus.OK
    assert execution.created["runtime"] == "python"
    sid = launched.metadata["session"]

    breakpoint_result = await cap.invoke(
        _request("breakpoint", session=sid, file=str(script), line=1),
        context=context,
    )
    assert breakpoint_result.status is CapabilityResultStatus.OK
    assert dap.requests[-1][0] == "setBreakpoints"

    detached = await cap.invoke(_request("detach", session=sid), context=context)
    assert detached.status is CapabilityResultStatus.OK
    assert execution.destroyed == "python-session"


def test_close_all_is_safe_with_no_sessions():
    if _DEBUGGER_AVAILABILITY is Availability.UNAVAILABLE:
        return
    DebuggerCapability(execution_manager=object()).close_all()
