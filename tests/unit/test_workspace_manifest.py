from __future__ import annotations

import subprocess

from athena.workspace_manifest import copy_ignore


def _git_repo(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Athena Tests"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "athena@example.invalid"], cwd=root, check=True)


def test_copy_ignore_preserves_tracked_target_tree(tmp_path):
    _git_repo(tmp_path)
    tracked = tmp_path / "target" / "tracked.txt"
    tracked.parent.mkdir()
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "target/tracked.txt"], cwd=tmp_path, check=True)

    assert "target" not in copy_ignore(str(tmp_path), ["target"])


def test_copy_ignore_drops_untracked_target_tree(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "target").mkdir()

    assert "target" in copy_ignore(str(tmp_path), ["target"])
