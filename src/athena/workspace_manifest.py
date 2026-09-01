"""Canonical project-tree policy for snapshots, fingerprints, and shadows."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ManifestPolicy:
    """One ignore policy shared by copies, manifests, and fingerprints."""

    git_root: Path | None
    tracked_paths: frozenset[str]

    @classmethod
    def for_root(cls, root: Path) -> "ManifestPolicy":
        git_root = _git_root(root)
        if git_root is None:
            return cls(None, frozenset())
        return cls(git_root, _tracked_paths_for_current_index(git_root))

    def ignored(self, path: Path, *, is_directory: bool) -> bool:
        if not ignored_name(path.name, is_directory=is_directory):
            return False
        if self.git_root is not None and _has_tracked_path(path, self.git_root, self.tracked_paths):
            return False
        return True


def copy_ignore(_directory: str, names: list[str]) -> list[str]:
    """Ignore callback suitable for :func:`shutil.copytree`."""
    directory = Path(_directory).resolve()
    policy = ManifestPolicy.for_root(directory)
    ignored: list[str] = []
    for name in names:
        path = directory / name
        is_directory = path.is_dir() and not path.is_symlink()
        if policy.ignored(path, is_directory=is_directory):
            ignored.append(name)
    return ignored


def _git_root(directory: Path) -> Path | None:
    """Find the containing checkout without trusting Git's work-tree state."""
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _tracked_paths_for_current_index(git_root: Path) -> frozenset[str]:
    index = _git_index_path(git_root)
    try:
        stat = index.stat()
        identity = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        identity = (0, 0)
    return _tracked_paths(git_root, identity)


def _git_index_path(git_root: Path) -> Path:
    """Resolve the index for normal checkouts and linked worktrees."""
    git_entry = git_root / ".git"
    if git_entry.is_dir():
        return git_entry / "index"
    try:
        marker = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        return git_entry / "index"
    if marker.startswith("gitdir:"):
        target = Path(marker.split(":", 1)[1].strip())
        if not target.is_absolute():
            target = git_entry.parent / target
        return target.resolve() / "index"
    return git_entry / "index"


@lru_cache(maxsize=64)
def _tracked_paths(git_root: Path, index_identity: tuple[int, int]) -> frozenset[str]:
    """Return indexed paths once per checkout for copytree callbacks."""
    del index_identity  # cache key invalidates when the Git index changes
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
    """List files/symlinks while preserving tracked ignored-name paths."""
    root = root.resolve()
    policy = ManifestPolicy.for_root(root)
    result: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not policy.ignored(directory_path / name, is_directory=True)
        )
        for name in sorted(filenames):
            path = directory_path / name
            if policy.ignored(path, is_directory=False):
                continue
            result.append(path)
        # os.walk does not include directory symlinks in filenames; preserve
        # them as resources without traversing their target.
        for name in list(dirnames):
            path = directory_path / name
            if path.is_symlink():
                result.append(path)
        dirnames[:] = [name for name in dirnames if not (Path(directory) / name).is_symlink()]
    return sorted(result)


def copy_workspace_tree(
    source: str | Path,
    destination: str | Path,
    *,
    ignore=None,
    dirs_exist_ok: bool = False,
) -> None:
    """Copy a workspace without dereferencing links outside that workspace.

    ``shutil.copytree(..., symlinks=False)`` follows links and can copy an
    arbitrary external file or directory into a supposedly isolated shadow.
    This helper preflights every visible link with ``lstat``/``readlink``,
    rejects broken or external targets, and recreates safe internal links as
    relative links in the clone.  The target is resolved only for validation;
    its bytes are never read through the source link.
    """
    src = Path(source).resolve(strict=True)
    dst = Path(destination)
    if not src.is_dir():
        raise NotADirectoryError(f"workspace root does not exist: {source}")
    ignored_cache: dict[Path, set[str]] = {}

    def ignored_names(directory: Path, names: list[str]) -> set[str]:
        if ignore is None:
            return set()
        cached = ignored_cache.get(directory)
        if cached is None:
            cached = set(str(name) for name in ignore(str(directory), sorted(names)))
            ignored_cache[directory] = cached
        return cached

    def inside(path: Path) -> bool:
        try:
            path.relative_to(src)
        except ValueError:
            return False
        return True

    def omitted(path: Path) -> bool:
        """Return whether a resolved target would be absent from the clone."""
        try:
            parts = path.relative_to(src).parts
        except ValueError:
            return True
        directory = src
        for part in parts:
            if part in ignored_names(directory, [part]):
                return True
            directory = directory / part
        return False

    def validate(directory: Path) -> None:
        with os.scandir(directory) as scanner:
            entries = sorted(list(scanner), key=lambda entry: entry.name)
            ignored = ignored_names(directory, [entry.name for entry in entries])
            for entry in entries:
                if entry.name in ignored:
                    continue
                path = Path(entry.path)
                if entry.is_symlink():
                    try:
                        target = path.resolve(strict=True)
                    except (OSError, RuntimeError) as exc:
                        raise ValueError(f"broken workspace symlink: {path}") from exc
                    if not inside(target):
                        raise ValueError(f"workspace symlink escapes source: {path}")
                    if omitted(target):
                        raise ValueError(f"workspace symlink targets an omitted path: {path}")
                    if not (target.is_file() or target.is_dir()):
                        raise ValueError(f"unsupported workspace symlink target: {path}")
                elif entry.is_dir(follow_symlinks=False):
                    validate(path)

    def remove_existing(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        elif path.is_dir():
            if not dirs_exist_ok:
                raise FileExistsError(path)
        elif path.exists():
            raise FileExistsError(path)

    def copy_directory(directory: Path, target_directory: Path) -> None:
        if target_directory.exists() and not target_directory.is_dir():
            if not dirs_exist_ok:
                raise FileExistsError(target_directory)
            target_directory.unlink()
        target_directory.mkdir(parents=True, exist_ok=dirs_exist_ok)
        with os.scandir(directory) as scanner:
            entries = sorted(list(scanner), key=lambda entry: entry.name)
            ignored = ignored_names(directory, [entry.name for entry in entries])
            for entry in entries:
                if entry.name in ignored:
                    continue
                source_path = Path(entry.path)
                destination_path = target_directory / entry.name
                if entry.is_symlink():
                    try:
                        resolved = source_path.resolve(strict=True)
                    except (OSError, RuntimeError) as exc:
                        raise ValueError(f"broken workspace symlink: {source_path}") from exc
                    target_relative = resolved.relative_to(src)
                    destination_target = dst / target_relative
                    link_target = os.path.relpath(
                        destination_target,
                        start=destination_path.parent,
                    )
                    if destination_path.exists() or destination_path.is_symlink():
                        if not dirs_exist_ok:
                            raise FileExistsError(destination_path)
                        remove_existing(destination_path)
                    destination_path.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(link_target, destination_path)
                elif entry.is_dir(follow_symlinks=False):
                    copy_directory(source_path, destination_path)
                elif entry.is_file(follow_symlinks=False):
                    if destination_path.exists() or destination_path.is_symlink():
                        if not dirs_exist_ok:
                            raise FileExistsError(destination_path)
                        remove_existing(destination_path)
                    destination_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, destination_path, follow_symlinks=False)
                else:
                    raise ValueError(f"unsupported workspace entry: {source_path}")

    validate(src)
    copy_directory(src, dst)


__all__ = [
    "IGNORED_DIRECTORY_NAMES",
    "ManifestPolicy",
    "copy_ignore",
    "copy_workspace_tree",
    "ignored_name",
    "tree_paths",
]
