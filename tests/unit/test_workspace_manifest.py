from __future__ import annotations

import subprocess
import pytest

from athena.workspace_manifest import copy_ignore, copy_workspace_tree, tree_paths


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


def test_tree_paths_preserves_tracked_coverage_file(tmp_path):
    _git_repo(tmp_path)
    coverage = tmp_path / ".coverage"
    coverage.write_text("tracked coverage\n")
    subprocess.run(["git", "add", ".coverage"], cwd=tmp_path, check=True)

    paths = {path.relative_to(tmp_path).as_posix() for path in tree_paths(tmp_path)}

    assert ".coverage" in paths


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


def _symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        pytest.skip("symlinks not supported")


@pytest.mark.parametrize("kind", ["file", "directory", "chain", "broken", "sibling-prefix"])
def test_copy_workspace_tree_rejects_unsafe_symlinks(tmp_path, kind):
    source = tmp_path / "workspace"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n")

    if kind == "file":
        target = outside / "secret.txt"
    elif kind == "directory":
        target = outside
    elif kind == "chain":
        first = source / "first"
        _symlink_or_skip(first, outside / "secret.txt")
        target = first
    elif kind == "broken":
        target = outside / "does-not-exist"
    else:
        sibling = tmp_path / "workspace-sibling"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("secret\n")
        target = sibling / "secret.txt"
    _symlink_or_skip(source / "link", target, target_is_directory=kind == "directory")

    with pytest.raises(ValueError, match="symlink"):
        copy_workspace_tree(source, tmp_path / f"clone-{kind}")


def test_copy_workspace_tree_rewrites_safe_internal_symlinks(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    target = source / "data.txt"
    target.write_text("inside\n")
    _symlink_or_skip(source / "link.txt", target)

    clone = tmp_path / "clone"
    copy_workspace_tree(source, clone)

    link = clone / "link.txt"
    assert link.is_symlink()
    assert link.resolve() == clone / "data.txt"
    assert link.read_text(encoding="utf-8") == "inside\n"
