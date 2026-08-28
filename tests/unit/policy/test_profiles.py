from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import EffectClass
from athena.protocol.policy import (
    PolicyDecision,
    PolicyRequest,
    PolicyVerdict,
    Principal,
)
from athena.protocol.tasks import AutonomyLevel, WorkspaceSpec, PathRule


def _req(
    capability_id: str,
    effects,
    *,
    task_id="t1",
    arguments=None,
    workspace=None,
) -> PolicyRequest:
    return PolicyRequest(
        principal=Principal("agent", "athena"),
        task_id=task_id,
        capability_id=capability_id,
        arguments=arguments or {},
        workspace=workspace or WorkspaceSpec(id="w1", root="/tmp/ws"),
        execution_backend="local",
        effects=frozenset(effects),
    )


def test_supervised_network_write_is_ask():
    engine = PolicyEngine(AutonomyLevel.SUPERVISED)
    decision: PolicyDecision = engine.evaluate(
        _req("net", {EffectClass.NETWORK_WRITE}, arguments={"url": "https://example.com"})
    )
    assert decision.decision is PolicyVerdict.ASK


def test_autonomous_network_write_still_requires_approval():
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    decision = engine.evaluate(
        _req(
            "network",
            {EffectClass.NETWORK_WRITE},
            arguments={"operation": "http", "method": "POST", "url": "https://example.com"},
        )
    )
    assert decision.decision is PolicyVerdict.ASK


def test_coding_in_workspace_write_allowed():
    engine = PolicyEngine(AutonomyLevel.CODING)
    ws = WorkspaceSpec(
        id="w1",
        root="/tmp/ws",
        writable=(PathRule("/tmp/ws/**"),),
        readable=(PathRule("/tmp/ws/**"),),
    )
    decision = engine.evaluate(
        _req(
            "files.write",
            {EffectClass.WRITE_LOCAL},
            workspace=ws,
            arguments={"operation": "write", "path": "/tmp/ws/notes.txt"},
        )
    )
    assert decision.decision is PolicyVerdict.ALLOW


def test_coding_out_of_workspace_write_denied():
    engine = PolicyEngine(AutonomyLevel.CODING)
    ws = WorkspaceSpec(id="w1", root="/tmp/ws", writable=(PathRule("/tmp/ws/**"),))
    decision = engine.evaluate(
        _req(
            "files.write",
            {EffectClass.WRITE_LOCAL},
            workspace=ws,
            arguments={"operation": "write", "path": "/etc/hosts"},
        )
    )
    assert decision.decision is PolicyVerdict.DENY


def test_autonomous_out_of_workspace_arbitrary_shell_not_auto_allowed():
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    decision = engine.evaluate(
        _req(
            "shell",
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
            arguments={"code": "rm -rf /important", "cwd": "/outside"},
        )
    )
    assert decision.decision is PolicyVerdict.DENY


def test_autonomous_build_command_out_of_workspace_allowed():
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    decision = engine.evaluate(
        _req(
            "shell",
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
            arguments={"code": "pytest -q", "cwd": "/other/repo"},
        )
    )
    assert decision.decision is PolicyVerdict.ALLOW


def test_execute_denied_when_network_policy_deny():
    from athena.protocol.tasks import NetworkPolicy

    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    ws = WorkspaceSpec(id="w1", root="/tmp/ws", network_policy=NetworkPolicy.DENY)
    decision = engine.evaluate(
        _req(
            "shell",
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
            workspace=ws,
            arguments={"code": "pytest -q"},
        )
    )
    assert decision.decision is PolicyVerdict.DENY


def test_execute_allowed_when_network_policy_allow():
    engine = PolicyEngine(AutonomyLevel.AUTONOMOUS)
    ws = WorkspaceSpec(id="w1", root="/tmp/ws", writable=(PathRule("/tmp/ws/**"),))
    decision = engine.evaluate(
        _req(
            "shell",
            {EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS},
            workspace=ws,
            arguments={"code": "echo hi"},
        )
    )
    assert decision.decision is PolicyVerdict.ALLOW
