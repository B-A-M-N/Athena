"""Identity and ownership mechanics for manager-owned process trees.

This module only discovers and proves process ownership.  It does not spawn,
signal, or decide task lifecycle; those responsibilities remain in
``process_tree`` and ``ExecutionManager``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

try:
    import psutil  # type: ignore
except Exception:  # rationale: psutil is optional; portable fallback remains available
    psutil = None


def create_process_cgroup() -> str | None:
    """Create a private cgroup when the host grants a writable cgroup v2 root."""
    root = Path("/sys/fs/cgroup")
    if os.name == "nt" or not (root / "cgroup.controllers").is_file():
        return None
    if not os.access(root, os.W_OK | os.X_OK):
        return None
    path = root / f"athena-{os.getpid()}-{time.monotonic_ns()}"
    try:
        path.mkdir()
    except OSError:
        return None
    return str(path)


def join_process_cgroup(path: str) -> None:
    try:
        with open(Path(path) / "cgroup.procs", "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        # The parent verifies membership after spawn and falls back to process
        # group ownership when the child cannot join the optional cgroup.
        pass


def cgroup_pids(path: str | None) -> set[int]:
    if not path:
        return set()
    try:
        return {
            int(value)
            for value in (Path(path) / "cgroup.procs").read_text(encoding="ascii").split()
            if int(value) > 0
        }
    except (OSError, ValueError):
        return set()


def remove_process_cgroup(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).rmdir()
    except OSError:
        # A non-empty cgroup is retained as an operator-visible cleanup
        # obligation; an already-removed cgroup needs no further action.
        pass


def process_group_id(process: Any) -> int | None:
    """Return the process-group id owning ``process`` on POSIX."""
    if os.name == "nt":
        return None
    try:
        return os.getpgid(process.pid)
    except ProcessLookupError:
        return None


def process_start_identity(pid: int) -> str | None:
    """Return Linux's process-start token for a PID."""
    if os.name == "nt" or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            line = handle.read()
        _pid_and_comm, separator, remainder = line.rpartition(")")
        if not separator:
            return None
        fields = remainder.split()
        # ``remainder`` starts at stat field 3; field 22 is index 19.
        return fields[19] if len(fields) > 19 else None
    except (OSError, UnicodeError):
        return None


def child_pids(root_pid: int) -> list[int]:
    """Return descendant PIDs of ``root_pid`` (excluding the root)."""
    if psutil is None:
        return []
    try:
        return [process.pid for process in _descendants(psutil.Process(root_pid))]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []


def processes_in_group(pgid: int | None) -> set[int]:
    if os.name == "nt" or pgid is None:
        return set()
    result: set[int] = set()
    for raw_pid in os.listdir("/proc"):
        if not raw_pid.isdigit():
            continue
        pid = int(raw_pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
                _prefix, separator, remainder = handle.read().rpartition(")")
            if separator and int(remainder.split()[2]) == pgid:
                result.add(pid)
        except (OSError, ValueError, IndexError):
            continue
    return result


def capture_owned_processes(
    root_pid: int,
    *,
    process_group: int | None = None,
    cgroup_path: str | None = None,
) -> dict[int, str | None]:
    """Capture root/descendant identities before the root can disappear."""
    pids = {
        root_pid,
        *child_pids(root_pid),
        *processes_in_group(process_group),
        *cgroup_pids(cgroup_path),
    }
    return {pid: process_start_identity(pid) for pid in pids if pid > 0}


def process_alive(pid: int, start_identity: str | None) -> bool:
    if pid <= 0:
        return False
    if start_identity is not None and process_start_identity(pid) != start_identity:
        return False
    if psutil is not None:
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() not in {
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def wait_for_owned_exit(
    captured: dict[int, str | None], *, timeout: float
) -> tuple[int, ...]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        survivors = tuple(
            pid for pid, identity in captured.items() if process_alive(pid, identity)
        )
        if not survivors or time.monotonic() >= deadline:
            return survivors
        time.sleep(0.02)


def _descendants(proc: Any) -> list[Any]:
    try:
        return proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


__all__ = [
    "capture_owned_processes",
    "cgroup_pids",
    "child_pids",
    "create_process_cgroup",
    "join_process_cgroup",
    "process_alive",
    "process_group_id",
    "process_start_identity",
    "processes_in_group",
    "remove_process_cgroup",
    "wait_for_owned_exit",
]
