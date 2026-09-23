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
import socket
import subprocess
import sys
import time
import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not expose POSIX rlimits
    resource = None

try:
    import psutil  # type: ignore
except Exception:  # rationale: psutil is optional; portable fallback remains available
    psutil = None


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


def _create_process_cgroup() -> str | None:
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


def _join_process_cgroup(path: str) -> None:
    try:
        with open(Path(path) / "cgroup.procs", "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        # The parent verifies membership after spawn and falls back to process
        # group ownership when the child cannot join the optional cgroup.
        pass


def _cgroup_pids(path: str | None) -> set[int]:
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


def _remove_process_cgroup(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).rmdir()
    except OSError:
        # A non-empty cgroup is retained as an operator-visible cleanup
        # obligation; an already-removed cgroup needs no further action.
        pass


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
        if normalized_network_policy and normalized_network_policy != "allow" and _network_syscalls_denied():
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
        my_env["PATH"] = _namespace_path(my_env.get("PATH", ""), root_abs)
        if "PYTHONPATH" in my_env:
            my_env["PYTHONPATH"] = _namespace_path(my_env["PYTHONPATH"], root_abs)
        # The process now starts in the namespace's path.  Passing the host
        # cwd to Popen would be both redundant and misleading.
        cwd = None
    existing_preexec = popen_kwargs.pop("preexec_fn", None)
    cgroup_path = _create_process_cgroup()
    if resource_limits is not None or existing_preexec is not None or cgroup_path is not None:
        def _preexec() -> None:
            if resource_limits is not None:
                _apply_resource_limits(resource_limits)
            if existing_preexec is not None:
                existing_preexec()
            if cgroup_path is not None:
                _join_process_cgroup(cgroup_path)

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
        _remove_process_cgroup(cgroup_path)
        raise
    if cgroup_path is not None and process.pid not in _cgroup_pids(cgroup_path):
        _remove_process_cgroup(cgroup_path)
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


def sandbox_argv(
    argv: list[str],
    *,
    root: str,
    cwd: str | None = None,
    network_policy: str | None = None,
    writable: bool = True,
    writable_paths: tuple[str, ...] | None = None,
    read_only_paths: tuple[str, ...] = (),
    toolchain_paths: tuple[str, ...] = (),
    writable_toolchain_paths: tuple[str, ...] = (),
) -> list[str]:
    """Build a fail-closed Linux Bubblewrap command line.

    This is deliberately a small, auditable mount policy rather than a
    best-effort ``cwd`` convention.  The workspace is writable; common
    interpreter/toolchain directories are read-only; temporary storage is a
    private tmpfs; and non-ALLOW network policy requests use a private network
    namespace.  If Bubblewrap is unavailable, the caller gets an error instead
    of silently falling back to host execution.
    """
    if os.name == "nt":
        raise RuntimeError("workspace sandbox is unavailable on Windows")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("workspace sandbox requires bubblewrap")
    root = os.path.realpath(os.path.abspath(root))
    if not os.path.isdir(root):
        raise RuntimeError(f"workspace sandbox root is not a directory: {root}")
    namespace_root = "/workspace"
    if cwd is None:
        namespace_cwd = namespace_root
    else:
        host_cwd = os.path.realpath(os.path.abspath(cwd))
        if host_cwd != root and not host_cwd.startswith(root + os.sep):
            raise RuntimeError("sandbox cwd is outside workspace root")
        namespace_cwd = namespace_root + host_cwd[len(root) :]

    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--bind" if writable and writable_paths is None else "--ro-bind",
        root,
        namespace_root,
        "--tmpfs",
        "/tmp",
    ]
    # Network namespaces require NETLINK_ROUTE during Bubblewrap setup.  On
    # locked-down hosts that already reject socket creation, retain that
    # inherited deny boundary instead of making bwrap setup fail; a socket-
    # capable host still receives an isolated namespace.
    if network_policy and network_policy != "allow" and not _network_syscalls_denied():
        command.append("--unshare-net")

    # Bind host toolchains read-only.  Never bind the host root, /home, or /etc
    # wholesale: that would turn a mount namespace into cosmetic cwd fencing.
    for path in ("/usr", "/bin", "/lib", "/lib64"):
        if os.path.exists(path):
            command.extend(("--ro-bind", path, path))
    # Resolve PATH-based launchers before entering the namespace.  Calling
    # ``sandbox_argv(["bash", ...])`` must not resolve relative to the
    # caller's cwd (which would produce ``<project>/bash``); the executable
    # is already mounted from its canonical system/toolchain path below.
    executable = ""
    if argv:
        resolved = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
        executable = os.path.realpath(resolved or argv[0])
    # The shell dependency route may invoke the exact Athena interpreter from
    # its source command. Mount it alongside the process entrypoint so that
    # dependency installation and generated Python use the same interpreter
    # identity inside the namespace.
    for tool in dict.fromkeys(
        item for item in (executable, os.path.realpath(sys.executable)) if item
    ):
        if tool == root or tool.startswith(root + os.sep):
            continue
        parent = os.path.dirname(tool)
        if os.path.basename(parent) == "bin":
            parent = os.path.dirname(parent)
        if parent and os.path.exists(parent) and parent not in ("/usr", "/bin", "/lib", "/lib64"):
            ancestor = parent
            ancestors: list[str] = []
            while ancestor not in ("", os.path.dirname(ancestor)):
                ancestors.append(ancestor)
                ancestor = os.path.dirname(ancestor)
            for directory in reversed(ancestors):
                if directory not in ("/", "/usr", "/bin", "/lib", "/lib64"):
                    command.extend(("--dir", directory))
            command.extend(("--ro-bind", parent, parent))

    for raw_path in toolchain_paths:
        path = os.path.realpath(os.path.abspath(raw_path))
        if not os.path.exists(path):
            raise RuntimeError(f"trusted toolchain path does not exist: {raw_path}")
        bind_path = path
        if os.path.isfile(path):
            parent = os.path.dirname(path)
        else:
            parent = path
        toolchain_ancestors: list[str] = []
        ancestor = parent
        while ancestor not in ("", os.path.dirname(ancestor)):
            toolchain_ancestors.append(ancestor)
            ancestor = os.path.dirname(ancestor)
        for directory in reversed(toolchain_ancestors):
            if directory not in ("/", "/usr", "/bin", "/lib", "/lib64"):
                command.extend(("--dir", directory))
        command.extend(("--ro-bind", bind_path, bind_path))

    for raw_path in writable_toolchain_paths:
        path = os.path.realpath(os.path.abspath(raw_path))
        if not os.path.exists(path):
            raise RuntimeError(f"trusted writable toolchain path does not exist: {raw_path}")
        parent = path if os.path.isdir(path) else os.path.dirname(path)
        writable_ancestors: list[str] = []
        ancestor = parent
        while ancestor not in ("", os.path.dirname(ancestor)):
            writable_ancestors.append(ancestor)
            ancestor = os.path.dirname(ancestor)
        for directory in reversed(writable_ancestors):
            if directory not in ("/", "/usr", "/bin", "/lib", "/lib64"):
                command.extend(("--dir", directory))
        command.extend(("--bind", path, path))

    # Mount the workspace read-only first whenever an explicit writable policy
    # is supplied, then overlay only the allowed canonical subtrees. Denied
    # existing subtrees are overlaid read-only last, preserving deny-overrides-
    # allow semantics inside arbitrary shell/Python execution.
    if writable_paths is not None:
        for path in writable_paths:
            _append_workspace_mount(command, path, root, namespace_root, "--bind")
        for path in read_only_paths:
            _append_workspace_mount(command, path, root, namespace_root, "--ro-bind")
    namespace_argv = []
    for index, arg in enumerate(argv):
        if index == 0 and executable and (arg == argv[0] or os.path.realpath(arg) == executable):
            # Virtualenv launchers are often symlinks into /usr.  The
            # namespace cannot resolve the host-side symlink path, so invoke
            # the already-mounted canonical executable.
            namespace_argv.append(executable)
        elif arg == root or arg.startswith(root + os.sep):
            namespace_argv.append(namespace_root + arg[len(root) :])
        else:
            namespace_argv.append(arg)
    command.extend(("--chdir", namespace_cwd, "--"))
    return command + namespace_argv


def _append_workspace_mount(
    command: list[str], path: str, root: str, namespace_root: str, option: str
) -> None:
    canonical = os.path.realpath(os.path.abspath(path))
    if canonical != root and not canonical.startswith(root + os.sep):
        return
    if not os.path.exists(canonical):
        return
    target = namespace_root + canonical[len(root) :]
    command.extend((option, canonical, target))


def _namespace_path(value: str, root: str) -> str:
    """Rewrite workspace-local PATH entries for the /workspace mount."""
    parts = []
    for item in value.split(os.pathsep):
        if item == root or item.startswith(root + os.sep):
            parts.append("/workspace" + item[len(root) :])
        else:
            parts.append(item)
    return os.pathsep.join(parts)


@lru_cache(maxsize=1)
def _network_syscalls_denied() -> bool:
    """Report whether this process already inherits a network deny filter.

    The result is intentionally conservative: only an explicit permission
    failure is treated as a usable inherited deny boundary.  Other failures
    leave Bubblewrap responsible for making the namespace private, and a
    failure there remains fail-closed.
    """
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        return True
    except OSError:
        return False
    finally:
        if probe is not None:
            probe.close()
    return False


def process_group_id(process: "subprocess.Popen") -> int | None:
    """Return the process-group id owning ``process``. On POSIX the spawned
    session leader has its own pgid because ``spawn_owned`` used
    ``start_new_session``."""
    if os.name == "nt":
        return None
    try:
        return os.getpgid(process.pid)
    except ProcessLookupError:
        return None


def process_start_identity(pid: int) -> str | None:
    """Return Linux's process-start token for ``pid``.

    A PID is recyclable. The token in ``/proc/<pid>/stat`` field 22 is stable
    for one process lifetime, so callers can bind a control request to
    ``(pid, start_identity)`` instead of trusting a bare PID. The parser
    splits after the command name because that field may contain whitespace
    or closing parentheses.
    """
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
    """Return descendant PIDs of ``root_pid`` (excluding root itself).

    Uses ``psutil`` when available; otherwise returns an empty list (POSIX
    group signalling still covers the tree for group-spawned children).
    """
    if psutil is None:
        return []
    try:
        return [p.pid for p in _descendants(psutil.Process(root_pid))]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []


def _processes_in_group(pgid: int | None) -> set[int]:
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


def _capture_owned_processes(
    root_pid: int,
    *,
    process_group: int | None = None,
    cgroup_path: str | None = None,
) -> dict[int, str | None]:
    """Capture root/descendant identities before the root can disappear."""
    pids = {
        root_pid,
        *child_pids(root_pid),
        *_processes_in_group(process_group),
        *_cgroup_pids(cgroup_path),
    }
    return {pid: process_start_identity(pid) for pid in pids if pid > 0}


def _process_alive(pid: int, start_identity: str | None) -> bool:
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


def _wait_for_owned_exit(
    captured: dict[int, str | None], *, timeout: float
) -> tuple[int, ...]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        survivors = tuple(
            pid for pid, identity in captured.items() if _process_alive(pid, identity)
        )
        if not survivors or time.monotonic() >= deadline:
            return survivors
        time.sleep(0.02)


def _descendants(proc) -> list:
    try:
        return proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


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
    captured = _capture_owned_processes(
        process.pid,
        process_group=pgid,
        cgroup_path=cgroup_path,
    )
    if process.poll() is not None:
        for pid, identity in captured.items():
            if pid == process.pid or not _process_alive(pid, identity):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        survivors = _wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
        if not survivors:
            _remove_process_cgroup(cgroup_path)
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
        survivors = _wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
        if not survivors:
            _remove_process_cgroup(cgroup_path)
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
    except Exception:
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
            if _process_alive(pid, captured[pid]):
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
            if _process_alive(pid, captured[pid]):
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass

    dead = process.poll() is not None
    survivors = _wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
    if not survivors:
        _remove_process_cgroup(cgroup_path)
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
            _capture_owned_processes(pid, process_group=pgid, cgroup_path=cgroup_path)
            if pid
            else {}
        )
        for child, identity in captured.items():
            if child == pid or not _process_alive(child, identity):
                continue
            try:
                os.kill(child, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        survivors = _wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
        if not survivors:
            _remove_process_cgroup(cgroup_path)
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
    captured = _capture_owned_processes(
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
        if child == pid or not _process_alive(child, identity):
            continue
        try:
            os.kill(child, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    survivors = _wait_for_owned_exit(captured, timeout=min(timeout, 1.0))
    if not survivors:
        _remove_process_cgroup(cgroup_path)
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
    except Exception:
        try:
            os.kill(process.pid, signal.SIGINT)
        except Exception:
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
