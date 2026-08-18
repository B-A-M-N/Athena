"""Process-tree helpers: spawn with an own process group, enumerate children,
and terminate the whole owned tree (SIGTERM then SIGKILL).

Used by runtimes and by ExecutionManager to satisfy BHV-061 (process ownership
per task) and BUILDSPEC 49-50 / BHV-062 (process-tree cancellation; orphaned
processes after cancellation are a release-blocking defect).
"""

from __future__ import annotations

import os
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
    kwargs: dict = {"env": my_env}
    if cwd is not None:
        kwargs["cwd"] = cwd
    if os.name != "nt":
        kwargs["start_new_session"] = True
    kwargs.update(popen_kwargs)
    return subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]


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
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
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
    "spawn_owned",
    "process_group_id",
    "child_pids",
    "kill_tree",
    "interrupt_group",
]