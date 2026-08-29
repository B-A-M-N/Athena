"""Adversarial self-host proof: candidate first, operator-controlled apply."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import replace as dc_replace

import pytest

from athena.execution.manager import ExecutionManager
from athena.execution.runtimes import PythonRuntime
from athena.protocol.execution import ExecutionExitStatus, ExecutionRequest
from athena.protocol.tasks import AgentRequest, MutationMode, NetworkPolicy
from athena.reality import RealityGate
from athena.service.service import AthenaService
from athena.shadow.engine import BranchStatus, ShadowEngine


@pytest.fixture
async def service(tmp_path):
    svc = AthenaService.in_memory()
    await svc.start()
    workspace = dc_replace(svc._default_workspace, root=str(tmp_path / "workspace"))
    object.__setattr__(svc, "_default_workspace", workspace)
    os.makedirs(workspace.root, exist_ok=True)
    try:
        yield svc, workspace
    finally:
        await svc.stop()


async def _task(svc, workspace) -> str:
    spec = await svc.submit(AgentRequest(prompt="self-host proof", workspace=workspace), wait=True)
    return spec.id


def _write(path: str, content: str) -> dict:
    return {
        "capability_id": "fs",
        "arguments": {"operation": "write", "path": path, "content": content},
    }


async def _verified_candidate(svc, workspace, *, proposal: list[dict]) -> tuple[str, object]:
    task_id = await _task(svc, workspace)
    branch = await svc.shadow_engine().open_branch(
        task_id=task_id,
        base_workspace=workspace,
        proposal=proposal,
    )
    branch = await svc.shadow_engine().execute_branch(branch, profile="autonomous")
    await svc.shadow_engine().record_verification(branch, [{"id": "proof", "passed": True}])
    svc._reality_gate.activate_branch(branch)
    return task_id, branch


async def test_self_host_review_apply_keeps_base_untouched_until_operator_apply(service):
    svc, workspace = service
    task_id, branch = await _verified_candidate(
        svc,
        workspace,
        proposal=[_write("candidate.py", "VALUE = 'candidate'\n")],
    )

    assert branch.status == BranchStatus.VERIFIED
    assert not (Path(workspace.root) / "candidate.py").exists()
    assert (Path(branch.shadow_workspace.root) / "candidate.py").exists()

    review = await svc.operator_candidate(task_id)
    assert review is not None
    assert review["status"] == BranchStatus.VERIFIED
    outcome = await svc.apply_candidate(task_id)

    assert outcome["status"] == "committed"
    assert (Path(workspace.root) / "candidate.py").read_text() == "VALUE = 'candidate'\n"


async def test_self_host_failed_verification_cannot_apply_or_change_base(service):
    svc, workspace = service
    task_id = await _task(svc, workspace)
    branch = await svc.shadow_engine().open_branch(
        task_id=task_id,
        base_workspace=workspace,
        proposal=[_write("rejected.py", "never\n")],
    )
    branch = await svc.shadow_engine().execute_branch(branch, profile="autonomous")
    await svc.shadow_engine().record_verification(branch, [{"id": "proof", "passed": False}])

    outcome = await svc.apply_candidate(task_id)

    assert branch.status == BranchStatus.FAILED
    assert outcome["status"] == "missing"
    assert not (Path(workspace.root) / "rejected.py").exists()


async def test_self_host_stale_base_conflict_preserves_candidate_and_unrelated_edit(service):
    svc, workspace = service
    (Path(workspace.root) / "README.md").write_text("base\n")
    (Path(workspace.root) / "unrelated.txt").write_text("base\n")
    task_id, branch = await _verified_candidate(
        svc,
        workspace,
        proposal=[_write("README.md", "candidate\n")],
    )

    (Path(workspace.root) / "README.md").write_text("concurrent\n")
    (Path(workspace.root) / "unrelated.txt").write_text("unrelated edit\n")
    outcome = await svc.apply_candidate(task_id)

    assert outcome["status"] == "CONFLICT"
    assert branch.status == BranchStatus.CONFLICTED
    assert (Path(workspace.root) / "README.md").read_text() == "concurrent\n"
    assert (Path(workspace.root) / "unrelated.txt").read_text() == "unrelated edit\n"
    assert Path(branch.shadow_workspace.root).is_dir()


async def test_self_host_discard_removes_multi_file_candidate_and_preserves_base(service):
    svc, workspace = service
    before = {
        path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in Path(workspace.root).rglob("*")
        if path.is_file()
    }
    task_id, branch = await _verified_candidate(
        svc,
        workspace,
        proposal=[_write("one.py", "1\n"), _write("nested/two.py", "2\n")],
    )
    candidate_root = Path(branch.shadow_workspace.root)

    outcome = await svc.discard_candidate(task_id)

    after = {
        path.relative_to(workspace.root).as_posix(): path.read_bytes()
        for path in Path(workspace.root).rglob("*")
        if path.is_file()
    }
    assert outcome["status"] == "discarded"
    assert after == before
    assert not candidate_root.exists()


async def test_self_host_restart_rehydrates_verified_candidate_before_apply(service):
    svc, workspace = service
    task_id, branch = await _verified_candidate(
        svc,
        workspace,
        proposal=[_write("restart.py", "survived\n")],
    )
    engine = svc.shadow_engine()
    restored_engine = ShadowEngine(
        dispatcher=svc._dispatcher,
        roots_parent=str(engine._roots_parent),
        state_root=str(engine._state_root),
    )
    restored_gate = RealityGate(restored_engine)
    svc._dispatcher.set_reality_gate(restored_gate)
    restored = restored_engine.get_branch(branch.id)

    assert restored is not None
    assert restored.status == BranchStatus.VERIFIED
    assert restored.verification_certificate
    assert restored_gate.active_branch(task_id) is restored
    await restored_gate.deactivate_branch(task_id)
    outcome = await restored_engine.commit(restored)

    assert outcome["status"] == "committed"
    assert (Path(workspace.root) / "restart.py").read_text() == "survived\n"


async def test_self_host_python_verification_imports_candidate_code(tmp_path):
    candidate = tmp_path / "candidate"
    source = candidate / "src" / "athena"
    source.mkdir(parents=True)
    init = source / "__init__.py"
    init.write_text("MARKER = 'candidate'\n", encoding="utf-8")
    manager = ExecutionManager()
    manager.register_runtime(PythonRuntime())

    result = await manager.execute(
        ExecutionRequest(
            runtime="python",
            source="import athena; print(athena.__file__)",
            task_id="self-host-import",
            workspace_id="candidate",
            backend="local",
            cwd=str(candidate),
            env={"PYTHONPATH": str(candidate / "src")},
        )
    )

    assert result.status is ExecutionExitStatus.EXITED
    assert Path(result.stdout.strip()).resolve() == init.resolve()


def test_self_host_direct_metadata_cannot_escape_candidate_boundary():
    svc = AthenaService.in_memory()
    spec = svc._build_task_spec(
        AgentRequest(
            prompt="self-host escape",
            metadata={"self_host": True, "mutation_mode": "direct", "network_policy": "allow"},
        ),
        "self-host-session",
    )

    assert spec.workspace.mutation_mode is MutationMode.SPECULATIVE
    assert spec.workspace.network_policy is NetworkPolicy.DENY
    assert spec.metadata["review_before_commit"] is True
