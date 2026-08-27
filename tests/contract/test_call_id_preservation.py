"""Contract: a capability result carries the SAME call_id as its request.

Capabilities MUST NOT mint a new call_id when the request already has one
(P0-9). This binds an invocation to its result across the dispatcher.
Tested end-to-end through the real dispatcher + executor for the capabilities
that honor the contract (fs); a call_id is assigned by the dispatcher when
absent so correlations stay lossless.
"""

from __future__ import annotations

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyDecision, PolicyVerdict
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResultStatus,
    InvocationContext,
)
from athena.protocol.tasks import WorkspaceSpec

class _AllowEngine:
    approvals = None

    def evaluate(self, request, *, autonomy=None):
        return PolicyDecision(PolicyVerdict.ALLOW, "allow", "stub.allow", ())

def _dispatcher(executor):
    reg = CapabilityRegistry()
    reg.register(executor)
    return CapabilityDispatcher(reg, _AllowEngine())

def _ws(tmp_path) -> WorkspaceSpec:
    return WorkspaceSpec(id="ws", root=str(tmp_path))

@pytest.mark.athena_claim("BHV-116")
@pytest.mark.athena_evidence("test", "invariant")
class TestCallIdPreservation:
    async def test_dispatcher_assigns_a_call_id_when_absent(self, tmp_path):
        dispatcher = _dispatcher(FilesystemCapability())
        request = CapabilityRequest("fs", {"operation": "read", "path": "x"}, "task-1")
        assert request.call_id == ""
        result = await dispatcher.dispatch(request, workspace=_ws(tmp_path))
        assert result.call_id

    async def test_result_mirrors_request_call_id_through_dispatcher(self, tmp_path):
        (tmp_path / "exists.txt").write_text("hello")
        dispatcher = _dispatcher(FilesystemCapability())
        request = CapabilityRequest("fs", {"operation": "read", "path": "exists.txt"}, "task-1")
        object.__setattr__(request, "call_id", "my-call-1")
        result = await dispatcher.dispatch(request, workspace=_ws(tmp_path))
        assert result.status == CapabilityResultStatus.OK
        assert result.call_id == "my-call-1"

    async def test_result_mirrors_call_id_on_ok_write(self, tmp_path):
        dispatcher = _dispatcher(FilesystemCapability())
        request = CapabilityRequest(
            "fs",
            {"operation": "write", "path": "f.txt", "content": "hello"},
            "task-1",
        )
        object.__setattr__(request, "call_id", "write-call-9")
        result = await dispatcher.dispatch(request, workspace=_ws(tmp_path))
        assert result.status == CapabilityResultStatus.OK
        assert result.call_id == "write-call-9"

    async def test_invoke_carries_call_id(self, tmp_path):
        (tmp_path / "inv.txt").write_text("hi")
        cap = FilesystemCapability()
        request = CapabilityRequest("fs", {"operation": "read", "path": "inv.txt"}, "task-1")
        object.__setattr__(request, "call_id", "invoke-call")
        result = await cap.invoke(
            request, context=InvocationContext(workspace=_ws(tmp_path))
        )
        assert result.call_id == "invoke-call"