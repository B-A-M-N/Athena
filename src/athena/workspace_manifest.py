"""Canonical project-tree policy for snapshots, fingerprints, and shadows."""

from __future__ import annotations

import os
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
    return [name for name in names if ignored_name(name, is_directory=False)]


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
