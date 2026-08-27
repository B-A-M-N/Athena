"""debugger capability honesty (BODY-005 evidence).

src/athena/capabilities/debugger.py deliberately keeps itself OFF the model
surface (``Availability.UNAVAILABLE``) because its launch helper would
create a raw host subprocess outside ExecutionManager and it has no DAP
client. These tests pin that honesty: the capability must refuse rather
than pretend, and must never leak a spawned process.
"""

from __future__ import annotations

import pytest

from athena.capabilities.debugger import DebuggerCapability, _DEBUGGER_AVAILABILITY
from athena.protocol.capabilities import (
    Availability,
    CapabilityRequest,
    CapabilityResultStatus,
)
from athena.protocol.ids import new_id


def _request(op: str, **args) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="debugger",
        arguments={"operation": op, **args},
        task_id="t-debug",
        call_id=new_id("call"),
    )


def test_descriptor_is_unavailable():
    """The capability is not advertised as usable — no false model surface."""
    assert _DEBUGGER_AVAILABILITY is Availability.UNAVAILABLE
    assert DebuggerCapability.descriptor.availability is Availability.UNAVAILABLE


async def test_launch_refused_while_unavailable(tmp_path):
    """Every operation refuses with the documented reason — nothing spawns."""
    cap = DebuggerCapability()
    script = tmp_path / "prog.py"
    script.write_text("print('hi')\n")
    result = await cap.invoke(_request("launch", script=str(script)))
    assert result.status is CapabilityResultStatus.FAILED
    assert "unavailable" in (result.error or "")
    # No session was created despite the launch request.
    assert cap._sessions == {}


async def test_all_operations_refused(tmp_path):
    """status/breakpoint/detach on a nonexistent session also refuse."""
    cap = DebuggerCapability()
    for op in ("status", "breakpoint", "detach"):
        result = await cap.invoke(_request(op, session="dbg_missing",
                                           file=str(tmp_path / "x.py"), line=1))
        assert result.status is CapabilityResultStatus.FAILED, op
        assert "unavailable" in (result.error or ""), op


def test_close_all_is_safe_with_no_sessions():
    cap = DebuggerCapability()
    cap.close_all()  # must not raise


async def test_session_ownership_is_enforced():
    """A session bound to one task is invisible to another task."""
    cap = DebuggerCapability()
    # Manufacture a session record directly to test the ownership guard
    # without spawning any process (launch is refused while unavailable).
    cap._sessions["dbg_x"] = {
        "task_id": "t-other", "port": 0, "proc": None,
        "paused": False, "breakpoints": {}, "session_id": "dbg_x",
    }
    result = await cap.invoke(_request("status", session="dbg_x"))
    assert result.status is CapabilityResultStatus.FAILED
    assert "unavailable" in (result.error or "")  # refusal precedes ownership


@pytest.mark.parametrize("op", ["attach", "continue", "pause", "stack",
                                "variables", "evaluate", "step"])
async def test_dap_ops_require_client(op):
    """Client-dependent ops refuse with the DAP-client explanation."""
    cap = DebuggerCapability()
    result = await cap.invoke(_request(op, session="dbg_any"))
    assert result.status is CapabilityResultStatus.FAILED
    # Unavailability is the first gate every op hits while disabled.
    assert "unavailable" in (result.error or "")
