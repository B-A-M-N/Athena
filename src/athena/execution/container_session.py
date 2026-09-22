"""Docker runtime-session workers beneath :mod:`athena.execution.container`."""

from __future__ import annotations

import subprocess
import threading
from typing import Any, Mapping

from athena.execution.process_tree import spawn_owned
from athena.execution.runtimes.node import _NodeSession
from athena.execution.runtimes.python import _PythonSession
from athena.protocol.execution import ExecutionRequest


class DockerPythonSession(_PythonSession):
    """The normal Athena Python worker launched through ``docker attach``."""

    def __init__(self, *, command: list[str], env: Mapping[str, str] | None = None) -> None:
        super().__init__(env=dict(env or {}), cwd=None, sandbox_root=None, network_policy=None)
        self._command = command

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


class DockerNodeSession(_NodeSession):
    """The normal Athena Node worker launched through ``docker attach``."""

    def __init__(self, *, command: list[str], env: Mapping[str, str] | None = None) -> None:
        super().__init__(env=dict(env or {}), cwd=None, sandbox_root=None, network_policy=None)
        self._command = command

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


class ContainerSession:
    """A Docker container plus one persistent language worker."""

    def __init__(
        self,
        *,
        session_id: str,
        task_id: str,
        runtime: str,
        container_id: str,
        cwd: str,
        env: Mapping[str, str],
        worker: Any,
        workspace_root: str,
        image_ref: str,
        image_digest: str,
        network_policy: str,
        start_identity: str,
    ) -> None:
        self.id = session_id
        self.task_id = task_id
        self.runtime = runtime
        self.container_id = container_id
        self.cwd = cwd
        self.env = dict(env)
        self.worker = worker
        self.workspace_root = workspace_root
        self.image_ref = image_ref
        self.image_digest = image_digest
        self.network_policy = network_policy
        self.start_identity = start_identity

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


__all__ = ["ContainerSession", "DockerNodeSession", "DockerPythonSession"]
