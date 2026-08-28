"""No-policy-bypass invariants (INV-004 / INV-008 / BHV-043).

Task capability policy is a hard ceiling: even if the global/profile policy
would allow an operation, the task's ``deny`` wins. Shell execution that would
escape the workspace is denied unless the autonomy profile explicitly grants it.
The offline profile rejects remote model providers.
"""

from __future__ import annotations

import pytest

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.policy import PolicyRequest, PolicyVerdict, Principal
from athena.protocol.tasks import (
    AutonomyLevel,
    CapabilityPolicy,
    WorkspaceSpec,
)


def _wk(root="/workspace") -> WorkspaceSpec:
    return WorkspaceSpec(id="w", root=root)


def _execute_args(cwd=None, code="echo hi"):
    args = {"language": "shell", "code": code}
    if cwd:
        args["cwd"] = cwd
    return args


class _RecordingExecutor:
    def __init__(self, cap_id: str, effects) -> None:
        self.descriptor = CapabilityDescriptor(
            id=cap_id,
            description=cap_id,
            input_schema={"allow_extra": True, "properties": {}},
            effects=frozenset(effects),
        )
        self.invocations = 0

    async def invoke(self, request, *, output_accumulator=None, context=None):
        self.invocations += 1
        return CapabilityResult(
            request.call_id, request.capability_id, CapabilityResultStatus.OK, output="ok"
        )


@pytest.mark.athena_claim("INV-004")
@pytest.mark.athena_evidence("security", "invariant")
@pytest.mark.athena_scenario("AUTH-004")
async def test_task_deny_is_hard_ceiling_even_when_global_allows():
    """execute allowed globally (coding) but task deny -> DENY, no effect."""
    reg = CapabilityRegistry()
    exec_ = _RecordingExecutor("execute", (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS))
    reg.register(exec_)
    dispatcher = CapabilityDispatcher(reg, PolicyEngine(profile=AutonomyLevel.CODING))

    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="execute",
            arguments={"language": "shell", "code": "ls"},
            task_id="t1",
        ),
        workspace=_wk(),
        task_policy=CapabilityPolicy(deny=("execute",)),
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert exec_.invocations == 0


@pytest.mark.athena_claim("INV-004")
@pytest.mark.athena_evidence("security")
async def test_execute_outside_workspace_without_grant_is_denied():
    """Coding profile allows execute generally, but an escape is still denied."""
    reg = CapabilityRegistry()
    exec_ = _RecordingExecutor("execute", (EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS))
    reg.register(exec_)
    dispatcher = CapabilityDispatcher(reg, PolicyEngine(profile=AutonomyLevel.CODING))

    result = await dispatcher.dispatch(
        CapabilityRequest(
            capability_id="execute",
            arguments={
                "language": "shell",
                "code": "make",
                "cwd": "/etc",
            },
            task_id="t1",
        ),
        workspace=_wk(),
    )
    assert result.status == CapabilityResultStatus.FAILED
    assert exec_.invocations == 0


@pytest.mark.athena_claim("BHV-008")
@pytest.mark.athena_evidence("test", "security")
def test_autonomous_grant_allows_build_cmd_outside_workspace():
    """INV-008: autonomous profile grants build/test commands outside ws."""
    engine = PolicyEngine(profile=AutonomyLevel.AUTONOMOUS)
    from athena.protocol.policy import PolicyVerdict as PV

    decision = engine.evaluate(
        PolicyRequest(
            principal=Principal("agent", "athena"),
            task_id="t1",
            capability_id="execute",
            arguments={"language": "shell", "code": "npm run build", "cwd": "/etc"},
            workspace=_wk(),
            effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        )
    )
    assert decision.decision == PV.ALLOW


@pytest.mark.athena_claim("BHV-038")
@pytest.mark.athena_evidence("test", "security")
def test_offline_profile_rejects_remote_model_inference():
    """BHV-038: offline profile denies remote model provider use."""
    engine = PolicyEngine(profile=AutonomyLevel.OFFLINE)
    decision = engine.evaluate(
        PolicyRequest(
            principal=Principal("agent", "athena"),
            task_id="t1",
            capability_id="model",
            arguments={"resource": "remote", "provider": "anthropic"},
            workspace=_wk(),
            effects=frozenset(),
        )
    )
    assert decision.decision == PolicyVerdict.DENY
    # local inference is allowed in offline (rule priority 80).
    local = engine.evaluate(
        PolicyRequest(
            principal=Principal("agent", "athena"),
            task_id="t1",
            capability_id="model",
            arguments={"resource": "local"},
            workspace=_wk(),
            effects=frozenset(),
        )
    )
    assert local.decision == PolicyVerdict.ALLOW
