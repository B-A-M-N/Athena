"""Regression tests for content-based shadow diffs and commit conflicts."""

from __future__ import annotations

from pathlib import Path

import pytest

from athena.protocol.tasks import WorkspaceSpec
from athena.shadow.engine import BranchStatus, ShadowBranch, ShadowEngine


def _branch(base: Path, shadow: Path, manifest: dict[str, str]) -> ShadowBranch:
    return ShadowBranch(
        id="branch-test",
        task_id="task-test",
        base_workspace=WorkspaceSpec(id="base", root=str(base)),
        shadow_workspace=WorkspaceSpec(id="shadow", root=str(shadow)),
        status=BranchStatus.VERIFIED,
        base_manifest=manifest,
    )


def test_shadow_diff_detects_same_size_content_edit(tmp_path):
    base = tmp_path / "base"
    shadow = tmp_path / "shadow"
    base.mkdir()
    shadow.mkdir()
    (base / "state.json").write_text("true", encoding="utf-8")
    (shadow / "state.json").write_text("null", encoding="utf-8")

    engine = ShadowEngine(roots_parent=str(tmp_path / "branches"))
    branch = _branch(base, shadow, engine._manifest(str(base)))

    changes = engine._diff_trees(branch)

    assert changes["modified"] == ["state.json"]
    assert changes["base_hashes"]["state.json"] == engine._manifest(str(base))["state.json"]


def test_shadow_conflict_rejects_same_size_concurrent_edit(tmp_path):
    base = tmp_path / "base"
    shadow = tmp_path / "shadow"
    base.mkdir()
    shadow.mkdir()
    (base / "state.json").write_text("true", encoding="utf-8")
    (shadow / "state.json").write_text("null", encoding="utf-8")

    engine = ShadowEngine(roots_parent=str(tmp_path / "branches"))
    branch = _branch(base, shadow, engine._manifest(str(base)))
    # The real workspace changes after the branch was opened, but the edit is
    # deliberately the same size as the branch's proposed edit.
    (base / "state.json").write_text("null", encoding="utf-8")

    conflicts = engine._conflicts(
        str(base),
        engine._diff_trees(branch)["base_hashes"],
    )

    assert conflicts == [
        {"resource": "state.json", "reason": "modified_elsewhere"}
    ]
    assert (base / "state.json").read_text(encoding="utf-8") == "null"


@pytest.mark.asyncio
async def test_shadow_branch_metadata_survives_engine_restart(tmp_path):
    base = tmp_path / "base"
    state = tmp_path / "runtime"
    base.mkdir()
    (base / "state.json").write_text("true", encoding="utf-8")
    workspace = WorkspaceSpec(id="base", root=str(base))
    roots = state / "shadows"

    engine = ShadowEngine(
        dispatcher=object(), roots_parent=str(roots), state_root=str(state)
    )
    branch = await engine.open_branch(
        task_id="task-restart",
        base_workspace=workspace,
        proposal=[{"capability_id": "fs", "arguments": {"operation": "read"}}],
        profile="supervised",
    )

    restored = ShadowEngine(
        dispatcher=object(), roots_parent=str(roots), state_root=str(state)
    )
    loaded = restored.get_branch(branch.id)
    assert loaded is not None
    assert loaded.status == BranchStatus.PROPOSED
    assert loaded.task_id == "task-restart"
    assert loaded.base_manifest == branch.base_manifest
    assert loaded.shadow_workspace.root == branch.shadow_workspace.root


@pytest.mark.asyncio
async def test_missing_shadow_workspace_requires_recovery(tmp_path):
    base = tmp_path / "base"
    state = tmp_path / "runtime"
    base.mkdir()
    workspace = WorkspaceSpec(id="base", root=str(base))
    roots = state / "shadows"

    engine = ShadowEngine(
        dispatcher=object(), roots_parent=str(roots), state_root=str(state)
    )
    branch = await engine.open_branch(
        task_id="task-recovery",
        base_workspace=workspace,
        proposal=[{"capability_id": "fs", "arguments": {"operation": "write"}}],
    )
    import shutil

    shutil.rmtree(branch.shadow_workspace.root)

    restored = ShadowEngine(
        dispatcher=object(), roots_parent=str(roots), state_root=str(state)
    )
    loaded = restored.get_branch(branch.id)
    assert loaded is not None
    assert loaded.status == BranchStatus.RECOVERY_REQUIRED
    assert "missing after restart" in (loaded.error or "")


@pytest.mark.asyncio
async def test_interrupted_commit_reconciles_without_guessing_outcome(tmp_path):
    base = tmp_path / "base"
    state = tmp_path / "runtime"
    base.mkdir()
    (base / "state.json").write_text("true", encoding="utf-8")
    workspace = WorkspaceSpec(id="base", root=str(base))

    engine = ShadowEngine(
        dispatcher=object(), roots_parent=str(state / "shadows"),
        state_root=str(state),
    )
    branch = await engine.open_branch(
        task_id="task-commit-recovery",
        base_workspace=workspace,
        proposal=[{"capability_id": "fs", "arguments": {"operation": "write"}}],
    )
    branch.status = BranchStatus.COMMITTING
    branch.commit_state = "APPLYING"
    branch.commit_plan = [{
        "call_id": "commit-1", "capability_id": "fs",
        "operation": "write", "path": "state.json",
        "content_sha256": "planned-hash",
    }]
    branch.checkpoint_id = "ckpt-before-commit"
    engine._persist_branches()

    restored = ShadowEngine(
        dispatcher=object(), roots_parent=str(state / "shadows"),
        state_root=str(state),
    )
    loaded = restored.get_branch(branch.id)
    assert loaded is not None
    assert loaded.status == BranchStatus.COMMITTING
    assert loaded.commit_plan[0]["path"] == "state.json"
    assert loaded.checkpoint_id == "ckpt-before-commit"

    assert await restored.reconcile_startup() == 1
    assert loaded.status == BranchStatus.RECOVERY_REQUIRED
    assert loaded.commit_state == "RECOVERY_REQUIRED"
    assert "reconcile the durable mutation ledger" in (loaded.error or "")
