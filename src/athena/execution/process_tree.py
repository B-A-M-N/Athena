"""Process-tree helpers: spawn with an own process group, enumerate children,
and terminate the whole owned tree (SIGTERM then SIGKILL).

Used by runtimes and by ExecutionManager to satisfy BHV-061 (process ownership
per task) and BUILDSPEC 49-50 / BHV-062 (process-tree cancellation; orphaned
processes after cancellation are a release-blocking defect).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import asyncio
from dataclasses import dataclass, field
from typing import Any

from athena.execution.process_ownership import (
    capture_owned_processes,
    cgroup_pids,
    child_pids,
    create_process_cgroup,
    join_process_cgroup,
    process_alive,
    process_group_id,
    process_start_identity,
    remove_process_cgroup,
    wait_for_owned_exit,
)
from athena.execution.sandbox import namespace_path, network_syscalls_denied, sandbox_argv

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not expose POSIX rlimits
    resource = None

@dataclass(frozen=True)
class ProcessKillOutcome:
    """Structured result of a process-tree kill (P1-11).

    Cleanup may stay best-effort, but shutdown must KNOW when a tree could
    not be proven dead — silence is how orphaned processes survive a
    release gate. ``proven_dead`` is True only when the root was observed
    exited (or was already dead) and every descendant captured before
    termination was also proven dead; ``survivors`` lists owned pids that
    remained alive after the escalation ladder.
    """

    proven_dead: bool
    survivors: tuple[int, ...] = field(default_factory=tuple)
    already_dead: bool = False
    cleanup_obligation: str | None = None


_MINIMAL_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "TERM",
)
_WINDOWS_ENV_KEYS = ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "USERPROFILE", "SystemDrive")


def spawn_owned(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    sandbox_root: str | None = None,
    network_policy: str | None = None,
    sandbox_writable: bool = True,
    writable_paths: tuple[str, ...] | None = None,
    read_only_paths: tuple[str, ...] = (),
    toolchain_paths: tuple[str, ...] = (),
    writable_toolchain_paths: tuple[str, ...] = (),
    resource_limits: Any = None,
    **popen_kwargs: object,
) -> "subprocess.Popen[str]":
    """Spawn ``argv`` in its own process group so the whole tree can be killed.

    Always passes ``start_new_session=True`` on POSIX so descendants share the
    new session/pgid. ``popen_kwargs`` may override pipe setup, text mode, etc.

    Environment: build a *minimal* sanitized environment (host secrets are never
    propagated into untrusted runtimes) and merge ONLY the explicitly-supplied
    ``env`` entries. ``PATH`` is always kept so runtimes can find python/bash
    (BUILDSPEC 43 secret hygiene).
    """
    my_env: dict[str, str] = {}
    for key in _MINIMAL_ENV_KEYS:
        if key in os.environ:
            my_env[key] = os.environ[key]
    if os.name == "nt":
        for key in _WINDOWS_ENV_KEYS:
            if key in os.environ:
                my_env[key] = os.environ[key]
    elif "PATH" not in my_env:
        my_env["PATH"] = os.environ.get("PATH", "")
    if env:
        my_env.update({k: str(v) for k, v in env.items()})
    my_env.setdefault("PYTHONIOENCODING", "utf-8")
    if toolchain_paths:
        toolchain_bins = []
        for raw_path in toolchain_paths:
            path = os.path.realpath(os.path.abspath(raw_path))
            parent = path if os.path.isdir(path) else os.path.dirname(path)
            if os.path.basename(parent) == "bin":
                toolchain_bins.append(parent)
        if toolchain_bins:
            my_env["PATH"] = ":".join(dict.fromkeys([*toolchain_bins, my_env.get("PATH", "")]))
    normalized_network_policy = getattr(network_policy, "value", network_policy)
    if normalized_network_policy not in (None, "allow") and sandbox_root is None:
        raise RuntimeError(
            "network policy requires a workspace sandbox; refusing unrestricted execution"
        )
    if sandbox_root is not None:
        # Some locked-down hosts already apply an inherited seccomp filter
        # which rejects AF_INET/AF_INET6 sockets.  Bubblewrap cannot create a
        # network namespace there because its setup needs a NETLINK_ROUTE
        # socket, even though the child is already unable to use the network.
        # Preserve the fail-closed guarantee by retaining that inherited
        # filter and only skipping the redundant namespace setup.  If the
        # host can create network sockets, a denied policy still requires the
        # normal isolated namespace below.
        effective_network_policy = normalized_network_policy
        if normalized_network_policy and normalized_network_policy != "allow" and network_syscalls_denied():
            effective_network_policy = "allow"
        argv = sandbox_argv(
            argv,
            root=sandbox_root,
            cwd=cwd,
            network_policy=effective_network_policy,
            writable=sandbox_writable,
            writable_paths=writable_paths,
            read_only_paths=read_only_paths,
            toolchain_paths=toolchain_paths,
            writable_toolchain_paths=writable_toolchain_paths,
        )
        root_abs = os.path.realpath(os.path.abspath(sandbox_root))
        my_env["PATH"] = namespace_path(my_env.get("PATH", ""), root_abs)
        if "PYTHONPATH" in my_env:
            my_env["PYTHONPATH"] = namespace_path(my_env["PYTHONPATH"], root_abs)
        # The process now starts in the namespace's path.  Passing the host
        # cwd to Popen would be both redundant and misleading.
        cwd = None
    existing_preexec = popen_kwargs.pop("preexec_fn", None)
    cgroup_path = create_process_cgroup()
    if resource_limits is not None or existing_preexec is not None or cgroup_path is not None:
        def _preexec() -> None:
            if resource_limits is not None:
                _apply_resource_limits(resource_limits)
            if existing_preexec is not None:
                existing_preexec()
            if cgroup_path is not None:
                join_process_cgroup(cgroup_path)

        popen_kwargs["preexec_fn"] = _preexec
    kwargs: dict = {"env": my_env}
    if cwd is not None:
        kwargs["cwd"] = cwd
    if os.name != "nt":
        kwargs["start_new_session"] = True
    kwargs.update(popen_kwargs)
    try:
        process = subprocess.Popen(argv, **kwargs)
    except (OSError, ValueError, TypeError):
        remove_process_cgroup(cgroup_path)
        raise
    if cgroup_path is not None and process.pid not in cgroup_pids(cgroup_path):
        remove_process_cgroup(cgroup_path)
        cgroup_path = None
    if cgroup_path is not None:
        setattr(process, "_athena_cgroup_path", cgroup_path)
    if os.name != "nt":
        try:
            setattr(process, "_athena_process_group_id", os.getpgid(process.pid))
        except ProcessLookupError:
            pass
    return process


def _apply_resource_limits(limits: Any) -> None:
    """Apply hard local-process limits before the runtime image starts."""
    if resource is None or os.name == "nt":
        raise RuntimeError("local resource limits are unavailable on this platform")
    values = {
        "max_memory_mb": getattr(limits, "max_memory_mb", None),
        "max_cpu_seconds": getattr(limits, "max_cpu_seconds", None),
        "max_processes": getattr(limits, "max_processes", None),
    }
    for name, value in values.items():
        if value is not None and (isinstance(value, bool) or int(value) < 1):
            raise ValueError(f"{name} must be a positive integer")
    resource_limits = (
        ("max_memory_mb", resource.RLIMIT_AS, int(values["max_memory_mb"]) * 1024 * 1024)
        if values["max_memory_mb"] is not None
        else None,
        ("max_cpu_seconds", resource.RLIMIT_CPU, int(values["max_cpu_seconds"]))
        if values["max_cpu_seconds"] is not None
        else None,
        ("max_processes", resource.RLIMIT_NPROC, int(values["max_processes"]))
        if values["max_processes"] is not None and hasattr(resource, "RLIMIT_NPROC")
        else None,
    )
    for name, kind, value in (item for item in resource_limits if item is not None):
        _soft, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY and value > hard:
            raise ValueError(f"{name} exceeds the host hard limit")
        resource.setrlimit(kind, (value, value))


def _reap(process: "subprocess.Popen", timeout: float) -> None:
    """Wait for ``process`` to fully exit and reap it (avoids zombies)."""
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:  # rationale: cleanup escalation must continue
            pass
        try:
            process.wait(timeout=timeout)
        except Exception:  # rationale: cleanup escalation must continue
            pass
    except Exception:  # rationale: failed wait is reported by the caller's outcome
        pass


def kill_tree(process: "subprocess.Popen", *, timeout: float = 3.0) -> ProcessKillOutcome:
    """SIGTERM then SIGKILL the whole process tree owned by ``process``.

    Signals the owning process group first so descendants are covered even
    without ``psutil``; then, within ``timeout``, escalates to SIGKILL on the
    group and any psutil-enumerated descendants. Finally reaps the root and any
    confirmed-dead children so no zombies are left behind (BHV-061/062).

    Returns a structured outcome (P1-11): ``proven_dead`` is False when the
    root survived the escalation ladder, so shutdown can report it instead
    of the failure hiding in a log line.
    """
    cgroup_path = getattr(process, "_athena_cgroup_path", None)
    pgid = process_group_id(process) or getattr(process, "_athena_process_group_id", None)
    captured = capture_owned_processes(
        process.pid,
        process_group=pgid,
        cgroup_path=cgroup_path,
    )
    if process.poll() is not None:
        for pid, identity in captured.items():
            if pid == process.pid or not process_alive(pid, identity):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        survivors = wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
        if not survivors:
            remove_process_cgroup(cgroup_path)
        ownership_proven = cgroup_path is not None and not survivors
        return ProcessKillOutcome(
            proven_dead=ownership_proven,
            survivors=survivors,
            already_dead=True,
            cleanup_obligation=(
                f"owned process descendants remain after root exit: {survivors}"
                if survivors
                else "root exited before the owned process tree could be proven"
            ),
        )
    if os.name == "nt":
        try:
            process.kill()
        except Exception:  # rationale: fall back to direct process termination
            pass
        _reap(process, 5.0)
        dead = process.poll() is not None
        survivors = wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
        if not survivors:
            remove_process_cgroup(cgroup_path)
        return ProcessKillOutcome(
            proven_dead=dead and not survivors,
            survivors=survivors,
            cleanup_obligation=(
                f"owned process descendants remain after termination: {survivors}"
                if survivors
                else None
            ),
        )

    assert pgid is not None  # POSIX + live process (checked above) implies a pgid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:  # rationale: process-group escalation falls back to the owned root
        try:
            process.terminate()
        except Exception:  # rationale: process-group escalation is best effort
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.05)

    if process.poll() is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:  # rationale: descendant cleanup is best effort
            pass
    # A detached descendant is no longer in the root's process group and the
    # root may already have exited by the time the escalation branch runs.
    # Signal every identity captured before termination independently.
    for pid in captured:
        if pid == process.pid:
            continue
        try:
            if process_alive(pid, captured[pid]):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:  # rationale: platform interrupt fallback is best effort
            pass

    _reap(process, timeout=5.0)
    for pid in captured:
        if pid == process.pid:
            continue
        try:
            if process_alive(pid, captured[pid]):
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass

    dead = process.poll() is not None
    survivors = wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
    if not survivors:
        remove_process_cgroup(cgroup_path)
    return ProcessKillOutcome(
        proven_dead=dead and not survivors,
        survivors=survivors,
        cleanup_obligation=(
            f"owned process descendants remain after termination: {survivors}"
            if survivors
            else None
        ),
    )


async def kill_tree_async(process: Any, *, timeout: float = 3.0) -> ProcessKillOutcome:
    """Terminate an asyncio subprocess and its owned process group.

    This is the async counterpart of :func:`kill_tree`; callers must use it
    for ``asyncio.subprocess.Process`` instances so cancellation cannot leave
    validation/compiler grandchildren alive. Returns the same structured
    outcome (P1-11): ``proven_dead`` only when the root was observed exited.
    """
    if process is None or process.returncode is not None:
        pid = int(getattr(process, "pid", 0) or 0)
        cgroup_path = getattr(process, "_athena_cgroup_path", None)
        pgid = getattr(process, "_athena_process_group_id", None)
        captured = (
            capture_owned_processes(pid, process_group=pgid, cgroup_path=cgroup_path)
            if pid
            else {}
        )
        for child, identity in captured.items():
            if child == pid or not process_alive(child, identity):
                continue
            try:
                os.kill(child, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        survivors = wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
        if not survivors:
            remove_process_cgroup(cgroup_path)
        ownership_proven = cgroup_path is not None and not survivors
        return ProcessKillOutcome(
            proven_dead=ownership_proven,
            survivors=survivors,
            already_dead=True,
            cleanup_obligation=(
                f"owned process descendants remain after root exit: {survivors}"
                if survivors
                else "root exited before the owned process tree could be proven"
            ),
        )
    pid = int(process.pid)
    cgroup_path = getattr(process, "_athena_cgroup_path", None)
    captured = capture_owned_processes(
        pid,
        process_group=getattr(process, "_athena_process_group_id", None),
        cgroup_path=cgroup_path,
    )
    if os.name == "nt":
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await _wait_async_process(process, timeout=5.0)
        return ProcessKillOutcome(
            proven_dead=process.returncode is not None,
            cleanup_obligation=(
                "Windows process-tree descendants could not be enumerated"
                if process.returncode is not None
                else "Windows process root did not terminate"
            ),
        )

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = None
    # Safety: if the child shares OUR process group (spawned without
    # start_new_session=True), killpg would signal Athena itself. Fall back
    # to the single-process handle, which is always safe.
    try:
        own_pgid = os.getpgid(0)
    except ProcessLookupError:
        own_pgid = None
    if pgid is not None and pgid != own_pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        pgid = None
        try:
            process.terminate()
        except ProcessLookupError:
            pass

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await _wait_async_process(process, timeout=5.0)
    for child, identity in captured.items():
        if child == pid or not process_alive(child, identity):
            continue
        try:
            os.kill(child, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    survivors = wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
    if not survivors:
        remove_process_cgroup(cgroup_path)
    return ProcessKillOutcome(
        proven_dead=process.returncode is not None and not survivors,
        survivors=survivors,
        cleanup_obligation=(
            f"owned process descendants remain after termination: {survivors}"
            if survivors
            else None
        ),
    )


async def _wait_async_process(process: Any, *, timeout: float) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


def interrupt_group(process: "subprocess.Popen") -> None:
    """Forward SIGINT to the owned process group (for interrupt, not kill)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.kill()
        except Exception:  # rationale: failed interrupt is observable by the owner
            pass
        _reap(process, 5.0)
        return
    pgid = process_group_id(process) or process.pid
    try:
        os.killpg(pgid, signal.SIGINT)
    except Exception:  # rationale: interrupt fallback is reported by the owning runtime
        try:
            os.kill(process.pid, signal.SIGINT)
        except Exception:  # rationale: a failed direct signal is already observable to the owner
            pass


__all__ = [
    "child_pids",
    "kill_tree",
    "kill_tree_async",
    "interrupt_group",
    "process_group_id",
    "process_start_identity",
    "spawn_owned",
]
