"""Governed SSH execution backend.

SSH profiles are operator-owned backend identities.  Requests select a
configured profile by name; they never supply a host, port, key, or SSH flag.
The backend uses strict known-host verification and the same persistent worker
protocols as local execution.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, AsyncIterator, Mapping

from athena.concurrency import run_blocking
from athena.execution.backend import BackendCapabilities, ExecutionBackend
from athena.execution.runtimes.base import BaseRuntime
from athena.execution.ssh_remote_runtime import _REMOTE_PYTHON_SUPERVISOR_V2
from athena.execution.ssh_profile import SSHProfile
from athena.execution.ssh_session import (
    _SSHNodeSession,  # noqa: F401 - compatibility import for existing backend fixtures
    _SSHPythonSession,  # noqa: F401 - compatibility import for backend fixtures
    _SSHSession,
)
from athena.execution.ssh_lifecycle import SSHSessionLifecycle
from athena.execution.ssh_transport import SSHTransport
from athena.execution.ssh_capabilities import ssh_capabilities
from athena.protocol.execution import ExecutionEvent, ExecutionEventType, ExecutionRequest
from athena.protocol.tasks import NetworkPolicy


__all__ = ["SSHBackend", "SSHProfile", "_REMOTE_PYTHON_SUPERVISOR_V2"]


class SSHBackend(ExecutionBackend):
    """Run persistent Python, shell, and Node workers over a fixed SSH profile."""
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
        self._transport = SSHTransport(
            profile, secret_manager=secret_manager, ssh_command=ssh_command
        )
        self._sessions: dict[str, _SSHSession] = {}
        self._tasks: dict[str, list[str]] = {}
        self._executions: dict[str, _SSHSession] = {}
        self._session_lifecycle = SSHSessionLifecycle(self.profile, self._transport)

    def capabilities(self) -> BackendCapabilities:
        return ssh_capabilities()

    def available(self) -> bool:
        return self._transport.available()

    def environment_identity(self) -> dict[str, str]:
        return {
            "host": self.profile.host,
            "user": self.profile.user,
            "port": str(self.profile.port),
            "known_hosts": os.path.realpath(os.path.expanduser(self.profile.known_hosts)),
        }

    def _ssh_base(self, key_path: str | None) -> list[str]:
        """Compatibility probe; transport owns command construction."""
        return self._transport.ssh_base(key_path)

    async def dependency_operation(
        self,
        *,
        task_id: str,
        manager: str,
        name: str,
        version: str = "",
        operation: str = "inventory",
        environment_id: str = "",
        expected: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Install or verify a remote dependency through the supervisor RPC."""
        if operation not in {"install", "inventory", "verify"}:
            raise ValueError("unsupported SSH dependency operation")
        key_path = await run_blocking(self._transport.credential_path, task_id, _pool="long")
        session_id = f"ssh_dependency_{task_id}_{secrets.token_hex(8)}"
        remote_cwd = self._transport.target_root(task_id)
        metadata: Mapping[str, str] | None = None
        control = _SSHSession(
            id=session_id,
            task_id=task_id,
            runtime="python",
            remote_cwd=remote_cwd,
            worker=None,
            host=self.profile.host,
            start_identity=session_id,
            key_path=key_path,
        )
        try:
            metadata = await run_blocking(
                self._transport.start_remote_python_supervisor,
                _pool="long",
                task_id=task_id,
                session_id=session_id,
                remote_cwd=remote_cwd,
                key_path=key_path,
                runtime="python",
            )
            control.remote_socket = metadata["socket_path"]
            control.remote_token_path = metadata["token_path"]
            control.remote_metadata_path = metadata.get("metadata_path")
            control.remote_controller_pid = metadata.get("pid")
            return await run_blocking(
                self._transport.remote_dependency,
                _pool="long",
                socket_path=metadata["socket_path"],
                token_path=metadata["token_path"],
                key_path=key_path,
                request={
                    "operation": operation,
                    "manager": manager,
                    "name": name,
                    "version": version,
                    "environment_id": environment_id,
                    "expected": dict(expected or {}),
                },
            )
        finally:
            if metadata is not None:
                try:
                    await run_blocking(self._transport.remote_shutdown, control, _pool="long")
                except Exception:  # noqa: BLE001 - preserve the primary RPC result
                    pass
            if self.profile.identity_file is None and key_path:
                try:
                    os.unlink(key_path)
                except FileNotFoundError:
                    pass

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
        return self._session_lifecycle.create(
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
        network_policy: NetworkPolicy | str | None = None,
    ) -> str:
        session = await run_blocking(
            self._make_session,
            _pool="long",
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
                "runtime_identity": session.runtime_identity,
                "worker_source_sha256": session.worker_source_sha256,
                "session_nonce": session.session_nonce,
                "authority_digest": session.authority_digest,
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
                await run_blocking(self._transport.remote_interrupt, session, _pool="long")
            else:
                session.interrupt()

    async def destroy_session(self, runtime_session_id: str) -> None:
        session = self._sessions.get(runtime_session_id)
        if session is None:
            return
        receipt: dict[str, Any] = {"confirmed": True}
        if session.remote_socket:
            receipt = await run_blocking(self._transport.remote_shutdown, session, _pool="long")
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
                    "remote_supervisor": "athena-controller-worker-v2",
                    "runtime_identity": session.runtime_identity or "",
                    "worker_source_sha256": session.worker_source_sha256 or "",
                    "session_nonce": session.session_nonce or "",
                    "authority_digest": session.authority_digest or "",
                }
            )
        return value

    async def reattach_session(self, record: Mapping[str, Any]) -> str:
        """Adopt a remote controller/worker supervisor after restart."""
        session = await self._session_lifecycle.reattach(record)
        self._sessions[session.id] = session
        self._tasks.setdefault(session.task_id, []).append(session.id)
        return session.id

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
