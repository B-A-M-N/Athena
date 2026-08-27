"""Shadow requests preserve internal orchestration provenance."""

from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.protocol.tasks import WorkspaceSpec
from athena.shadow.engine import ShadowBranch, ShadowEngine


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[CapabilityRequest] = []

    async def dispatch(self, request, *, workspace, profile=None):
        self.requests.append(request)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="ok",
        )


async def test_shadow_proposal_is_trusted_orchestration_not_model_repair():
    dispatcher = _RecordingDispatcher()
    engine = ShadowEngine(dispatcher=dispatcher)
    branch = ShadowBranch(
        id="branch-1",
        task_id="task-1",
        base_workspace=WorkspaceSpec(id="base", root="/tmp/base"),
        shadow_workspace=WorkspaceSpec(id="shadow", root="/tmp/shadow"),
        proposal=[{"capability_id": "fs", "arguments": {"operation": "read"}}],
    )

    result = await engine.execute_branch(branch, profile="autonomous")

    assert result.status == "EXECUTING"
    assert len(dispatcher.requests) == 1
    assert dispatcher.requests[0].origin == CapabilityRequestOrigin.TRUSTED_ORCHESTRATION
