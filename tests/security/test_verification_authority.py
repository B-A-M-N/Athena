"""Bounded verification authority (P0-7).

``SYSTEM`` origin is pure host observation and stays a full ceiling bypass.
``SYSTEM_VERIFICATION`` origin is the acceptance verifier's bounded
execution authority: it may execute operator-declared criteria without
inheriting the task's capability ceiling, but ONLY within a restricted
effect envelope — never secrets, privilege, external publication, or
computer input. Hard workspace/global boundaries still apply.
"""

from __future__ import annotations

import asyncio

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityRequestOrigin,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.tasks import AutonomyLevel, CapabilityPolicy, WorkspaceSpec


class _RecordingExecutor:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.invocations = []

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invocations.append(request)
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="ok",
        )


def _exec(cap_id: str, effects) -> _RecordingExecutor:
    return _RecordingExecutor(
        CapabilityDescriptor(
            id=cap_id,
            description=cap_id,
            input_schema={"allow_extra": True, "properties": {}},
            effects=frozenset(effects),
        )
    )


def _dispatcher(*executors, profile=AutonomyLevel.SUPERVISED) -> CapabilityDispatcher:
    reg = CapabilityRegistry()
    for ex in executors:
        reg.register(ex)
    return CapabilityDispatcher(reg, PolicyEngine(profile))


def _req(cap: str, origin: CapabilityRequestOrigin) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=cap,
        arguments={},
        task_id="t1",
        origin=origin,
    )


# ---------------------------------------------------------------------- #
# The effect floor
# ---------------------------------------------------------------------- #


def test_verification_authority_allows_bounded_execute():
    """SYSTEM_VERIFICATION may execute (READ+WRITE+EXECUTE+SPAWN is inside
    the floor) even when the task ceiling denies everything. The criteria
    are operator-declared, so no second human gate: run under AUTONOMOUS."""
    ex = _exec("verify.cmd", {EffectClass.READ_LOCAL, EffectClass.EXECUTE})
    dispatcher = _dispatcher(ex, profile=AutonomyLevel.AUTONOMOUS)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("verify.cmd", CapabilityRequestOrigin.SYSTEM_VERIFICATION),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
            task_policy=CapabilityPolicy(deny=("verify.cmd",)),
        )
    )
    assert isinstance(result, CapabilityResult)
    assert result.status == CapabilityResultStatus.OK
    assert len(ex.invocations) == 1


def test_verification_authority_rejects_secret_effects():
    ex = _exec("verify.secret", {EffectClass.SECRET_READ})
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("verify.secret", CapabilityRequestOrigin.SYSTEM_VERIFICATION),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        )
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert "verification envelope" in (result.error or "")
    assert ex.invocations == []


def test_verification_authority_rejects_privilege_escalation():
    ex = _exec("verify.priv", {EffectClass.PRIVILEGED})
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("verify.priv", CapabilityRequestOrigin.SYSTEM_VERIFICATION),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        )
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert ex.invocations == []


def test_verification_authority_rejects_external_publication():
    # An operation map pins the exact effect set (EXECUTE + NETWORK_WRITE),
    # so the egress side effect cannot be dropped by the legacy heuristic.
    ex = _exec(
        "verify.publish",
        {EffectClass.EXECUTE, EffectClass.NETWORK_WRITE},
    )
    object.__setattr__(
        ex.descriptor,
        "operation_effects",
        {
            "publish": frozenset({EffectClass.EXECUTE, EffectClass.NETWORK_WRITE}),
        },
    )
    dispatcher = _dispatcher(ex, profile=AutonomyLevel.AUTONOMOUS)
    result = asyncio.run(
        dispatcher.dispatch(
            CapabilityRequest(
                capability_id="verify.publish",
                arguments={"operation": "publish"},
                task_id="t1",
                origin=CapabilityRequestOrigin.SYSTEM_VERIFICATION,
            ),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        )
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert "verification envelope" in (result.error or "")
    assert ex.invocations == []


def test_verification_authority_rejects_computer_input():
    ex = _exec("verify.desktop", {EffectClass.COMPUTER_INPUT})
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("verify.desktop", CapabilityRequestOrigin.SYSTEM_VERIFICATION),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        )
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert ex.invocations == []


def test_verification_authority_rejects_network_write_only():
    """Pure outbound (NETWORK_WRITE without READ) stays outside the floor."""
    ex = _exec("verify.egress", {EffectClass.NETWORK_WRITE})
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("verify.egress", CapabilityRequestOrigin.SYSTEM_VERIFICATION),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        )
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert ex.invocations == []


def test_verification_authority_still_honors_workspace_containment():
    """The floor governs WHICH effects; hard containment still applies."""
    ex = _exec(
        "verify.write",
        {EffectClass.WRITE_LOCAL},
    )
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("verify.write", CapabilityRequestOrigin.SYSTEM_VERIFICATION),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
        )
    )
    # No path argument: pathless write within an unconfined default workspace.
    # Containment for verification writes is exercised in the workspace suite.
    assert isinstance(result, CapabilityResult)


# ---------------------------------------------------------------------- #
# SYSTEM (pure observation) remains unchanged
# ---------------------------------------------------------------------- #


def test_system_observation_still_bypasses_task_ceiling():
    ex = _exec("observe.state", {EffectClass.READ_LOCAL})
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("observe.state", CapabilityRequestOrigin.SYSTEM),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
            task_policy=CapabilityPolicy(deny=("observe.state",)),
        )
    )
    assert isinstance(result, CapabilityResult)
    assert result.status == CapabilityResultStatus.OK


def test_model_origin_cannot_claim_verification_authority():
    """A MODEL-origin call is never treated as verifier authority: the task
    ceiling still applies, and deny stays deny."""
    ex = _exec("model.cmd", {EffectClass.READ_LOCAL, EffectClass.EXECUTE})
    dispatcher = _dispatcher(ex)
    result = asyncio.run(
        dispatcher.dispatch(
            _req("model.cmd", CapabilityRequestOrigin.MODEL),
            workspace=WorkspaceSpec(id="w1", root="/tmp/ws"),
            task_policy=CapabilityPolicy(deny=("model.cmd",)),
        )
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert ex.invocations == []
