"""Docker-backed execution backend.

The container backend is deliberately small and boring.  It owns one Docker
container per Athena runtime session, mounts the workspace read-only, uses a
private network namespace for denied/restricted network policy, and runs the
same persistent worker protocols as the local Python and shell runtimes.

Docker is optional.  Importing Athena must continue to work without the
optional package or a running daemon; selecting this backend then fails
closed with an actionable error.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from typing import Any, AsyncIterator, Mapping

from athena.execution.backend import ExecutionBackend
from athena.execution.process_tree import spawn_owned
from athena.execution.runtimes.base import BaseRuntime
from athena.execution.runtimes.python import _PythonSession, _WORKER_SOURCE
from athena.execution.runtimes.shell import _SubprocessSession
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionRequest,
)
from athena.protocol.tasks import NetworkPolicy

__all__ = ["ContainerBackend", "available_backends"]

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORKSPACE_MOUNT = "/workspace"
_CONTAINER_CWD = "/workspace"


class _DockerPythonSession(_PythonSession):
    """The normal Athena Python worker launched through ``docker exec``."""

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


class _ContainerSession:
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


class ContainerBackend(ExecutionBackend):
    """Run Athena's persistent runtimes inside Docker.

    The backend intentionally supports the runtimes whose worker protocols
    are defined by Athena today: Python and shell.  Node can be added when its
    worker is made a stable shared protocol; silently treating it as a shell
    would be a correctness and policy bug.
    """

    name = "container"
    _RUNTIME_ALIASES = {
        "python": "python",
        "python3": "python",
        "py": "python",
        "shell": "shell",
        "bash": "shell",
        "sh": "shell",
        "zsh": "shell",
    }

    def __init__(
        self,
        image: str = "python:3.13-slim",
        *,
        docker_command: str = "docker",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.image = image
        self.docker_command = docker_command
        self._runner = runner or subprocess.run
        self._sessions: dict[str, _ContainerSession] = {}
        self._tasks: dict[str, list[str]] = {}
        self._exec_sessions: dict[str, _ContainerSession] = {}

    def available(self) -> bool:
        """Return whether Docker can actually service a request."""
        if shutil.which(self.docker_command) is None:
            return False
        try:
            result = self._runner(
                [self.docker_command, "info", "--format", "{{.ServerVersion}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                env=self._docker_env(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and bool((result.stdout or "").strip())

    def environment_identity(self) -> dict[str, str]:
        """Return the image identity used by new container sessions."""
        image_ref, image_digest = self._resolve_image()
        return {"image": image_ref, "image_digest": image_digest}

    def _require(self) -> None:
        if not self.available():
            raise RuntimeError(
                "container execution unavailable: Docker CLI and a reachable "
                "Docker daemon are required"
            )

    @classmethod
    def _canonical_runtime(cls, runtime: str) -> str:
        canonical = cls._RUNTIME_ALIASES.get(runtime.casefold())
        if canonical is None:
            raise ValueError(
                f"container backend does not support runtime {runtime!r}; "
                "supported runtimes: python, shell"
            )
        return canonical

    @staticmethod
    def _validate_env(env: Mapping[str, str] | None) -> dict[str, str]:
        values = {str(key): str(value) for key, value in (env or {}).items()}
        for key, value in values.items():
            if not _ENV_NAME.fullmatch(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            if "\x00" in value:
                raise ValueError(f"environment variable {key!r} contains NUL")
        return values

    @staticmethod
    def _workspace_root(workspace_root: str | None) -> str:
        if not workspace_root:
            raise ValueError("container execution requires a workspace root")
        root = os.path.realpath(os.path.abspath(workspace_root))
        if not os.path.isdir(root):
            raise ValueError(f"container workspace root is not a directory: {root}")
        if "," in root:
            raise ValueError("container workspace paths may not contain commas")
        return root

    @staticmethod
    def _workspace_cwd(root: str, cwd: str | None) -> str:
        target = os.path.realpath(os.path.abspath(cwd or root))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError(f"container cwd is outside workspace: {cwd}")
        if not os.path.isdir(target):
            raise ValueError(f"container cwd is not a directory: {cwd}")
        suffix = target[len(root) :]
        return _WORKSPACE_MOUNT + suffix

    @staticmethod
    def _docker_env() -> dict[str, str]:
        # These variables select the Docker endpoint/context; they are not
        # copied wholesale because the host environment may contain secrets.
        keys = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
        return {key: os.environ[key] for key in keys if key in os.environ}

    def _run_docker(self, args: list[str], *, timeout: float = 30.0) -> str:
        result = self._runner(
            [self.docker_command, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=self._docker_env(),
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "docker command failed").strip()
            raise RuntimeError(error)
        return (result.stdout or "").strip()

    def _resolve_image(self) -> tuple[str, str]:
        """Resolve the configured image to an immutable local identity.

        A tag is only a lookup name: it can point at different content on a
        later run.  Inspect first, then pass the resolved repository digest
        (or the immutable image ID for locally-built images) to ``docker run``.
        This also prevents an execution from implicitly pulling a changed tag
        after its proof identity was established.
        """
        raw = self._run_docker(
            [
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                self.image,
            ]
        )
        try:
            record = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Docker returned invalid image metadata for {self.image!r}"
            ) from exc
        if not isinstance(record, dict):
            raise RuntimeError(f"Docker image metadata is not an object for {self.image!r}")

        repo_digests = record.get("RepoDigests")
        if isinstance(repo_digests, list):
            for value in repo_digests:
                candidate = str(value or "")
                match = re.search(r"@(?P<digest>sha256:[0-9a-f]{64})$", candidate)
                if match:
                    return candidate, match.group("digest")

        image_id = str(record.get("Id") or "")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            return image_id, image_id
        raise RuntimeError(
            f"container image {self.image!r} has no immutable digest; "
            "use a digest-pinned image or build a local image"
        )

    def _create_container(
        self,
        *,
        task_id: str,
        workspace_root: str,
        network_policy: NetworkPolicy | str | None,
        image_ref: str | None = None,
    ) -> str:
        policy = getattr(network_policy, "value", network_policy) or NetworkPolicy.DENY.value
        command = [
            "run",
            "--detach",
            "--rm",
            "--name",
            f"athena-{uuid.uuid4().hex[:16]}",
            "--label",
            "athena.task_id=" + task_id,
            "--label",
            "athena.backend=container",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            "--mount",
            f"type=bind,source={workspace_root},target={_WORKSPACE_MOUNT},readonly",
            "--workdir",
            _CONTAINER_CWD,
        ]
        if policy != NetworkPolicy.ALLOW.value:
            # Restricted currently has no allowlist representation at the
            # execution boundary, so it is fail-closed like denied network.
            command.extend(("--network", "none"))
        command.extend(
            (image_ref or self._resolve_image()[0], "sh", "-c", "while :; do sleep 3600; done")
        )
        container_id = self._run_docker(command)
        if not container_id:
            raise RuntimeError("Docker returned an empty container id")
        return container_id.splitlines()[-1].strip()

    def _exec_command(
        self,
        *,
        container_id: str,
        runtime: str,
        cwd: str,
        env: Mapping[str, str],
    ) -> list[str]:
        command = [
            self.docker_command,
            "exec",
            "-i",
            "--workdir",
            cwd,
        ]
        for key, value in env.items():
            command.extend(("--env", f"{key}={value}"))
        command.extend((container_id, "bash" if runtime == "shell" else "python"))
        if runtime == "shell":
            command.extend(("--norc", "--noprofile"))
        else:
            command.extend(("-u", "-c", _WORKER_SOURCE))
        return command

    def _make_session(
        self,
        *,
        session_id: str,
        task_id: str,
        runtime: str,
        cwd: str | None,
        env: Mapping[str, str] | None,
        workspace_root: str | None,
        network_policy: NetworkPolicy | str | None,
    ) -> _ContainerSession:
        self._require()
        canonical = self._canonical_runtime(runtime)
        root = self._workspace_root(workspace_root)
        container_cwd = self._workspace_cwd(root, cwd)
        values = self._validate_env(env)
        image_ref, image_digest = self._resolve_image()
        container_id = self._create_container(
            task_id=task_id,
            workspace_root=root,
            network_policy=network_policy,
            image_ref=image_ref,
        )
        command = self._exec_command(
            container_id=container_id,
            runtime=canonical,
            cwd=container_cwd,
            env=values,
        )
        try:
            worker: Any
            if canonical == "shell":
                worker = _SubprocessSession(
                    env={},
                    cwd=None,
                    start_cmd=command,
                    sandbox_root=None,
                    network_policy=None,
                )
            else:
                worker = _DockerPythonSession(command=command, env=values)
            worker.start()
        except Exception:
            self._remove_container(container_id)
            raise
        return _ContainerSession(
            session_id=session_id,
            task_id=task_id,
            runtime=canonical,
            container_id=container_id,
            cwd=container_cwd,
            env=values,
            worker=worker,
            workspace_root=root,
            image_ref=image_ref,
            image_digest=image_digest,
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
        session_id = f"container_{task_id}_{uuid.uuid4().hex[:10]}"
        session = await asyncio.to_thread(
            self._make_session,
            session_id=session_id,
            task_id=task_id,
            runtime=runtime,
            cwd=cwd,
            env=env,
            workspace_root=workspace_root,
            network_policy=network_policy,
        )
        self._sessions[session_id] = session
        self._tasks.setdefault(task_id, []).append(session_id)
        return session_id

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        execution_id = str(request.metadata.get("__execution_id") or uuid.uuid4().hex)
        session = self._sessions.get(request.runtime_session_id or "")
        if session is None and request.runtime_session_id:
            raise RuntimeError(f"unknown container runtime session: {request.runtime_session_id}")
        canonical = self._canonical_runtime(request.runtime)
        if session is None:
            for session_id in self._tasks.get(request.task_id, []):
                candidate = self._sessions.get(session_id)
                if candidate is not None and candidate.runtime == canonical:
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
        if session.runtime != canonical:
            raise RuntimeError("runtime does not match the container session")
        self._exec_sessions[execution_id] = session
        yield ExecutionEvent(
            type=ExecutionEventType.STARTED,
            execution_id=execution_id,
            metadata={
                "runtime_session_id": session.id,
                "container_id": session.container_id,
                "backend": self.name,
                "image": session.image_ref,
                "image_digest": session.image_digest,
            },
        )
        try:
            async for event in BaseRuntime._bridge_sync_generator(
                session.run(request, execution_id)
            ):
                yield event
        finally:
            self._exec_sessions.pop(execution_id, None)

    async def interrupt(self, execution_id: str) -> None:
        session = self._exec_sessions.get(execution_id)
        if session is not None:
            session.interrupt()

    async def destroy_session(self, runtime_session_id: str) -> None:
        session = self._sessions.pop(runtime_session_id, None)
        if session is None:
            return
        task_sessions = self._tasks.get(session.task_id, [])
        if runtime_session_id in task_sessions:
            task_sessions.remove(runtime_session_id)
        if not task_sessions:
            self._tasks.pop(session.task_id, None)
        session.close()
        await asyncio.to_thread(self._remove_container, session.container_id)

    def _remove_container(self, container_id: str) -> None:
        try:
            self._run_docker(["rm", "-f", container_id], timeout=15)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # Teardown is best effort.  The container was created with --rm;
            # the daemon will remove it when its init process exits.
            pass

    async def shutdown(self) -> None:
        for session_id in list(self._sessions):
            await self.destroy_session(session_id)


def available_backends() -> dict[str, bool]:
    """Return the backends that can be selected on this host."""
    return {"local": True, "container": ContainerBackend().available()}
