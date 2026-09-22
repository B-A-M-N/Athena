"""Authenticated SSH worker/session mechanics beneath ``SSHBackend``."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from athena.execution.process_tree import spawn_owned
from athena.execution.runtimes.node import _NodeSession
from athena.execution.runtimes.python import _PythonSession
from athena.execution.ssh_protocol import encode_frame
from athena.protocol.execution import ExecutionRequest


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
            frame = encode_frame({"op": "configure", "env": self._env})
            self.process.stdin.write(frame.decode("utf-8"))
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
    runtime_identity: str | None = None
    worker_source_sha256: str | None = None
    session_nonce: str | None = None
    authority_digest: str | None = None
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


__all__ = ["_SSHNodeSession", "_SSHPythonSession", "_SSHSession"]
