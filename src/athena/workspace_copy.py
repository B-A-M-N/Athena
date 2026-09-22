"""Bounded, link-safe workspace copying and asynchronous staging helpers."""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
from functools import lru_cache
from pathlib import Path

from athena.concurrency import run_blocking

_FICLONE = 0x40049409


@lru_cache(maxsize=8)
def _reflink_supported(directory: str) -> bool:
    """Keep the portable copy path as the safe default."""
    del directory
    return False


def _copy_file(source: Path, destination: Path) -> None:
    """Copy one regular file without aliasing source bytes."""
    try:
        if _reflink_supported(str(source.parent)):
            _reflink_file(source, destination)
            return
    except (OSError, ValueError):
        pass
    shutil.copy2(source, destination, follow_symlinks=False)


def _reflink_file(source: Path, destination: Path) -> None:
    src_fd = os.open(source, os.O_RDONLY)
    try:
        st = os.fstat(src_fd)
        dst_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            fcntl.ioctl(dst_fd, _FICLONE, src_fd)
            os.chmod(destination, stat.S_IMODE(st.st_mode))
            os.utime(destination, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            try:
                os.unlink(destination)
            except OSError:
                pass
            raise
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def copy_workspace_tree(
    source: str | Path,
    destination: str | Path,
    *,
    ignore=None,
    dirs_exist_ok: bool = False,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> None:
    """Copy a workspace while rejecting links that escape its root."""
    src = Path(source).resolve(strict=True)
    dst = Path(destination)
    if not src.is_dir():
        raise NotADirectoryError(f"workspace root does not exist: {source}")
    _enforce_copy_budget(src, max_files=max_files, max_bytes=max_bytes)
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
                    link_target = os.path.relpath(destination_target, start=destination_path.parent)
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
                    _copy_file(source_path, destination_path)
                else:
                    raise ValueError(f"unsupported workspace entry: {source_path}")

    validate(src)
    copy_directory(src, dst)


async def copy_workspace_tree_async(
    source: str | Path,
    destination: str | Path,
    *,
    ignore=None,
    dirs_exist_ok: bool = False,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> None:
    """Run workspace copying outside the event loop."""
    await run_blocking(
        copy_workspace_tree,
        source,
        destination,
        ignore=ignore,
        dirs_exist_ok=dirs_exist_ok,
        max_files=max_files,
        max_bytes=max_bytes,
    )


async def rmtree_async(path: str | Path, *, ignore_errors: bool = False) -> None:
    """Run recursive workspace cleanup outside the event loop."""
    await run_blocking(shutil.rmtree, path, ignore_errors=ignore_errors)


def _enforce_copy_budget(
    source: Path, *, max_files: int | None, max_bytes: int | None
) -> None:
    """Reject oversized staging trees before creating destination files."""
    if max_files is None and max_bytes is None:
        return
    if max_files is not None and max_files < 1:
        raise ValueError("workspace copy max_files must be positive")
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("workspace copy max_bytes must be positive")
    files = 0
    total_bytes = 0
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = Path(directory) / name
            files += 1
            if max_files is not None and files > max_files:
                raise ValueError(f"workspace copy exceeds max_files={max_files}")
            try:
                total_bytes += path.stat().st_size if not path.is_symlink() else len(os.readlink(path))
            except OSError as exc:
                raise ValueError(f"workspace copy cannot inspect {path}") from exc
            if max_bytes is not None and total_bytes > max_bytes:
                raise ValueError(f"workspace copy exceeds max_bytes={max_bytes}")


__all__ = [
    "copy_workspace_tree",
    "copy_workspace_tree_async",
    "rmtree_async",
]
