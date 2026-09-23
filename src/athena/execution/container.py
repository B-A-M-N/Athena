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

import shutil
import subprocess
import uuid
from collections.abc import Callable
from typing import Any, AsyncIterator, Mapping

from athena.concurrency import run_blocking
from athena.execution.backend import BackendCapabilities, ExecutionBackend
from athena.execution.container_lifecycle import ContainerSessionLifecycle
from athena.execution.container_session import (
    ContainerSession as _ContainerSession,
    DockerNodeSession as _DockerNodeSession,  # noqa: F401 - compatibility surface
    DockerPythonSession as _DockerPythonSession,  # noqa: F401 - compatibility surface
)
from athena.execution.container_transport import ContainerTransport
from athena.execution.runtimes.base import BaseRuntime
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionLimits,
    ExecutionRequest,
)
from athena.protocol.tasks import NetworkPolicy

__all__ = ["ContainerBackend", "available_backends"]


class ContainerBackend(ExecutionBackend):
    """Run Athena's persistent runtimes inside Docker."""

    name = "container"
    supports_reattach = True
    _RUNTIME_ALIASES = {
        "python": "python",
        "python3": "python",
        "py": "python",
        "shell": "shell",
        "bash": "shell",
        "sh": "shell",
        "zsh": "shell",
        "node": "node",
        "nodejs": "node",
        "javascript": "node",
        "js": "node",
    }

    def __init__(
        self,
        image: str = "python:3.13-slim",
        *,
        runtime_images: Mapping[str, str] | None = None,
        docker_command: str = "docker",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.image = image
        self.runtime_images = {
            "node": "node:22-slim",
            **{str(key).casefold(): str(value) for key, value in (runtime_images or {}).items()},
        }
        self.docker_command = docker_command
        self._transport = ContainerTransport(
            image=image,
            runtime_images=self.runtime_images,
            docker_command=docker_command,
            runner=runner or subprocess.run,
            which=shutil.which,
        )
        self._sessions: dict[str, _ContainerSession] = {}
        self._tasks: dict[str, list[str]] = {}
        self._exec_sessions: dict[str, _ContainerSession] = {}
        self._session_lifecycle = ContainerSessionLifecycle(
            require_available=lambda: self._require(),
            canonical_runtime=lambda runtime: self._canonical_runtime(runtime),
            workspace_root=lambda root: self._workspace_root(root),
            workspace_cwd=lambda root, cwd: self._workspace_cwd(root, cwd),
            validate_env=lambda env: self._validate_env(env),
            image_for_runtime=lambda runtime: self._image_for_runtime(runtime),
            resolve_image=self._resolve_image,
            create_container=self._create_container,
            inspect_container=lambda container_id: self._inspect_container(container_id),
            exec_command=self._exec_command,
            remove_container=self._remove_container,
        )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supported_runtimes=("node", "python", "shell"),
            persistent_sessions=True,
            persistent_runtime_state=True,
            reattach=True,
            filesystem_persistence=True,
            network_modes=("allow", "deny"),
            network_policy_effects={"allow": "allow", "deny": "deny", "restricted": "deny"},
            secret_materialization=True,
            interactive_stdin=True,
            process_signals=True,
            dependency_installation=("python", "node"),
            runtime_lifetime="container",
            runtime_capabilities={
                runtime: {
                    "filesystem_containment": True,
                    "network_containment": True,
                }
                for runtime in ("python", "shell")
            },
        )

    def available(self) -> bool:
        return self._transport.available()

    def environment_identity(self) -> dict[str, str]:
        """Return the image identity used by new container sessions."""
        return self._transport.environment_identity()

    def _image_for_runtime(self, runtime: str) -> str:
        return self._transport.image_for_runtime(self._canonical_runtime(runtime))

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
                "supported runtimes: node, python, shell"
            )
        return canonical

    @staticmethod
    def _validate_env(env: Mapping[str, str] | None) -> dict[str, str]:
        return ContainerTransport.validate_env(env)

    @staticmethod
    def _workspace_root(workspace_root: str | None) -> str:
        return ContainerTransport.workspace_root(workspace_root)

    @staticmethod
    def _workspace_cwd(root: str, cwd: str | None) -> str:
        return ContainerTransport.workspace_cwd(root, cwd)

    @staticmethod
    def _docker_env() -> dict[str, str]:
        return ContainerTransport._docker_env()

    def _run_docker(self, args: list[str], *, timeout: float = 30.0) -> str:
        return self._transport.run_docker(args, timeout=timeout)

    def _inspect_container(self, container_id: str) -> dict[str, Any]:
        return self._transport.inspect_container(container_id)

    def _resolve_image(self, image: str | None = None) -> tuple[str, str]:
        return self._transport.resolve_image(image)

    def _create_container(
        self,
        *,
        task_id: str,
        workspace_root: str,
        network_policy: NetworkPolicy | str | None,
        runtime: str = "python",
        env: Mapping[str, str] | None = None,
        image_ref: str | None = None,
        session_id: str | None = None,
        image_digest: str | None = None,
    ) -> str:
        return self._transport.create_container(
            task_id=task_id,
            workspace_root=workspace_root,
            network_policy=network_policy,
            runtime=self._canonical_runtime(runtime),
            env=env,
            image_ref=image_ref,
            session_id=session_id,
            image_digest=image_digest,
        )

    def _exec_command(
        self,
        *,
        container_id: str,
        runtime: str,
        cwd: str,
        env: Mapping[str, str],
    ) -> list[str]:
        return self._transport.exec_command(
            container_id=container_id,
            runtime=runtime,
            cwd=cwd,
            env=env,
        )

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
        return self._session_lifecycle.create(
            session_id=session_id,
            task_id=task_id,
            runtime=runtime,
            cwd=cwd,
            env=env,
            workspace_root=workspace_root,
            network_policy=network_policy,
        )

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        workspace_root: str | None = None,
        network_policy: str | None = None,
        resource_limits: ExecutionLimits | None = None,
    ) -> str:
        if resource_limits is not None:
            raise RuntimeError("container backend does not enforce resource limits")
        session_id = f"container_{task_id}_{uuid.uuid4().hex[:10]}"
        session = await run_blocking(
            self._make_session,
            _pool="long",
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

    def _reattach(self, record: Mapping[str, Any]) -> _ContainerSession:
        return self._session_lifecycle.reattach(record)

    async def describe_session(self, runtime_session_id: str) -> Mapping[str, str]:
        session = self._sessions.get(runtime_session_id)
        if session is None:
            raise RuntimeError(f"unknown container runtime session: {runtime_session_id}")
        return {
            "container_id": session.container_id,
            "process_identity": session.container_id,
            "start_identity": session.start_identity,
            "workspace_identity": session.workspace_root,
            "network_policy": session.network_policy,
            "runtime_version": session.image_digest,
            "image_digest": session.image_digest,
            "image_ref": session.image_ref,
        }

    async def reattach_session(self, record: Mapping[str, Any]) -> str:
        session = await run_blocking(self._reattach, record, _pool="long")
        self._sessions[session.id] = session
        self._tasks.setdefault(session.task_id, []).append(session.id)
        return session.id

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
                "start_identity": session.start_identity,
                "workspace_identity": session.workspace_root,
                "network_policy": session.network_policy,
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
        await run_blocking(self._remove_container, session.container_id, _pool="long")

    def _remove_container(self, container_id: str) -> None:
        self._transport.remove_container(container_id)

    async def shutdown(self) -> None:
        for session_id in list(self._sessions):
            await self.destroy_session(session_id)


def available_backends() -> dict[str, bool]:
    """Return the backends that can be selected on this host."""
    return {"local": True, "container": ContainerBackend().available()}
