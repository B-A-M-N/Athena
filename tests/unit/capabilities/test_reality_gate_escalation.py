"""Escalation-ladder coverage for the RealityGate dispositions.

Proves the four ExecutionDispositions are all reachable and behave as
contractual reality boundaries, not just enum labels.
"""

from __future__ import annotations

from pathlib import Path

from athena.capabilities.dispatcher import CapabilityDispatcher
from athena.capabilities.fs import FilesystemCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.causal.checkpoint import CheckpointManager
from athena.policy.engine import PolicyEngine
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    DispatchDirectives,
    EffectClass,
)
from athena.protocol.tasks import (
    AgentRequest,
    AutonomyLevel,
    MutationMode,
    WorkspaceSpec,
)
from athena.reality import ExecutionDisposition, RealityGate
from athena.service.service import AthenaService
from athena.shadow.engine import ShadowEngine


def _ws(tmp_path: Path, *, mode: MutationMode) -> WorkspaceSpec:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.txt").write_text("base\n", encoding="utf-8")
    return WorkspaceSpec(
        id="project",
        root=str(project),
        mutation_mode=mode,
    )


def _request(capability_id: str, arguments: dict, call_id: str):
    return CapabilityRequest(
        capability_id=capability_id,
        arguments=arguments,
        task_id="task-escalation",
        call_id=call_id,
        origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
    )


def _gate(tmp_path: Path, *, with_checkpoints: bool = False):
    registry = CapabilityRegistry()
    registry.register(FilesystemCapability())
    dispatcher = CapabilityDispatcher(registry, PolicyEngine(profile="offline"))
    engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
    )
    gate = RealityGate(engine)
    if with_checkpoints:
        gate.bind_checkpoint_manager(CheckpointManager(root=str(tmp_path / "ckpts")))
    engine.bind(dispatcher)
    dispatcher.set_reality_gate(gate)
    return dispatcher, engine, gate


async def test_isolated_write_does_not_join_task_branch(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path)

    route = await gate.route(
        _request(
            "fs", {"operation": "write", "path": "README.txt", "content": "isolated\n"}, "write-iso"
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="isolated",
    )
    assert route.disposition is ExecutionDisposition.ISOLATED
    assert gate.active_branch("task-escalation") is None
    assert gate.ephemeral_branch("write-iso") is not None

    # The real project is untouched by the isolated call's shadow.
    assert (Path(ws.root) / "README.txt").read_text(encoding="utf-8") == "base\n"
    await gate.discard_ephemeral("write-iso")
    assert gate.ephemeral_branch("write-iso") is None


async def test_dispatcher_discards_isolated_write_before_returning(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path)

    result = await dispatcher.dispatch(
        _request(
            "fs",
            {"operation": "write", "path": "README.txt", "content": "isolated\n"},
            "dispatch-iso",
        ),
        workspace=ws,
        profile="autonomous",
        _directives=DispatchDirectives(reality_tier="isolated"),
    )

    assert result.status.value == "ok"
    assert gate.ephemeral_branch("dispatch-iso") is None
    assert (Path(ws.root) / "README.txt").read_text(encoding="utf-8") == "base\n"
    assert any(branch.status == "DISCARDED" for branch in engine.list_branches())


async def test_transactional_checkpoints_real_workspace_and_compensates(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path, with_checkpoints=True)

    route = await gate.route(
        _request(
            "fs", {"operation": "write", "path": "README.txt", "content": "changed\n"}, "write-txn"
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    assert route.disposition is ExecutionDisposition.TRANSACTIONAL
    assert route.checkpoint_id is not None
    assert gate.checkpoint_id("task-escalation") == route.checkpoint_id

    # Execute through the canonical dispatcher so the transaction records the
    # exact post-state it owns.  Direct external writes are intentionally not
    # eligible for compensation under the strict recovery contract.
    target = Path(ws.root) / "README.txt"
    result = await dispatcher.dispatch(
        _request(
            "fs",
            {"operation": "write", "path": "README.txt", "content": "changed\n"},
            "write-txn-dispatch",
        ),
        workspace=ws,
        profile="autonomous",
        _directives=DispatchDirectives(reality_tier="transactional"),
    )
    assert result.status.value == "ok"
    assert target.read_text(encoding="utf-8") == "changed\n"

    # compensate rolls the project back to the captured revision.
    ok = await gate.compensate("task-escalation")
    assert ok is True
    assert target.read_text(encoding="utf-8") == "base\n"
    assert gate.checkpoint_id("task-escalation") is None


async def test_transactional_without_checkpoints_falls_back_to_speculative(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path, with_checkpoints=False)

    route = await gate.route(
        _request(
            "fs", {"operation": "write", "path": "README.txt", "content": "changed\n"}, "write-txn"
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="transactional",
    )
    # No checkpoint backend: must not perform an unrecoverable in-place change.
    assert route.disposition is ExecutionDisposition.SPECULATIVE
    assert route.transaction_id is not None
    branch = gate.active_branch("task-escalation")
    assert branch is not None
    await engine.discard(branch, reason="cleanup")


async def test_single_reversible_write_defaults_to_speculative(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path)

    route = await gate.route(
        _request(
            "fs", {"operation": "write", "path": "README.txt", "content": "x\n"}, "write-default"
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    # Default (no forced tier) must preserve the original contract: a
    # project-sensitive mutation opens the sticky candidate branch, not an
    # in-place change.
    assert route.disposition is ExecutionDisposition.SPECULATIVE
    assert route.transaction_id is not None
    branch = gate.active_branch("task-escalation")
    assert branch is not None
    await engine.discard(branch, reason="cleanup")


async def test_unknown_tier_is_ignored_and_defaults_to_speculative(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path)

    route = await gate.route(
        _request(
            "fs", {"operation": "write", "path": "README.txt", "content": "x\n"}, "write-bogus"
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
        tier="not-a-real-tier",
    )
    assert route.disposition is ExecutionDisposition.SPECULATIVE
    branch = gate.active_branch("task-escalation")
    assert branch is not None
    await engine.discard(branch, reason="cleanup")


async def test_speculative_branch_rehydrates_to_same_candidate_after_restart(tmp_path):
    ws = _ws(tmp_path, mode=MutationMode.SPECULATIVE)
    dispatcher, engine, gate = _gate(tmp_path)

    await gate.route(
        _request(
            "fs",
            {"operation": "write", "path": "README.txt", "content": "candidate\n"},
            "write-restart",
        ),
        ws,
        {EffectClass.WRITE_LOCAL},
        FilesystemCapability().descriptor,
    )
    branch = gate.active_branch("task-escalation")
    assert branch is not None
    candidate_root = branch.shadow_workspace.root

    restored_engine = ShadowEngine(
        roots_parent=str(tmp_path / "shadows"),
        state_root=str(tmp_path / "state"),
        dispatcher=dispatcher,
    )
    restored_gate = RealityGate(restored_engine)
    restored = restored_gate.active_branch("task-escalation")

    assert restored is not None
    assert restored.shadow_workspace.root == candidate_root
    resumed = await restored_gate.route(
        _request("fs", {"operation": "read", "path": "README.txt"}, "read-restart"),
        ws,
        {EffectClass.READ_LOCAL},
        FilesystemCapability().descriptor,
    )
    assert resumed.workspace.root == candidate_root


def _noop_branch():
    return None


def test_coding_tasks_default_to_speculative_workspace():
    service = AthenaService.in_memory()
    spec = service._build_task_spec(
        AgentRequest(prompt="fix the parser", autonomy=AutonomyLevel.CODING),
        "session-escalation",
    )
    assert spec.workspace is not None
    assert spec.workspace.mutation_mode is MutationMode.SPECULATIVE
