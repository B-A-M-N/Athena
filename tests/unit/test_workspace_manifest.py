from __future__ import annotations

import subprocess

from athena.workspace_manifest import copy_ignore, tree_paths


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


def test_tree_paths_preserves_tracked_ignored_name_trees(tmp_path):
    _git_repo(tmp_path)
    tracked_target = tmp_path / "target" / "important.txt"
    tracked_htmlcov = tmp_path / "htmlcov" / "report.txt"
    tracked_target.parent.mkdir()
    tracked_htmlcov.parent.mkdir()
    tracked_target.write_text("target\n")
    tracked_htmlcov.write_text("coverage\n")
    subprocess.run(
        ["git", "add", "target/important.txt", "htmlcov/report.txt"],
        cwd=tmp_path,
        check=True,
    )

    paths = {path.relative_to(tmp_path).as_posix() for path in tree_paths(tmp_path)}

    assert {"target/important.txt", "htmlcov/report.txt"} <= paths


def test_tree_paths_prunes_untracked_ignored_name_trees(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "generated.txt").write_text("generated\n")
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "htmlcov" / "index.html").write_text("generated\n")
    (tmp_path / ".coverage").write_text("generated\n")

    paths = {path.relative_to(tmp_path).as_posix() for path in tree_paths(tmp_path)}

    assert not paths.intersection({"target/generated.txt", "htmlcov/index.html", ".coverage"})


def test_tracked_manifest_cache_refreshes_after_git_add(tmp_path):
    _git_repo(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    first = target / "first.txt"
    first.write_text("first\n")
    assert "target/first.txt" not in {
        path.relative_to(tmp_path).as_posix() for path in tree_paths(tmp_path)
    }

    second = target / "second.txt"
    second.write_text("second\n")
    subprocess.run(["git", "add", "target/second.txt"], cwd=tmp_path, check=True)

    paths = {path.relative_to(tmp_path).as_posix() for path in tree_paths(tmp_path)}
    assert "target/second.txt" in paths
