"""Fail-closed Bubblewrap command construction for local execution."""

from __future__ import annotations

import os
import shutil
import socket
import sys
from functools import lru_cache


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

    The workspace is writable; common interpreter/toolchain directories are
    read-only; temporary storage is private; and non-ALLOW network policy
    requests use a private network namespace. Missing Bubblewrap is an error,
    never a host-execution fallback.
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
    # Retain an inherited network deny filter when Bubblewrap cannot create a
    # namespace on this host; socket-capable hosts still get --unshare-net.
    if network_policy and network_policy != "allow" and not network_syscalls_denied():
        command.append("--unshare-net")

    for path in ("/usr", "/bin", "/lib", "/lib64"):
        if os.path.exists(path):
            command.extend(("--ro-bind", path, path))
    executable = ""
    if argv:
        resolved = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
        executable = os.path.realpath(resolved or argv[0])
    for tool in dict.fromkeys(
        item for item in (executable, os.path.realpath(sys.executable)) if item
    ):
        if tool == root or tool.startswith(root + os.sep):
            continue
        parent = os.path.dirname(tool)
        if os.path.basename(parent) == "bin":
            parent = os.path.dirname(parent)
        if parent and os.path.exists(parent) and parent not in ("/usr", "/bin", "/lib", "/lib64"):
            ancestors = _ancestors(parent)
            for directory in reversed(ancestors):
                if directory not in ("/", "/usr", "/bin", "/lib", "/lib64"):
                    command.extend(("--dir", directory))
            command.extend(("--ro-bind", parent, parent))

    for raw_path in toolchain_paths:
        path = os.path.realpath(os.path.abspath(raw_path))
        if not os.path.exists(path):
            raise RuntimeError(f"trusted toolchain path does not exist: {raw_path}")
        bind_path = path
        parent = os.path.dirname(path) if os.path.isfile(path) else path
        for directory in reversed(_ancestors(parent)):
            if directory not in ("/", "/usr", "/bin", "/lib", "/lib64"):
                command.extend(("--dir", directory))
        command.extend(("--ro-bind", bind_path, bind_path))

    for raw_path in writable_toolchain_paths:
        path = os.path.realpath(os.path.abspath(raw_path))
        if not os.path.exists(path):
            raise RuntimeError(f"trusted writable toolchain path does not exist: {raw_path}")
        parent = path if os.path.isdir(path) else os.path.dirname(path)
        for directory in reversed(_ancestors(parent)):
            if directory not in ("/", "/usr", "/bin", "/lib", "/lib64"):
                command.extend(("--dir", directory))
        command.extend(("--bind", path, path))

    if writable_paths is not None:
        for path in writable_paths:
            _append_workspace_mount(command, path, root, namespace_root, "--bind")
        for path in read_only_paths:
            _append_workspace_mount(command, path, root, namespace_root, "--ro-bind")
    namespace_argv = []
    for index, arg in enumerate(argv):
        if index == 0 and executable and (arg == argv[0] or os.path.realpath(arg) == executable):
            namespace_argv.append(executable)
        elif arg == root or arg.startswith(root + os.sep):
            namespace_argv.append(namespace_root + arg[len(root) :])
        else:
            namespace_argv.append(arg)
    command.extend(("--chdir", namespace_cwd, "--"))
    return command + namespace_argv


def _ancestors(path: str) -> list[str]:
    result: list[str] = []
    ancestor = path
    while ancestor not in ("", os.path.dirname(ancestor)):
        result.append(ancestor)
        ancestor = os.path.dirname(ancestor)
    return result


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


def namespace_path(value: str, root: str) -> str:
    """Rewrite workspace-local PATH entries for the /workspace mount."""
    parts = []
    for item in value.split(os.pathsep):
        parts.append(
            "/workspace" + item[len(root) :]
            if item == root or item.startswith(root + os.sep)
            else item
        )
    return os.pathsep.join(parts)


@lru_cache(maxsize=1)
def network_syscalls_denied() -> bool:
    """Report whether this process already inherits a network deny filter."""
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


__all__ = ["namespace_path", "network_syscalls_denied", "sandbox_argv"]
