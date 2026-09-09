"""Governed SSH execution backend.

SSH profiles are operator-owned backend identities.  Requests select a
configured profile by name; they never supply a host, port, key, or SSH flag.
The backend uses strict known-host verification and the same persistent worker
protocols as local execution.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping

from athena.execution.async_call import run_blocking
from athena.execution.backend import BackendCapabilities, ExecutionBackend
from athena.execution.process_tree import spawn_owned
from athena.execution.runtimes.base import BaseRuntime
from athena.execution.runtimes.node import _NODE_WORKER, _NodeSession
from athena.execution.runtimes.python import _PythonSession
from athena.execution.runtimes.shell import _SubprocessSession
from athena.protocol.execution import ExecutionEvent, ExecutionEventType, ExecutionRequest
from athena.protocol.tasks import NetworkPolicy

__all__ = ["SSHBackend", "SSHProfile"]

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z0-9_./~-]+$")

_REMOTE_PYTHON_SUPERVISOR = r"""
import contextlib, io, json, os, secrets, socket, sys, traceback

socket_path, token_path, metadata_path, session_id, task_id, runtime = sys.argv[1:]
if runtime != "python":
    raise SystemExit("unsupported remote supervisor runtime")
os.makedirs(os.path.dirname(socket_path), exist_ok=True)
try:
    os.unlink(socket_path)
except FileNotFoundError:
    pass
token = secrets.token_urlsafe(32)
with open(token_path, "w", encoding="utf-8") as token_handle:
    os.chmod(token_path, 0o600)
    token_handle.write(token)
    token_handle.flush()
    os.fsync(token_handle.fileno())
pid = os.getpid()
try:
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_handle:
        start_identity = f"{pid}:{stat_handle.read().split()[21]}"
except (OSError, IndexError):
    start_identity = f"{pid}:unknown"
metadata = {
    "session_id": session_id,
    "task_id": task_id,
    "runtime": runtime,
    "pid": pid,
    "start_identity": start_identity,
    "socket_path": socket_path,
    "token_path": token_path,
    "metadata_path": metadata_path,
}
with open(metadata_path, "w", encoding="utf-8") as metadata_handle:
    os.chmod(metadata_path, 0o600)
    json.dump(metadata, metadata_handle, separators=(",", ":"))
    metadata_handle.flush()
    os.fsync(metadata_handle.fileno())

state = {"__name__": "__main__"}
stopping = False

def send(connection, value):
    connection.sendall((json.dumps(value, separators=(",", ":")) + "\n").encode())

def run_source(connection, execution):
    output = io.StringIO()
    error = io.StringIO()
    ok = True
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            exec(str(execution.get("source") or ""), state)
    except BaseException:
        ok = False
        error.write(traceback.format_exc())
    if output.getvalue():
        send(connection, {"type": "out", "data": output.getvalue()})
    if error.getvalue():
        send(connection, {"type": "err", "data": error.getvalue()})
    send(connection, {"type": "done", "ok": ok})

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(socket_path)
os.chmod(socket_path, 0o600)
server.listen(8)
while not stopping:
    connection, _ = server.accept()
    try:
        stream = connection.makefile("rb")
        auth_line = stream.readline()
        auth = json.loads(auth_line.decode())
        if auth.get("token") != token:
            send(connection, {"kind": "error", "error": "remote supervisor authentication failed"})
            continue
        operation = auth.get("op")
        if operation == "describe":
            send(connection, {"kind": "response", **metadata})
            continue
        if operation == "shutdown":
            send(connection, {"kind": "response", "stopping": True})
            stopping = True
            continue
        while True:
            length_line = stream.readline()
            if not length_line:
                break
            length = int(length_line.strip())
            payload = stream.read(length)
            if len(payload) != length:
                break
            run_source(connection, json.loads(payload.decode()))
    except (BrokenPipeError, ConnectionError, ValueError, json.JSONDecodeError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
        connection.close()
server.close()
try:
    os.unlink(socket_path)
except FileNotFoundError:
    pass
try:
    os.unlink(metadata_path)
except FileNotFoundError:
    pass
"""

_REMOTE_RELAY = r"""
import json, os, select, socket, sys
socket_path, token_path = sys.argv[1:]
with open(token_path, encoding="utf-8") as handle:
    token = handle.read().strip()
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect(socket_path)
connection.sendall((json.dumps({"token": token}, separators=(",", ":")) + "\n").encode())
while True:
    readable, _, _ = select.select([0, connection], [], [])
    if 0 in readable:
        chunk = os.read(0, 65536)
        if not chunk:
            break
        connection.sendall(chunk)
    if connection in readable:
        chunk = connection.recv(65536)
        if not chunk:
            break
        os.write(1, chunk)
connection.close()
"""

# The control process never executes user source. It owns a separate worker
# process and handles each authenticated socket connection in its own thread,
# so a blocked worker cannot prevent describe/shutdown from being serviced.
_REMOTE_PYTHON_SUPERVISOR_V2 = r"""
import json, os, signal, socket, subprocess, sys, threading, time

socket_path, token_path, metadata_path, session_id, task_id, runtime, remote_cwd = sys.argv[1:]
if runtime != "python":
    raise SystemExit("unsupported remote supervisor runtime")

def identity(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return f"{pid}:{handle.read().split()[21]}"
    except (OSError, IndexError):
        return f"{pid}:unknown"

WORKER = r'''import contextlib, json, os, sys, traceback
state = {"__name__": "__main__"}
while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        length = int(line.strip())
        payload = json.loads(sys.stdin.read(length))
        source = str(payload.get("source") or "")
    except Exception:
        sys.stdout.write(json.dumps({"type": "err", "data": "bad execution request"}) + "\n")
        sys.stdout.write(json.dumps({"type": "done", "ok": False}) + "\n")
        sys.stdout.flush()
        continue
    out, err = [], []
    class Capture:
        def __init__(self, target): self.target = target
        def write(self, value):
            if value: self.target.append(str(value))
        def flush(self): pass
    ok = True
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = Capture(out), Capture(err)
        ns = dict(state)
        exec(source, ns)
        state.update({k: v for k, v in ns.items() if not k.startswith("__")})
    except BaseException:
        ok = False
        err.append(traceback.format_exc())
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    if out:
        sys.stdout.write(json.dumps({"type": "out", "data": "".join(out)}) + "\n")
    if err:
        sys.stdout.write(json.dumps({"type": "err", "data": "".join(err)}) + "\n")
    sys.stdout.write(json.dumps({"type": "done", "ok": ok}) + "\n")
    sys.stdout.flush()
'''

os.makedirs(os.path.dirname(socket_path), exist_ok=True)
for path in (socket_path, token_path, metadata_path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
token = __import__("secrets").token_urlsafe(32)
with open(token_path, "w", encoding="utf-8") as handle:
    os.chmod(token_path, 0o600)
    handle.write(token)
    handle.flush()
    os.fsync(handle.fileno())
controller_pid = os.getpid()
metadata = {
    "session_id": session_id,
    "task_id": task_id,
    "runtime": runtime,
    "pid": controller_pid,
    "start_identity": identity(controller_pid),
    "socket_path": socket_path,
    "token_path": token_path,
    "metadata_path": metadata_path,
}
with open(metadata_path, "w", encoding="utf-8") as handle:
    os.chmod(metadata_path, 0o600)
    json.dump(metadata, handle, separators=(",", ":"))
    handle.flush()
    os.fsync(handle.fileno())

worker = None
worker_identity = None
session_env = {}
worker_lock = threading.Lock()
stop = threading.Event()
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(socket_path)
os.chmod(socket_path, 0o600)
server.listen(16)
server.settimeout(0.2)

def send(connection, value):
    connection.sendall((json.dumps(value, separators=(",", ":")) + "\n").encode())

def ensure_worker(env=None):
    global worker, worker_identity
    if worker is not None and worker.poll() is None:
        return worker
    merged = os.environ.copy()
    merged.update({str(k): str(v) for k, v in session_env.items()})
    merged.update({str(k): str(v) for k, v in (env or {}).items()})
    worker = subprocess.Popen(
        [sys.executable, "-u", "-c", WORKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=remote_cwd,
        env=merged,
        start_new_session=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    worker_identity = identity(worker.pid)
    metadata["worker_pid"] = worker.pid
    metadata["worker_start_identity"] = worker_identity
    return worker

def stop_worker():
    global worker
    current = worker
    if current is None:
        return {"confirmed": True, "worker_pid": None, "worker_start_identity": None, "alive_after": False}
    pid = current.pid
    start = worker_identity
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        current.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            current.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
    alive = current.poll() is None
    if not alive:
        try:
            alive = identity(pid) == start and os.kill(pid, 0) == 0
        except (OSError, ProcessLookupError):
            alive = False
    return {
        "confirmed": not alive,
        "worker_pid": pid,
        "worker_start_identity": start,
        "alive_after": alive,
    }

def handle(connection):
    global worker
    stream = None
    try:
        stream = connection.makefile("rb")
        auth_line = stream.readline()
        auth = json.loads(auth_line.decode())
        if auth.get("token") != token:
            send(connection, {"kind": "error", "error": "remote supervisor authentication failed"})
            return
        operation = auth.get("op")
        if operation == "describe":
            described = dict(metadata)
            described["start_identity"] = identity(controller_pid)
            described["worker_alive"] = bool(worker is not None and worker.poll() is None)
            if worker is not None and worker.poll() is None:
                described["worker_start_identity"] = identity(worker.pid)
            send(connection, {"kind": "response", **described})
            return
        if operation == "shutdown":
            receipt = stop_worker()
            receipt.update({"kind": "response", "controller_pid": controller_pid})
            send(connection, receipt)
            stop.set()
            return
        if operation == "interrupt":
            current = worker
            interrupted = bool(current is not None and current.poll() is None)
            if interrupted:
                try:
                    os.killpg(current.pid, signal.SIGINT)
                except ProcessLookupError:
                    interrupted = False
            send(
                connection,
                {
                    "kind": "response",
                    "interrupted": interrupted,
                    "worker_pid": current.pid if current is not None else None,
                },
            )
            return
        while True:
            length_line = stream.readline()
            if not length_line:
                break
            length = int(length_line.strip())
            payload = stream.read(length)
            if len(payload) != length:
                break
            request = json.loads(payload.decode())
            if request.get("op") == "configure":
                session_env.update(
                    {str(key): str(value) for key, value in (request.get("env") or {}).items()}
                )
                ensure_worker()
                continue
            current = ensure_worker()
            if current.stdin is None or current.stdout is None:
                break
            with worker_lock:
                current.stdin.write(length_line.decode())
                current.stdin.write(payload.decode())
                current.stdin.flush()
                while True:
                    frame = current.stdout.readline()
                    if not frame:
                        break
                    connection.sendall(frame.encode())
                    try:
                        if json.loads(frame).get("type") == "done":
                            break
                    except Exception:
                        pass
                if current.poll() is not None:
                    break
    except (BrokenPipeError, ConnectionError, ValueError, json.JSONDecodeError, OSError):
        pass
    finally:
        if stream is not None:
            try: stream.close()
            except Exception: pass
        try: connection.close()
        except Exception: pass

threads = []
try:
    while not stop.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        thread = threading.Thread(target=handle, args=(connection,), daemon=True)
        threads.append(thread)
        thread.start()
finally:
    try: server.close()
    except Exception: pass
    for thread in threads:
        thread.join(timeout=2.0)
    try: os.unlink(socket_path)
    except FileNotFoundError: pass
    try: os.unlink(token_path)
    except FileNotFoundError: pass
    try: os.unlink(metadata_path)
    except FileNotFoundError: pass
"""


@dataclass(frozen=True)
class SSHProfile:
    name: str
    host: str
    user: str
    port: int = 22
    credential_id: str | None = None
    identity_file: str | None = None
    known_hosts: str = ""
    remote_root: str = "~/athena-workspaces"
    connect_timeout: float = 15.0

    def __post_init__(self) -> None:
        for field_name in ("name", "host", "user"):
            value = str(getattr(self, field_name)).strip()
            if not value or any(char.isspace() for char in value):
                raise ValueError(f"SSH profile {field_name} must be a non-empty token")
            object.__setattr__(self, field_name, value)
        if _SAFE_HOST.fullmatch(self.host) is None or _SAFE_SEGMENT.fullmatch(self.user) is None:
            raise ValueError("SSH profile host/user contains unsafe characters")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("SSH profile port must be between 1 and 65535")
        if not str(self.known_hosts).strip():
            raise ValueError("SSH profile requires a known_hosts path")
        root = str(self.remote_root).strip()
        if (
            not root
            or "\x00" in root
            or ".." in root.split("/")
            or _SAFE_REMOTE_PATH.fullmatch(root) is None
        ):
            raise ValueError("SSH profile remote_root is invalid")
        object.__setattr__(self, "remote_root", root)
        if self.credential_id is None and self.identity_file is None:
            raise ValueError("SSH profile requires credential_id or identity_file")
        object.__setattr__(self, "connect_timeout", max(1.0, float(self.connect_timeout)))


class _SSHWorker:
    def __init__(self, command: list[str], *, env: Mapping[str, str] | None = None) -> None:
        self._command = command
        self._env = dict(env or {})


class _SSHPythonSession(_PythonSession, _SSHWorker):
    def __init__(self, *, command: list[str], env: Mapping[str, str] | None = None) -> None:
        _PythonSession.__init__(self, env=dict(env or {}), cwd=None, sandbox_root=None)
        _SSHWorker.__init__(self, command, env=env)

    def start(self) -> None:
        self.process = spawn_owned(
            self._command,
            env={},
            cwd=None,
            sandbox_root=None,
            network_policy=None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if self.process.stdout is not None:
            threading.Thread(
                target=self._read_loop, args=(self.process.stdout,), daemon=True
            ).start()
        # Environment values cross the SSH boundary only inside the
        # authenticated, length-framed supervisor protocol. They never appear
        # in the SSH command line or a process argument list.
        if self._env and self.process.stdin is not None:
            payload = json.dumps({"op": "configure", "env": self._env})
            self.process.stdin.write(f"{len(payload)}\n{payload}")
            self.process.stdin.flush()


class _SSHNodeSession(_NodeSession, _SSHWorker):
    def __init__(self, *, command: list[str], env: Mapping[str, str] | None = None) -> None:
        _NodeSession.__init__(self, env=dict(env or {}), cwd=None, sandbox_root=None)
        _SSHWorker.__init__(self, command, env=env)

    def start(self) -> None:
        self.process = spawn_owned(
            self._command,
            env={},
            cwd=None,
            sandbox_root=None,
            network_policy=None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if self.process.stdout is not None:
            threading.Thread(
                target=self._read_loop, args=(self.process.stdout,), daemon=True
            ).start()


@dataclass
class _SSHSession:
    id: str
    task_id: str
    runtime: str
    remote_cwd: str
    worker: Any
    host: str
    start_identity: str
    key_path: str | None = None
    remote_socket: str | None = None
    remote_token_path: str | None = None
    remote_metadata_path: str | None = None
    remote_controller_pid: str | None = None

    def run(self, request: ExecutionRequest, execution_id: str) -> Any:
        return self.worker.run(request.source, request.timeout, execution_id)

    def interrupt(self) -> None:
        interrupt = getattr(self.worker, "interrupt", None)
        if interrupt is not None:
            interrupt()

    def close(self) -> None:
        close = getattr(self.worker, "close", None)
        if close is not None:
            close()
        if self.key_path:
            try:
                os.unlink(self.key_path)
            except FileNotFoundError:
                pass
            self.key_path = None


class SSHBackend(ExecutionBackend):
    """Run persistent Python, shell, and Node workers over a fixed SSH profile."""

    # Python sessions are owned by the authenticated remote supervisor and
    # can be adopted after Athena restarts. Direct shell/Node sessions remain
    # process-lifetime resources until their supervisor protocol is available.
    supports_reattach = True

    def __init__(
        self,
        profile: SSHProfile,
        *,
        secret_manager: Any = None,
        ssh_command: str = "ssh",
    ) -> None:
        self.profile = profile
        self.name = profile.name
        self._secrets = secret_manager
        self.ssh_command = ssh_command
        self._sessions: dict[str, _SSHSession] = {}
        self._tasks: dict[str, list[str]] = {}
        self._executions: dict[str, _SSHSession] = {}

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supported_runtimes=("node", "python", "shell"),
            persistent_sessions=True,
            # Python sessions are owned by the authenticated remote
            # supervisor and can be adopted after an Athena restart. Shell
            # and Node remain process-lifetime sessions without reattach or
            # secret transport until their supervisor protocol exists.
            reattach=False,
            filesystem_persistence=True,
            network_modes=("allow",),
            secret_materialization=False,
            interactive_stdin=True,
            process_signals=True,
            # Remote package installation needs a remote lock/materialization
            # protocol; do not report host-path installation as supported.
            dependency_installation=(),
            runtime_capabilities={
                "python": {
                    "persistent_sessions": True,
                    "reattach": True,
                    "secret_materialization": True,
                    "interactive_stdin": True,
                    "process_signals": True,
                },
                "node": {
                    "persistent_sessions": True,
                    "reattach": False,
                    "secret_materialization": False,
                    "interactive_stdin": True,
                    "process_signals": False,
                },
                "shell": {
                    "persistent_sessions": True,
                    "reattach": False,
                    "secret_materialization": False,
                    "interactive_stdin": True,
                    "process_signals": False,
                },
            },
        )

    def available(self) -> bool:
        if shutil.which(self.ssh_command) is None:
            return False
        return os.path.isfile(os.path.expanduser(self.profile.known_hosts))

    def environment_identity(self) -> dict[str, str]:
        return {
            "host": self.profile.host,
            "user": self.profile.user,
            "port": str(self.profile.port),
            "known_hosts": os.path.realpath(os.path.expanduser(self.profile.known_hosts)),
        }

    def _credential_path(self, task_id: str) -> str | None:
        if self.profile.identity_file:
            path = os.path.realpath(os.path.expanduser(self.profile.identity_file))
            if not os.path.isfile(path):
                raise RuntimeError(f"SSH identity file does not exist: {path}")
            return path
        if self._secrets is None:
            raise RuntimeError("SSH credential requires the configured SecretManager")
        value = self._secrets.resolve(
            self.profile.credential_id,
            owner_task=task_id,
            backend=self.name,
        )
        if not value:
            raise RuntimeError("SSH credential resolved to an empty value")
        if os.path.isfile(os.path.expanduser(value)):
            return os.path.realpath(os.path.expanduser(value))
        if "BEGIN" not in value:
            raise RuntimeError("SSH credential must resolve to a private key or key path")
        handle = tempfile.NamedTemporaryFile(prefix="athena-ssh-", mode="w", delete=False)
        try:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(value)
            handle.flush()
            return handle.name
        finally:
            handle.close()

    def _target_root(self, task_id: str) -> str:
        if not _SAFE_SEGMENT.fullmatch(str(task_id)):
            raise ValueError("task id cannot be used as an SSH workspace segment")
        return self.profile.remote_root.rstrip("/") + "/" + str(task_id)

    def _ssh_base(self, key_path: str | None) -> list[str]:
        known_hosts = os.path.realpath(os.path.expanduser(self.profile.known_hosts))
        if not os.path.isfile(known_hosts):
            raise RuntimeError(f"SSH known_hosts file does not exist: {known_hosts}")
        command = [
            self.ssh_command,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"ConnectTimeout={int(self.profile.connect_timeout)}",
            "-p",
            str(self.profile.port),
        ]
        if key_path:
            command.extend(("-i", key_path, "-o", "IdentitiesOnly=yes"))
        command.append(f"{self.profile.user}@{self.profile.host}")
        return command

    @staticmethod
    def _encoded_command(program: str, source: str, cwd: str) -> str:
        encoded = base64.b64encode(source.encode()).decode("ascii")
        # The remote shell receives only fixed interpreter text and a
        # base64-encoded worker; model source travels later over the worker
        # protocol, never as an SSH command argument.
        if program == "python":
            runner = f"python3 -u -c \"import base64;exec(base64.b64decode('{encoded}'))\""
        else:
            runner = f"node -e \"eval(Buffer.from('{encoded}','base64').toString())\""
        return f"mkdir -p -- {cwd!s} && cd -- {cwd!s} && exec {runner}"

    @staticmethod
    def _remote_arg(path: str) -> str:
        """Render a validated remote path while preserving ``~/`` expansion."""
        value = str(path)
        if value.startswith("~/"):
            return "$HOME/" + value[2:]
        return shlex.quote(value)

    def _supervisor_paths(self, task_id: str, session_id: str) -> dict[str, str]:
        base = self._target_root(task_id).rstrip("/") + "/.athena-supervisor/" + session_id
        return {
            "socket": base + ".sock",
            "token": base + ".token",
            "metadata": base + ".json",
        }

    def _run_remote_command(
        self,
        command: str,
        *,
        key_path: str | None,
        timeout: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [*self._ssh_base(key_path), command],
            capture_output=True,
            text=True,
            timeout=timeout or self.profile.connect_timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "remote command failed").strip()
            raise RuntimeError(f"SSH remote supervisor command failed: {detail[-1000:]}")
        return completed

    def _start_remote_python_supervisor(
        self,
        *,
        task_id: str,
        session_id: str,
        remote_cwd: str,
        key_path: str | None,
    ) -> dict[str, str]:
        paths = self._supervisor_paths(task_id, session_id)
        root = self._remote_arg(remote_cwd)
        socket_path = self._remote_arg(paths["socket"])
        token_path = self._remote_arg(paths["token"])
        metadata_path = self._remote_arg(paths["metadata"])
        encoded = base64.b64encode(_REMOTE_PYTHON_SUPERVISOR_V2.encode()).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"mkdir -p -- {root} {self._remote_arg(paths['socket'].rsplit('/', 1)[0])}; "
            f"nohup python3 -u -c {shlex.quote(launcher)} {socket_path} {token_path} "
            f"{metadata_path} {shlex.quote(session_id)} {shlex.quote(task_id)} python {root} "
            "></dev/null >/dev/null 2>&1 &"
        )
        self._run_remote_command(command, key_path=key_path)
        for _ in range(50):
            probe = self._run_remote_command(
                f"test -s {metadata_path} && cat {metadata_path}",
                key_path=key_path,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                try:
                    metadata = json.loads(probe.stdout)
                except json.JSONDecodeError:
                    metadata = None
                if isinstance(metadata, dict) and metadata.get("session_id") == session_id:
                    return {str(key): str(value) for key, value in metadata.items()}
            time.sleep(0.1)
        raise RuntimeError("remote Python supervisor did not become ready")

    def _remote_relay_command(self, paths: Mapping[str, str]) -> str:
        encoded = base64.b64encode(_REMOTE_RELAY.encode()).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        return (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self._remote_arg(paths['socket_path'])} {self._remote_arg(paths['token_path'])}"
        )

    def _remote_describe(
        self,
        *,
        socket_path: str,
        token_path: str,
        key_path: str | None,
    ) -> Mapping[str, Any]:
        encoded = base64.b64encode(
            (
                "import json,socket,sys; "
                "p,t=sys.argv[1:]; token=open(t).read().strip(); "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
                "s.sendall((json.dumps({'token':token,'op':'describe'})+'\\n').encode()); "
                "print(s.recv(65536).decode().strip()); s.close()"
            ).encode()
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self._remote_arg(socket_path)} {self._remote_arg(token_path)}"
        )
        completed = self._run_remote_command(
            command,
            key_path=key_path,
            check=False,
            timeout=self.profile.connect_timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError("remote supervisor describe failed")
        try:
            value = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("remote supervisor describe response is invalid") from exc
        if not isinstance(value, Mapping) or value.get("kind") != "response":
            raise RuntimeError("remote supervisor describe response is not authoritative")
        return value

    def _remote_interrupt(self, session: _SSHSession) -> bool:
        if not session.remote_socket or not session.remote_token_path:
            return False
        encoded = base64.b64encode(
            (
                "import json,socket,sys; "
                "p,t=sys.argv[1:]; token=open(t).read().strip(); "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
                "s.sendall((json.dumps({'token':token,'op':'interrupt'})+'\\n').encode()); "
                "print(s.recv(65536).decode().strip()); s.close()"
            ).encode()
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self._remote_arg(session.remote_socket)} {self._remote_arg(session.remote_token_path)}"
        )
        completed = self._run_remote_command(
            command, key_path=session.key_path, check=False, timeout=self.profile.connect_timeout
        )
        try:
            response = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError):
            return False
        return bool(response.get("interrupted"))

    def _remote_shutdown(self, session: _SSHSession) -> dict[str, Any]:
        if not session.remote_socket or not session.remote_token_path:
            return {"confirmed": True, "alive_after": False}
        control = {
            "socket_path": session.remote_socket,
            "token_path": session.remote_token_path,
        }
        encoded = base64.b64encode(
            (
                "import json,socket,sys; "
                "p,t=sys.argv[1:]; token=open(t).read().strip(); "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect(p); "
                "s.sendall((json.dumps({'token':token,'op':'shutdown'})+'\\n').encode()); "
                "print(s.recv(65536).decode().strip()); s.close()"
            ).encode()
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded!r}))"
        command = (
            f"python3 -u -c {shlex.quote(launcher)} "
            f"{self._remote_arg(control['socket_path'])} {self._remote_arg(control['token_path'])}"
        )
        completed = self._run_remote_command(
            command,
            key_path=session.key_path,
            timeout=max(self.profile.connect_timeout, 5.0),
            check=False,
        )
        try:
            receipt = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, TypeError, json.JSONDecodeError):
            return {
                "confirmed": False,
                "alive_after": True,
                "error": (completed.stderr or completed.stdout or "shutdown receipt missing")[
                    -1000:
                ],
            }
        controller_pid = str(
            receipt.get("controller_pid")
            or session.remote_controller_pid
            or str(session.start_identity).split(":", 1)[0]
        )
        metadata_path = session.remote_metadata_path or "/dev/null"
        probe = (
            f"for i in 1 2 3 4 5 6 7 8 9 10; do "
            f"if ! kill -0 {shlex.quote(controller_pid)} 2>/dev/null "
            f"&& test ! -e {self._remote_arg(session.remote_socket)} "
            f"&& test ! -e {self._remote_arg(session.remote_token_path)} "
            f"&& test ! -e {self._remote_arg(metadata_path)}; then exit 0; fi; "
            "sleep 0.1; done; exit 1"
        )
        verified = self._run_remote_command(
            probe,
            key_path=session.key_path,
            timeout=max(self.profile.connect_timeout, 5.0),
            check=False,
        )
        receipt["controller_alive_after"] = verified.returncode != 0
        receipt["confirmed"] = bool(receipt.get("confirmed")) and verified.returncode == 0
        receipt["alive_after"] = bool(receipt.get("alive_after")) or verified.returncode != 0
        return receipt

    def _make_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
        workspace_root: str | None,
        network_policy: NetworkPolicy | str | None,
    ) -> _SSHSession:
        del workspace_root
        policy = getattr(network_policy, "value", network_policy)
        if policy not in {None, NetworkPolicy.ALLOW.value}:
            raise ValueError("SSH backend cannot prove denied/restricted remote networking")
        canonical = str(runtime).casefold()
        if canonical in {"python3", "py"}:
            canonical = "python"
        elif canonical in {"bash", "sh", "zsh"}:
            canonical = "shell"
        elif canonical in {"nodejs", "js", "javascript"}:
            canonical = "node"
        if canonical not in {"python", "shell", "node"}:
            raise ValueError("SSH backend supports python, shell, and node")
        if env and canonical != "python":
            raise ValueError(
                "SSH environment/secret materialization is supported only for the "
                "authenticated Python supervisor"
            )
        key_path = self._credential_path(task_id)
        remote_cwd = cwd or self._target_root(task_id)
        # ExecutionRequest.cwd is normally the host workspace path. SSH has
        # no host-path namespace, so an out-of-profile cwd maps to this task's
        # deterministic remote workspace instead of being rejected or leaked
        # into an arbitrary remote directory.
        if (
            not remote_cwd.startswith(self.profile.remote_root.rstrip("/") + "/")
            or _SAFE_REMOTE_PATH.fullmatch(remote_cwd) is None
        ):
            remote_cwd = self._target_root(task_id)
        session_id = f"ssh_{task_id}_{secrets.token_hex(8)}"
        remote_metadata: dict[str, str] | None = None
        worker: Any
        try:
            if canonical == "shell":
                remote_command = f"mkdir -p -- {remote_cwd} && cd -- {remote_cwd} && exec bash --norc --noprofile"
            elif canonical == "python":
                remote_metadata = self._start_remote_python_supervisor(
                    task_id=task_id,
                    session_id=session_id,
                    remote_cwd=remote_cwd,
                    key_path=key_path,
                )
                relay_paths = {
                    "socket_path": remote_metadata["socket_path"],
                    "token_path": remote_metadata["token_path"],
                }
                remote_command = self._remote_relay_command(relay_paths)
            else:
                remote_command = self._encoded_command("node", _NODE_WORKER, remote_cwd)
            command = [*self._ssh_base(key_path), remote_command]
            if canonical == "shell":
                worker = _SubprocessSession(
                    env={}, cwd=None, start_cmd=command, sandbox_root=None, network_policy=None
                )
            elif canonical == "python":
                worker = _SSHPythonSession(command=command, env=env)
            else:
                worker = _SSHNodeSession(command=command, env=env)
            worker.start()
        except Exception:
            if remote_metadata is not None:
                self._remote_shutdown(
                    _SSHSession(
                        id=session_id,
                        task_id=task_id,
                        runtime=canonical,
                        remote_cwd=remote_cwd,
                        worker=None,
                        host=self.profile.host,
                        start_identity=remote_metadata.get("start_identity", session_id),
                        key_path=key_path,
                        remote_socket=remote_metadata.get("socket_path"),
                        remote_token_path=remote_metadata.get("token_path"),
                        remote_controller_pid=remote_metadata.get("pid"),
                    )
                )
            if self.profile.identity_file is None and key_path:
                try:
                    os.unlink(key_path)
                except FileNotFoundError:
                    pass
            raise
        return _SSHSession(
            id=session_id,
            task_id=task_id,
            runtime=canonical,
            remote_cwd=remote_cwd,
            worker=worker,
            host=self.profile.host,
            start_identity=(remote_metadata or {}).get("start_identity", session_id),
            key_path=key_path if self.profile.identity_file is None else None,
            remote_socket=(remote_metadata or {}).get("socket_path"),
            remote_token_path=(remote_metadata or {}).get("token_path"),
            remote_metadata_path=(remote_metadata or {}).get("metadata_path"),
            remote_controller_pid=(remote_metadata or {}).get("pid"),
        )

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
        network_policy: NetworkPolicy | str | None = None,
    ) -> str:
        session = await run_blocking(
            self._make_session,
            task_id=task_id,
            runtime=runtime,
            cwd=cwd,
            env=env,
            workspace_root=workspace_root,
            network_policy=network_policy,
        )
        self._sessions[session.id] = session
        self._tasks.setdefault(task_id, []).append(session.id)
        return session.id

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        execution_id = str(request.metadata.get("__execution_id") or secrets.token_hex(12))
        session = self._sessions.get(request.runtime_session_id or "")
        if session is None:
            for session_id in self._tasks.get(request.task_id, []):
                candidate = self._sessions.get(session_id)
                if candidate is not None and candidate.runtime == _runtime_name(request.runtime):
                    session = candidate
                    break
        if session is None:
            session_id = await self.create_session(
                task_id=request.task_id,
                runtime=request.runtime,
                cwd=request.cwd,
                env=request.env,
                workspace_root=request.workspace_root,
                network_policy=request.network_policy,
            )
            session = self._sessions[session_id]
        self._executions[execution_id] = session
        yield ExecutionEvent(
            type=ExecutionEventType.STARTED,
            execution_id=execution_id,
            metadata={
                "runtime_session_id": session.id,
                "backend": self.name,
                "host": session.host,
                "remote_cwd": session.remote_cwd,
                "start_identity": session.start_identity,
            },
        )
        try:
            async for event in BaseRuntime._bridge_sync_generator(
                session.run(request, execution_id)
            ):
                yield event
        finally:
            self._executions.pop(execution_id, None)

    async def interrupt(self, execution_id: str) -> None:
        session = self._executions.get(execution_id)
        if session is not None:
            if session.remote_socket:
                await run_blocking(self._remote_interrupt, session)
            else:
                session.interrupt()

    async def destroy_session(self, runtime_session_id: str) -> None:
        session = self._sessions.get(runtime_session_id)
        if session is None:
            return
        receipt: dict[str, Any] = {"confirmed": True}
        if session.remote_socket:
            receipt = await run_blocking(self._remote_shutdown, session)
            if not bool(receipt.get("confirmed")):
                raise RuntimeError(
                    "SSH supervisor shutdown was not proven; remote resource remains unresolved"
                )
        session.close()
        self._sessions.pop(runtime_session_id, None)
        ids = self._tasks.get(session.task_id, [])
        if runtime_session_id in ids:
            ids.remove(runtime_session_id)
        if not ids:
            self._tasks.pop(session.task_id, None)

    async def describe_session(self, runtime_session_id: str) -> Mapping[str, str]:
        session = self._sessions.get(runtime_session_id)
        if session is None:
            raise RuntimeError(f"unknown SSH runtime session: {runtime_session_id}")
        value: dict[str, str] = {
            "host": session.host,
            "process_identity": session.start_identity,
            "start_identity": session.start_identity,
            "workspace_identity": session.remote_cwd,
            "network_policy": "operator-profile",
        }
        if session.remote_socket:
            value.update(
                {
                    "remote_socket": session.remote_socket,
                    "remote_token_path": session.remote_token_path or "",
                    "remote_metadata_path": session.remote_metadata_path or "",
                    "controller_pid": session.remote_controller_pid or "",
                    "remote_supervisor": "python-unix-socket",
                }
            )
        return value

    async def reattach_session(self, record: Mapping[str, Any]) -> str:
        """Adopt a remote Python supervisor after Athena itself restarted."""
        runtime = _runtime_name(str(record.get("runtime") or ""))
        if runtime != "python":
            raise RuntimeError("SSH reattachment is currently available for Python sessions only")
        raw_metadata = record.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        session_id = str(record.get("id") or "")
        task_id = str(record.get("task_id") or "")
        remote_socket = str(metadata.get("remote_socket") or record.get("remote_socket") or "")
        remote_token_path = str(
            metadata.get("remote_token_path") or record.get("remote_token_path") or ""
        )
        remote_metadata_path = str(
            metadata.get("remote_metadata_path") or record.get("remote_metadata_path") or ""
        )
        if (
            not session_id
            or not task_id
            or not remote_socket
            or not remote_token_path
            or not remote_metadata_path
        ):
            raise RuntimeError("SSH runtime record lacks remote supervisor identity")
        expected_suffixes = (
            f"/.athena-supervisor/{session_id}.sock",
            f"/.athena-supervisor/{session_id}.token",
            f"/.athena-supervisor/{session_id}.json",
        )
        for path, suffix in zip(
            (remote_socket, remote_token_path, remote_metadata_path), expected_suffixes
        ):
            if (
                _SAFE_REMOTE_PATH.fullmatch(path) is None
                or ".." in path.split("/")
                or not path.endswith(suffix)
            ):
                raise RuntimeError("SSH runtime record contains an invalid supervisor path")
        key_path = await run_blocking(self._credential_path, task_id)
        try:
            probe = await run_blocking(
                self._run_remote_command,
                f"test -s {self._remote_arg(remote_metadata_path)} && cat {self._remote_arg(remote_metadata_path)}",
                key_path=key_path,
                check=False,
            )
            if probe.returncode != 0:
                raise RuntimeError("remote supervisor metadata is unavailable")
            try:
                remote = json.loads(probe.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("remote supervisor metadata is invalid") from exc
            if not isinstance(remote, Mapping):
                raise RuntimeError("remote supervisor metadata is not an object")
            if str(remote.get("session_id")) != session_id:
                raise RuntimeError("remote supervisor session identity mismatch")
            if str(remote.get("task_id")) != task_id:
                raise RuntimeError("remote supervisor task ownership mismatch")
            if str(remote.get("runtime")) != "python":
                raise RuntimeError("remote supervisor runtime identity mismatch")
            expected_start = str(
                record.get("start_identity") or metadata.get("start_identity") or ""
            )
            if expected_start and str(remote.get("start_identity")) != expected_start:
                raise RuntimeError("remote supervisor process identity mismatch")
            pid = str(remote.get("pid") or "")
            if not pid.isdigit():
                raise RuntimeError("remote supervisor pid identity is invalid")
            described = await run_blocking(
                self._remote_describe,
                socket_path=remote_socket,
                token_path=remote_token_path,
                key_path=key_path,
            )
            if str(described.get("session_id")) != session_id:
                raise RuntimeError("remote supervisor live session identity mismatch")
            if str(described.get("task_id")) != task_id:
                raise RuntimeError("remote supervisor live task ownership mismatch")
            live_start = str(described.get("start_identity") or "")
            if not live_start or live_start != expected_start:
                raise RuntimeError("remote supervisor live process identity mismatch")
            relay_paths = {"socket_path": remote_socket, "token_path": remote_token_path}
            command = [
                *self._ssh_base(key_path),
                self._remote_relay_command(relay_paths),
            ]
            worker = _SSHPythonSession(command=command)
            worker.start()
            session = _SSHSession(
                id=session_id,
                task_id=task_id,
                runtime="python",
                remote_cwd=str(
                    record.get("workspace_identity") or metadata.get("workspace_identity") or ""
                ),
                worker=worker,
                host=self.profile.host,
                start_identity=str(remote.get("start_identity") or expected_start),
                key_path=key_path if self.profile.identity_file is None else None,
                remote_socket=remote_socket,
                remote_token_path=remote_token_path,
                remote_metadata_path=remote_metadata_path,
                remote_controller_pid=str(remote.get("pid") or ""),
            )
            self._sessions[session_id] = session
            self._tasks.setdefault(task_id, []).append(session_id)
            return session_id
        except Exception:
            if self.profile.identity_file is None and key_path:
                try:
                    os.unlink(key_path)
                except FileNotFoundError:
                    pass
            raise

    async def shutdown(self) -> None:
        for session_id in list(self._sessions):
            await self.destroy_session(session_id)


def _runtime_name(value: str) -> str:
    value = str(value).casefold()
    return {
        "python3": "python",
        "py": "python",
        "bash": "shell",
        "sh": "shell",
        "zsh": "shell",
        "nodejs": "node",
        "js": "node",
        "javascript": "node",
    }.get(value, value)
