"""Regression tests for content-based shadow diffs and commit conflicts."""

from __future__ import annotations

from pathlib import Path

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
