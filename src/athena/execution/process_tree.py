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

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil is optional
    psutil = None


_MINIMAL_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME",
    "TZ", "TERM",
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
    if sandbox_root is not None:
        argv = sandbox_argv(
            argv,
            root=sandbox_root,
            cwd=cwd,
            network_policy=network_policy,
            writable=sandbox_writable,
        )
        root_abs = os.path.realpath(os.path.abspath(sandbox_root))
        my_env["PATH"] = _namespace_path(my_env.get("PATH", ""), root_abs)
        # The process now starts in the namespace's path.  Passing the host
        # cwd to Popen would be both redundant and misleading.
        cwd = None
    kwargs: dict = {"env": my_env}
    if cwd is not None:
        kwargs["cwd"] = cwd
    if os.name != "nt":
        kwargs["start_new_session"] = True
    kwargs.update(popen_kwargs)
    return subprocess.Popen(argv, **kwargs)


def sandbox_argv(
    argv: list[str],
    *,
    root: str,
    cwd: str | None = None,
    network_policy: str | None = None,
    writable: bool = True,
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
        namespace_cwd = namespace_root + host_cwd[len(root):]

    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--proc", "/proc",
        "--dev", "/dev",
        "--bind" if writable else "--ro-bind", root, namespace_root,
        "--tmpfs", "/tmp",
    ]
    if network_policy and network_policy != "allow":
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
    if executable and not (executable == root or executable.startswith(root + os.sep)):
        parent = os.path.dirname(executable)
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
    namespace_argv = []
    for index, arg in enumerate(argv):
        if index == 0 and executable and (
            arg == argv[0] or os.path.realpath(arg) == executable
        ):
            # Virtualenv launchers are often symlinks into /usr.  The
            # namespace cannot resolve the host-side symlink path, so invoke
            # the already-mounted canonical executable.
            namespace_argv.append(executable)
        elif arg == root or arg.startswith(root + os.sep):
            namespace_argv.append(namespace_root + arg[len(root):])
        else:
            namespace_argv.append(arg)
    command.extend(("--chdir", namespace_cwd, "--"))
    return command + namespace_argv


def _namespace_path(value: str, root: str) -> str:
    """Rewrite workspace-local PATH entries for the /workspace mount."""
    parts = []
    for item in value.split(os.pathsep):
        if item == root or item.startswith(root + os.sep):
            parts.append("/workspace" + item[len(root):])
        else:
            parts.append(item)
    return os.pathsep.join(parts)


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
        except Exception:
            pass
        try:
            process.wait(timeout=timeout)
        except Exception:
            pass
    except Exception:
        pass


def kill_tree(process: "subprocess.Popen", *, timeout: float = 3.0) -> None:
    """SIGTERM then SIGKILL the whole process tree owned by ``process``.

    Signals the owning process group first so descendants are covered even
    without ``psutil``; then, within ``timeout``, escalates to SIGKILL on the
    group and any psutil-enumerated descendants. Finally reaps the root and any
    confirmed-dead children so no zombies are left behind (BHV-061/062).
    """
    if process.poll() is not None:
        return
    pgid = process_group_id(process)
    if os.name == "nt":
        try:
            process.kill()
        except Exception:
            pass
        _reap(process, 5.0)
        return

    assert pgid is not None  # POSIX + live process (checked above) implies a pgid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.05)

    if process.poll() is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        for pid in child_pids(process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                pass

    _reap(process, timeout=5.0)
    for pid in child_pids(process.pid):
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass
        except Exception:
            pass


def interrupt_group(process: "subprocess.Popen") -> None:
    """Forward SIGINT to the owned process group (for interrupt, not kill)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.kill()
        except Exception:
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
    "interrupt_group",
    "process_group_id",
    "process_start_identity",
    "spawn_owned",
]
