"""RealityGate opaque execution authority regression tests.

Verifies that opaque execution (arbitrary shell/Python/code, PTY,
generated capabilities) cannot reach the real workspace when the task
uses DIRECT mutation mode — the effects the capability declares are
not the effects arbitrary code can actually perform.
"""

from __future__ import annotations

from pathlib import Path

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    InvocationContext,
)
from athena.protocol.tasks import (
    MutationMode,
    NetworkPolicy,
    WorkspaceSpec,
)
from athena.reality import RealityGate
from athena.reality.request_risk import is_opaque_execution
from athena.shadow.engine import ShadowEngine


class _OpaqueExecutor:
    """Stand-in for arbitrary execute/generated code.

    Deliberately writes through the context workspace so the test proves
    the gate, not a particular shell parser, owns isolation.
    """

    descriptor = CapabilityDescriptor(
        id="execute",
        description="opaque execution test double",
        input_schema={
            "type": "object",
            "required": ["language", "code"],
            "properties": {
                "language": {"type": "string"},
                "code": {"type": "string"},
            },
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.EXECUTE, EffectClass.SPAWN_PROCESS}),
        origin=CapabilityOrigin.NATIVE,
    )

    async def invoke(self, request, *, context: InvocationContext | None = None, **_):
        assert context is not None
        target = Path(context.workspace.root) / "opaque-write.txt"
        target.write_text("candidate\n", encoding="utf-8")
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="wrote candidate",
        )


class _GeneratedExecutor:
    """A generated capability with a deceptively narrow effect envelope."""

    descriptor = CapabilityDescriptor(
        id="gen.run",
        description="generated capability test double",
        input_schema={
            "type": "object",
            "required": ["code"],
            "properties": {"code": {"type": "string"}},
            "additionalProperties": False,
        },
        effects=frozenset({EffectClass.EXECUTE}),
        origin=CapabilityOrigin.GENERATED,
    )

    async def invoke(self, request, *, context: InvocationContext | None = None, **_):
        assert context is not None
        target = Path(context.workspace.root) / "gen-write.txt"
        target.write_text("generated\n", encoding="utf-8")
        return CapabilityResult(
            request.call_id,
            request.capability_id,
            CapabilityResultStatus.OK,
            output="generated write",
        )


def _request(capability_id: str, arguments: dict, call_id: str) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=capability_id,
        arguments=arguments,
        task_id="task-opaque",
        call_id=call_id,
    )


async def test_opaque_execute_direct_mode_cannot_write_real_workspace(tmp_path):
    """The core P0 regression: opaque execution in DIRECT mode must still
    enter the candidate path so the real workspace is protected from
    undeclared mutations."""
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.DIRECT,
    )
    registry = CapabilityRegistry()
    registry.register(_OpaqueExecutor())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(engine)
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)

    result = await dispatcher.dispatch(
        _request("execute", {"language": "shell", "code": "rewrite project"}, "exec-1"),
        workspace=workspace,
        profile="autonomous",
    )
    assert result.status is CapabilityResultStatus.OK
    # Real workspace is untouched; write landed on the candidate branch.
    assert not (project / "opaque-write.txt").exists()
    branch = gate.active_branch("task-opaque")
    assert branch is not None
    assert (Path(branch.shadow_workspace.root) / "opaque-write.txt").exists()
    await engine.discard(branch, reason="test cleanup")


async def test_generated_capability_direct_mode_cannot_write_real_workspace(tmp_path):
    """A generated capability with EXECUTE in DIRECT mode must also enter
    the candidate path."""
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.DIRECT,
    )
    registry = CapabilityRegistry()
    registry.register(_GeneratedExecutor())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(engine)
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)

    result = await dispatcher.dispatch(
        _request("gen.run", {"code": "write something"}, "gen-1"),
        workspace=workspace,
        profile="autonomous",
    )
    assert result.status is CapabilityResultStatus.OK
    assert not (project / "gen-write.txt").exists()
    branch = gate.active_branch("task-opaque")
    assert branch is not None
    assert (Path(branch.shadow_workspace.root) / "gen-write.txt").exists()
    await engine.discard(branch, reason="test cleanup")


async def test_non_opaque_direct_write_still_reaches_real_workspace(tmp_path):
    """Transparent declared-path fs.write in DIRECT mode must still reach
    the real workspace — the opaque-execution fix must not over-broaden."""
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.DIRECT,
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(engine)
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)

    result = await dispatcher.dispatch(
        _request("fs", {"operation": "write", "path": "real.txt", "content": "real"}, "write-1"),
        workspace=workspace,
        profile="autonomous",
    )
    assert result.status is CapabilityResultStatus.OK
    # Real workspace IS mutated because this is a transparent declared-path write.
    assert (project / "real.txt").read_text(encoding="utf-8") == "real"
    assert gate.active_branch("task-opaque") is None


async def test_opaque_execute_network_deny_blocks_all_outbound(tmp_path):
    """Opaque execution in a network-DENY workspace cannot reach the network.
    The universal network boundary applies regardless of capability origin."""
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.DIRECT,
        network_policy=NetworkPolicy.DENY,
    )
    registry = CapabilityRegistry()
    registry.register(_OpaqueExecutor())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(engine)
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)

    result = await dispatcher.dispatch(
        _request("execute", {"language": "shell", "code": "curl example.com"}, "exec-net"),
        workspace=workspace,
        profile="autonomous",
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert "network_policy is DENY" in (result.error or "")


async def test_pty_session_inherits_candidate_workspace(tmp_path):
    """A PTY session created against a candidate workspace must remain
    bound to that candidate for its lifetime (no escape to real workspace
    via the PTY)."""
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
    )
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(engine)
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)

    # Write triggers speculative branch creation.
    write = await dispatcher.dispatch(
        _request("fs", {"operation": "write", "path": "a.txt", "content": "a"}, "write-a"),
        workspace=workspace,
        profile="autonomous",
    )
    assert write.status is CapabilityResultStatus.OK
    branch = gate.active_branch("task-opaque")
    assert branch is not None

    # Another opaque execute against the same task must land on the same
    # candidate branch, not the real workspace.
    registry.register(_OpaqueExecutor())
    result = await dispatcher.dispatch(
        _request("execute", {"language": "shell", "code": "echo hi"}, "exec-2"),
        workspace=workspace,
        profile="autonomous",
    )
    assert result.status is CapabilityResultStatus.OK
    assert not (project / "opaque-write.txt").exists()
    # Still on the same branch.
    assert gate.active_branch("task-opaque").id == branch.id
    await engine.discard(branch, reason="test cleanup")


def test_is_opaque_execution_classifies_correctly():
    """Unit-level classification of opaque execution."""
    desc = CapabilityDescriptor(
        id="x",
        description="d",
        input_schema={},
        effects=frozenset(),
        origin=CapabilityOrigin.NATIVE,
    )
    assert is_opaque_execution(
        CapabilityRequest(capability_id="execute", arguments={}, task_id="t", call_id="c"),
        frozenset({EffectClass.EXECUTE}),
        desc,
    )
    assert is_opaque_execution(
        CapabilityRequest(capability_id="shell", arguments={}, task_id="t", call_id="c"),
        frozenset({EffectClass.SPAWN_PROCESS}),
        desc,
    )
    assert is_opaque_execution(
        CapabilityRequest(capability_id="gen.run", arguments={}, task_id="t", call_id="c"),
        frozenset({EffectClass.READ_LOCAL}),
        CapabilityDescriptor(
            id="gen.run",
            description="d",
            input_schema={},
            effects=frozenset(),
            origin=CapabilityOrigin.GENERATED,
        ),
    )
    # Transparent fs.write is not opaque.
    assert not is_opaque_execution(
        CapabilityRequest(capability_id="fs", arguments={}, task_id="t", call_id="c"),
        frozenset({EffectClass.WRITE_LOCAL}),
        desc,
    )
