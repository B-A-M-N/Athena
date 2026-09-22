"""One autonomy resolver must own readiness, dispatch, verification, and reality."""

from __future__ import annotations

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.concurrency.autonomy import (
    InvalidTaskAutonomyError,
    resolve_autonomy_value,
    resolve_task_autonomy,
)
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import CapabilityRequest, EffectClass
from athena.protocol.policy import DEFAULT_PRINCIPAL_ID, PolicyRequest, Principal, PolicyVerdict
from athena.protocol.tasks import AgentRequest, AutonomyLevel, WorkspaceSpec
from athena.service.service import AthenaService


@pytest.mark.parametrize("level", list(AutonomyLevel))
def test_all_explicit_levels_decode_without_defaulting(level):
    assert resolve_autonomy_value(level.value) is level
    assert resolve_task_autonomy({"autonomy": level.value}) is level


def test_invalid_value_is_rejected_not_omitted():
    with pytest.raises(InvalidTaskAutonomyError):
        resolve_autonomy_value("offlien")
    with pytest.raises(InvalidTaskAutonomyError):
        resolve_task_autonomy({"autonomy": "offlien"})


@pytest.mark.parametrize("level", list(AutonomyLevel))
async def test_readiness_and_canonical_policy_use_same_profile(tmp_path, level):
    service = AthenaService.in_memory()
    await service.start()
    try:
        workspace = WorkspaceSpec(id="parity", root=str(tmp_path))
        spec = service._build_task_spec(
            AgentRequest(
                prompt="refactor the authentication subsystem and run all tests",
                workspace=workspace,
                autonomy=level,
            ),
            "parity-session",
        )
        result = await service.check_complex_coding_readiness(spec)
        if not result["required"]:
            pytest.skip("objective did not classify as complex coding")
        policy_gap = next(
            (gap for gap in result["required_gaps"] if gap["check"] == "verification_policy"),
            None,
        )
        request = PolicyRequest(
            principal=Principal("agent", DEFAULT_PRINCIPAL_ID),
            task_id=spec.id,
            capability_id="execute",
            arguments={"operation": "run", "command": "athena-ready-probe"},
            workspace=workspace,
            execution_backend="local",
            effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
            resources=frozenset(),
            session_id=spec.session_id,
            call_id=None,
        )
        decision = service._policy.evaluate(request, autonomy=level)
        readiness_denied = policy_gap is not None and "DENY" in policy_gap["detail"]
        assert (decision.decision is PolicyVerdict.DENY) is readiness_denied
    finally:
        await service.stop()


@pytest.mark.parametrize("level", list(AutonomyLevel))
async def test_dispatch_policy_rejects_invalid_autonomy_at_boundary(tmp_path, level):
    registry = CapabilityRegistry()
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile=level))
    request = CapabilityRequest(
        capability_id="execute",
        arguments={"language": "shell", "code": "true"},
        task_id="invalid-dispatch",
        call_id="invalid-call",
    )
    with pytest.raises(InvalidTaskAutonomyError):
        await dispatcher.dispatch(
            request,
            workspace=WorkspaceSpec(id="invalid", root=str(tmp_path)),
            profile=level.value + "-malformed",
        )
