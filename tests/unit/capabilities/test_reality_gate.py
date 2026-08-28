"""Dispatcher-path coverage for the task workspace reality boundary."""

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
    AgentRequest,
    AutonomyLevel,
    MutationMode,
    WorkspaceSpec,
)
from athena.reality import RealityGate
from athena.service.service import AthenaService
from athena.shadow.engine import ShadowEngine


class _OpaqueExecutor:
    """A stand-in for arbitrary execute/generated code.

    It deliberately writes through the context workspace so the test proves
    the dispatcher boundary, not a particular shell parser, owns isolation.
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


def _request(capability_id: str, arguments: dict, call_id: str) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id=capability_id,
        arguments=arguments,
        task_id="task-reality-gate",
        call_id=call_id,
    )


async def test_speculative_workspace_is_lazy_sticky_and_coherent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.txt").write_text("base\n", encoding="utf-8")
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

    initial = await dispatcher.dispatch(
        _request("fs", {"operation": "read", "path": "README.txt"}, "read-1"),
        workspace=workspace,
    )
    assert initial.status is CapabilityResultStatus.OK
    assert initial.output == "base\n"
    assert gate.active_branch("task-reality-gate") is None

    written = await dispatcher.dispatch(
        _request(
            "fs",
            {
                "operation": "write",
                "path": "README.txt",
                "content": "candidate\n",
            },
            "write-1",
        ),
        workspace=workspace,
        profile="autonomous",
    )
    assert written.status is CapabilityResultStatus.OK
    assert (project / "README.txt").read_text(encoding="utf-8") == "base\n"
    transaction_id = written.metadata["reality"]["transaction_id"]
    branch = gate.active_branch("task-reality-gate")
    assert branch is not None and branch.id == transaction_id

    observed = await dispatcher.dispatch(
        _request("fs", {"operation": "read", "path": "README.txt"}, "read-2"),
        workspace=workspace,
    )
    assert observed.status is CapabilityResultStatus.OK
    assert observed.output == "candidate\n"
    assert observed.metadata["reality"]["transaction_id"] == transaction_id

    await engine.discard(branch, reason="test cleanup")


async def test_opaque_execution_cannot_write_the_real_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    workspace = WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=MutationMode.SPECULATIVE,
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
        _request(
            "execute",
            {"language": "shell", "code": "rewrite project"},
            "execute-1",
        ),
        workspace=workspace,
        profile="autonomous",
    )
    assert result.status is CapabilityResultStatus.OK
    assert not (project / "opaque-write.txt").exists()
    branch = gate.active_branch("task-reality-gate")
    assert branch is not None
    assert (Path(branch.shadow_workspace.root) / "opaque-write.txt").exists()
    await engine.discard(branch, reason="test cleanup")


def test_coding_tasks_default_to_speculative_workspace():
    service = AthenaService.in_memory()
    spec = service._build_task_spec(
        AgentRequest(prompt="fix the parser", autonomy=AutonomyLevel.CODING),
        "session-reality-gate",
    )
    assert spec.workspace is not None
    assert spec.workspace.mutation_mode is MutationMode.SPECULATIVE

    direct = service._build_task_spec(
        AgentRequest(
            prompt="run a direct maintenance operation",
            autonomy=AutonomyLevel.CODING,
            metadata={"mutation_mode": "direct"},
        ),
        "session-reality-gate-direct",
    )
    assert direct.workspace is not None
    assert direct.workspace.mutation_mode is MutationMode.DIRECT
