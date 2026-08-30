"""Canonical project-tree policy for snapshots, fingerprints, and shadows."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        ".venv312",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        "htmlcov",
        "target",
    }
)


def ignored_name(name: str, *, is_directory: bool) -> bool:
    """Return whether a tree entry is outside the project revision."""
    return name in IGNORED_DIRECTORY_NAMES or (
        not is_directory and (name.endswith(".pyc") or name == ".coverage")
    )


def copy_ignore(_directory: str, names: list[str]) -> list[str]:
    """Ignore callback suitable for :func:`shutil.copytree`."""
    directory = Path(_directory).resolve()
    git_root = _git_root(directory)
    tracked = _tracked_paths(git_root) if git_root is not None else frozenset()
    ignored: list[str] = []
    for name in names:
        path = directory / name
        is_directory = path.is_dir() and not path.is_symlink()
        if not ignored_name(name, is_directory=is_directory):
            continue
        if git_root is not None and _has_tracked_path(path, git_root, tracked):
            continue
        ignored.append(name)
    return ignored


def _git_root(directory: Path) -> Path | None:
    """Find the containing checkout without trusting Git's work-tree state."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


@lru_cache(maxsize=32)
def _tracked_paths(git_root: Path) -> frozenset[str]:
    """Return indexed paths once per checkout for copytree callbacks."""
    try:
        result = subprocess.run(  # architecture-lint: allow subprocess-outside-approved-backends reason=read-only tracked manifest query
            ["git", "-C", str(git_root), "ls-files", "--cached", "-z"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def _has_tracked_path(path: Path, git_root: Path, tracked: frozenset[str]) -> bool:
    """Keep an ignored entry when it is or contains an indexed path."""
    try:
        relative = path.resolve().relative_to(git_root.resolve()).as_posix()
    except ValueError:
        return False
    prefix = relative + "/"
    return any(item == relative or item.startswith(prefix) for item in tracked)


def tree_paths(root: Path) -> list[Path]:
    """List files/symlinks while pruning the canonical ignored directories."""
    result: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not ignored_name(name, is_directory=True))
        for name in sorted(filenames):
            if ignored_name(name, is_directory=False):
                continue
            path = Path(directory) / name
            result.append(path)
        # os.walk does not include directory symlinks in filenames; preserve
        # them as resources without traversing their target.
        for name in list(dirnames):
            path = Path(directory) / name
            if path.is_symlink():
                result.append(path)
        dirnames[:] = [name for name in dirnames if not (Path(directory) / name).is_symlink()]
    return sorted(result)
