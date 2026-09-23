"""Small local runtime supervisor used for process-surviving reattachment.

The host owns only runtime processes and a Unix-socket protocol. It does not
parse model input, make policy decisions, or hold Athena authority. The
client-side backend proves ownership with a private token and a persisted
session identity before adopting a session after an Athena restart.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from athena.execution.backend import BackendCapabilities, ExecutionBackend
from athena.execution.process_tree import process_start_identity
from athena.execution.runtimes import NodeRuntime, PythonRuntime, ShellRuntime
from athena.protocol.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionExitStatus,
    ExecutionRequest,
)

__all__ = ["LocalRuntimeSupervisor", "SupervisedLocalBackend"]


@dataclass(frozen=True)
class _HostSession:
    runtime: Any
    underlying_id: str
    runtime_name: str
    task_id: str
    cwd: str | None
    env: Mapping[str, str]
    workspace_root: str | None
    network_policy: str | None
    resource_limits: Mapping[str, Any] | None


def _event_record(event: ExecutionEvent) -> dict[str, Any]:
    return {
        "type": event.type.value,
        "execution_id": event.execution_id,
        "data": event.data,
        "exit_status": event.exit_status.value if event.exit_status else None,
        "exit_code": event.exit_code,
        "duration_ms": event.duration_ms,
        "metadata": dict(event.metadata or {}),
    }


def _event_from_record(value: Mapping[str, Any]) -> ExecutionEvent:
    return ExecutionEvent(
        type=ExecutionEventType(str(value["type"])),
        execution_id=str(value.get("execution_id") or ""),
        data=value.get("data"),
        exit_status=(
            ExecutionExitStatus(str(value["exit_status"])) if value.get("exit_status") else None
        ),
        exit_code=value.get("exit_code"),
        duration_ms=value.get("duration_ms"),
        metadata=dict(value.get("metadata") or {}),
    )


def _request_record(request: ExecutionRequest) -> dict[str, Any]:
    value = asdict(request)
    value["persistence"] = request.persistence.value
    value["network_policy"] = (
        request.network_policy.value if request.network_policy is not None else None
    )
    value["timeout"] = request.timeout.total_seconds() if request.timeout is not None else None
    value["stdin"] = base64.b64encode(request.stdin).decode() if request.stdin is not None else None
    value["resource_limits"] = asdict(request.resource_limits) if request.resource_limits else None
    return value


class _RuntimeHost:
    def __init__(self, socket_path: str, token_file: str) -> None:
        self.socket_path = Path(socket_path)
        self.token_file = Path(token_file)
        self.token = self.token_file.read_text(encoding="utf-8").strip()
        if len(self.token) < 32:
            raise RuntimeError("runtime host token is too short")
        self.runtimes: dict[str, Any] = {
            "python": PythonRuntime(),
            "shell": ShellRuntime(),
        }
        if NodeRuntime is not None and NodeRuntime.available():
            self.runtimes["node"] = NodeRuntime()
        self.sessions: dict[str, _HostSession] = {}
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self._client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        async with server:
            await self._stop_event.wait()
            server.close()
            await server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                request = json.loads(line)
                if request.get("token") != self.token:
                    raise PermissionError("runtime host authentication failed")
                response = await self._dispatch(request, writer)
                if response is not None:
                    writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        except Exception as exc:  # rationale: protocol errors are scoped to one client
            try:
                writer.write((json.dumps({"kind": "error", "error": str(exc)}) + "\n").encode())
                await writer.drain()
            except Exception:  # rationale: a disconnected client cannot receive protocol error output
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: Mapping[str, Any], writer) -> dict[str, Any] | None:
        operation = str(request.get("op") or "")
        if operation == "create":
            runtime_name = str(request.get("runtime") or "")
            runtime = self.runtimes.get(runtime_name)
            if runtime is None:
                raise ValueError(f"runtime unavailable: {runtime_name}")
            session_id = str(request.get("session_id") or "")
            if not session_id or session_id in self.sessions:
                raise ValueError("runtime session id is already owned")
            underlying = await runtime.create_session(
                task_id=str(request["task_id"]),
                backend="local",
                cwd=request.get("cwd"),
                env=dict(request.get("env") or {}),
                workspace_root=request.get("workspace_root"),
                network_policy=request.get("network_policy"),
                resource_limits=(
                    _decode_request(request).resource_limits
                    if request.get("resource_limits")
                    else None
                ),
            )
            self.sessions[session_id] = _HostSession(
                runtime=runtime,
                underlying_id=str(underlying),
                runtime_name=runtime_name,
                task_id=str(request.get("task_id") or ""),
                cwd=request.get("cwd"),
                env=dict(request.get("env") or {}),
                workspace_root=request.get("workspace_root"),
                network_policy=request.get("network_policy"),
                resource_limits=dict(request.get("resource_limits") or {}) or None,
            )
            return {"kind": "response", "session_id": session_id}
        if operation == "ping":
            return {
                "kind": "response",
                "ready": True,
                "pid": os.getpid(),
                "sessions": len(self.sessions),
            }
        if operation == "shutdown":
            self._stop_event.set()
            return {"kind": "response", "stopping": True}
        if operation == "describe":
            record = self._session(str(request["session_id"]), request.get("task_id"))
            runtime, underlying, runtime_name, task_id = (
                record.runtime,
                record.underlying_id,
                record.runtime_name,
                record.task_id,
            )
            session = getattr(runtime, "_sessions", {}).get(underlying)
            process = getattr(session, "process", None)
            pid = getattr(process, "pid", None)
            start_identity = process_start_identity(pid) if pid else None
            return {
                "kind": "response",
                "session_id": str(request["session_id"]),
                "runtime": runtime_name,
                "task_id": task_id,
                "cwd": getattr(session, "cwd", None),
                "workspace_identity": getattr(session, "sandbox_root", None),
                "network_policy": getattr(session, "network_policy", None),
                "pid": pid,
                "start_identity": start_identity,
                "process_identity": f"{pid}:{start_identity}" if pid else None,
            }
        if operation == "close":
            record = self._session(str(request["session_id"]), request.get("task_id"))
            runtime, underlying = record.runtime, record.underlying_id
            await runtime.close(underlying)
            self.sessions.pop(str(request["session_id"]), None)
            return {"kind": "response", "closed": True}
        if operation == "interrupt":
            record = self._session(str(request["session_id"]), request.get("task_id"))
            runtime = record.runtime
            await runtime.interrupt(str(request["execution_id"]))
            return {"kind": "response", "interrupted": True}
        if operation == "execute":
            session_id = str(request["session_id"])
            wire_request = _decode_request(dict(request["request"]))
            record = self._session(session_id, wire_request.task_id)
            if wire_request.runtime != record.runtime_name:
                raise PermissionError("runtime identity cannot be changed for an existing session")
            if wire_request.cwd is not None and wire_request.cwd != record.cwd:
                raise PermissionError("working directory cannot be changed for an existing session")
            if wire_request.env and dict(wire_request.env) != dict(record.env):
                raise PermissionError("environment cannot be changed for an existing session")
            if wire_request.workspace_root is not None and wire_request.workspace_root != record.workspace_root:
                raise PermissionError("workspace identity cannot be changed for an existing session")
            requested_network = getattr(wire_request.network_policy, "value", wire_request.network_policy)
            if requested_network is not None and requested_network != record.network_policy:
                raise PermissionError("network policy cannot be changed for an existing session")
            requested_limits = (
                asdict(wire_request.resource_limits) if wire_request.resource_limits else None
            )
            if requested_limits is not None and requested_limits != record.resource_limits:
                raise PermissionError("resource limits cannot be changed for an existing session")
            runtime, underlying = record.runtime, record.underlying_id
            wire_request = replace(wire_request, runtime_session_id=underlying, backend="local")
            async for event in runtime.execute(wire_request, str(request["execution_id"])):
                writer.write(
                    (json.dumps({"kind": "event", "event": _event_record(event)}) + "\n").encode()
                )
                await writer.drain()
            return {"kind": "done"}
        raise ValueError(f"unknown runtime host operation: {operation}")

    def _session(self, session_id: str, task_id: object = None) -> _HostSession:
        value = self.sessions.get(session_id)
        if value is None:
            raise KeyError(f"unknown runtime session: {session_id}")
        if task_id is not None and str(task_id) != value.task_id:
            raise PermissionError(
                f"runtime session {session_id} belongs to task {value.task_id}, not {task_id}"
            )
        return value


def _decode_request(value: Mapping[str, Any]) -> ExecutionRequest:
    from datetime import timedelta
    from athena.protocol.execution import ExecutionLimits, RuntimePersistence
    from athena.protocol.tasks import NetworkPolicy

    return ExecutionRequest(
        runtime=str(value["runtime"]),
        source=str(value.get("source") or ""),
        task_id=str(value["task_id"]),
        workspace_id=str(value.get("workspace_id") or ""),
        backend=str(value.get("backend") or "local"),
        runtime_session_id=value.get("runtime_session_id"),
        persistence=RuntimePersistence(str(value.get("persistence") or "ephemeral")),
        cwd=value.get("cwd"),
        env=dict(value.get("env") or {}),
        stdin=(base64.b64decode(value["stdin"]) if value.get("stdin") else None),
        timeout=(timedelta(seconds=float(value["timeout"])) if value.get("timeout") else None),
        network_policy=(
            NetworkPolicy(str(value["network_policy"])) if value.get("network_policy") else None
        ),
        workspace_root=value.get("workspace_root"),
        writable_paths=tuple(value["writable_paths"])
        if value.get("writable_paths") is not None
        else None,
        read_only_paths=tuple(value.get("read_only_paths") or ()),
        toolchain_paths=tuple(value.get("toolchain_paths") or ()),
        writable_toolchain_paths=tuple(value.get("writable_toolchain_paths") or ()),
        resource_limits=(
            ExecutionLimits(**value["resource_limits"]) if value.get("resource_limits") else None
        ),
        metadata=dict(value.get("metadata") or {}),
    )


class LocalRuntimeSupervisor:
    """Lifecycle wrapper for the external local runtime host."""

    def __init__(self, root: str) -> None:
        root_path = Path(root).expanduser().resolve()
        root_path.mkdir(parents=True, exist_ok=True)
        self.socket_path = root_path / "runtime-host.sock"
        self.token_file = root_path / "runtime-host.token"
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            token = ""
        self.token = token if len(token) >= 32 else secrets.token_urlsafe(32)
        self.process: asyncio.subprocess.Process | None = None
        self._adopted = False

    async def _control(self, operation: str) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(
                (
                    json.dumps({"op": operation, "token": self.token}, separators=(",", ":")) + "\n"
                ).encode()
            )
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise RuntimeError("runtime host closed the control connection")
            response = json.loads(line)
            if response.get("kind") == "error":
                raise RuntimeError(str(response.get("error") or "runtime host error"))
            return response
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if self.socket_path.exists():
            try:
                response = await self._control("ping")
                if response.get("ready") is True:
                    self._adopted = True
                    return
            except (ConnectionError, OSError, RuntimeError, ValueError):
                # A stale socket is safe to replace because the host protocol
                # is authenticated and the path is private to this supervisor.
                try:
                    self.socket_path.unlink()
                except FileNotFoundError:
                    pass
        self.token_file.write_text(self.token, encoding="utf-8")
        os.chmod(self.token_file, 0o600)
        self._adopted = False
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "athena.execution.runtime_host",
            "--socket",
            str(self.socket_path),
            "--token-file",
            str(self.token_file),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(100):
            if self.socket_path.exists():
                try:
                    response = await self._control("ping")
                    if response.get("ready") is True:
                        return
                except (ConnectionError, OSError, RuntimeError, ValueError):
                    pass
            await asyncio.sleep(0.02)
        if self.process.returncode is not None:
            details = ""
            if self.process.stderr is not None:
                details = (await self.process.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(
                f"local runtime host exited before readiness (code {self.process.returncode})"
                + (f": {details[-500:]}" if details else "")
            )
        raise RuntimeError("local runtime host did not create its socket")

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            if self._adopted:
                try:
                    await self._control("shutdown")
                except (ConnectionError, OSError, RuntimeError, ValueError):
                    pass
                self._adopted = False
            return
        if process.returncode is not None:
            await process.wait()
            self._adopted = False
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        self._adopted = False


class SupervisedLocalBackend(ExecutionBackend):
    name = "local-supervised"
    supports_reattach = True

    def __init__(self, supervisor: LocalRuntimeSupervisor) -> None:
        self.supervisor = supervisor
        self._sessions: dict[str, str] = {}
        self._execution_sessions: dict[str, str] = {}

    def capabilities(self) -> BackendCapabilities:
        runtimes = ["python", "shell"]
        if NodeRuntime is not None and NodeRuntime.available():
            runtimes.append("node")
        return BackendCapabilities(
            supported_runtimes=tuple(runtimes),
            persistent_sessions=True,
            persistent_runtime_state=True,
            reattach=True,
            filesystem_persistence=True,
            network_modes=("allow", "deny", "restricted"),
            resource_limits=True,
            secret_materialization=True,
            interactive_stdin=True,
            process_signals=True,
            dependency_installation=("python", "node"),
            runtime_capabilities={
                runtime: {
                    "filesystem_containment": True,
                    "network_containment": True,
                }
                for runtime in ("python", "shell")
            },
        )

    async def create_session(
        self,
        *,
        task_id,
        runtime,
        cwd=None,
        env=None,
        workspace_root=None,
        network_policy=None,
        resource_limits=None,
    ):
        await self.supervisor.start()
        session_id = f"local-host:{task_id}:{runtime}:{secrets.token_hex(8)}"
        await self._request(
            {
                "op": "create",
                "session_id": session_id,
                "task_id": task_id,
                "runtime": runtime,
                "cwd": cwd,
                "env": dict(env or {}),
                "workspace_root": workspace_root,
                "network_policy": getattr(network_policy, "value", network_policy),
                "resource_limits": asdict(resource_limits) if resource_limits else None,
            }
        )
        self._sessions[session_id] = str(runtime)
        return session_id

    async def _request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(str(self.supervisor.socket_path))
        try:
            writer.write((json.dumps({**payload, "token": self.supervisor.token}) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            if response.get("kind") == "error":
                raise RuntimeError(str(response.get("error") or "runtime host error"))
            return response
        finally:
            writer.close()
            await writer.wait_closed()

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[ExecutionEvent]:
        await self.supervisor.start()
        session_id = str(request.runtime_session_id or "")
        if not session_id:
            session_id = await self.create_session(
                task_id=request.task_id,
                runtime=request.runtime,
                cwd=request.cwd,
                env=request.env,
                workspace_root=request.workspace_root,
                network_policy=request.network_policy,
                resource_limits=request.resource_limits,
            )
            request = replace(request, runtime_session_id=session_id)
        elif session_id not in self._sessions:
            # A session can be reconstructed from durable state during startup;
            # unknown ids must still be rejected by the authenticated host.
            await self.describe_session(session_id)
            self._sessions.setdefault(session_id, str(request.runtime))
        execution_id = str(request.metadata.get("__execution_id") or secrets.token_hex(12))
        self._execution_sessions[execution_id] = session_id
        reader, writer = await asyncio.open_unix_connection(str(self.supervisor.socket_path))
        try:
            payload = {
                "op": "execute",
                "session_id": session_id,
                "execution_id": execution_id,
                "request": _request_record(request),
                "token": self.supervisor.token,
            }
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    raise RuntimeError("runtime host closed execution stream")
                value = json.loads(line)
                if value.get("kind") == "event":
                    event = _event_from_record(value["event"])
                    yield replace(
                        event,
                        metadata={
                            **dict(event.metadata or {}),
                            "runtime_session_id": session_id,
                            "backend": self.name,
                        },
                    )
                elif value.get("kind") == "done":
                    return
                elif value.get("kind") == "error":
                    raise RuntimeError(str(value.get("error") or "runtime host error"))
        finally:
            self._execution_sessions.pop(execution_id, None)
            writer.close()
            await writer.wait_closed()

    async def interrupt(self, execution_id: str) -> None:
        session_id = self._execution_sessions.get(execution_id)
        if session_id is not None:
            await self._request(
                {"op": "interrupt", "session_id": session_id, "execution_id": execution_id}
            )

    async def destroy_session(self, runtime_session_id: str) -> None:
        await self._request({"op": "close", "session_id": runtime_session_id})
        self._sessions.pop(runtime_session_id, None)

    async def describe_session(self, runtime_session_id: str) -> Mapping[str, Any]:
        response = await self._request({"op": "describe", "session_id": runtime_session_id})
        return {
            **response,
            "host_socket": str(self.supervisor.socket_path),
            "host_token_fingerprint": hashlib.sha256(
                self.supervisor.token.encode("utf-8")
            ).hexdigest(),
        }

    async def reattach_session(self, record: Mapping[str, Any]) -> str:
        session_id = str(record.get("id") or "")
        if not session_id:
            raise ValueError("runtime session record has no id")
        await self.supervisor.start()
        raw_metadata = record.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        expected_socket = str(metadata.get("host_socket") or record.get("host_socket") or "")
        if expected_socket and expected_socket != str(self.supervisor.socket_path):
            raise RuntimeError("runtime host socket identity mismatch")
        expected_token = str(
            metadata.get("host_token_fingerprint") or record.get("host_token_fingerprint") or ""
        )
        if expected_token:
            actual_token = hashlib.sha256(self.supervisor.token.encode("utf-8")).hexdigest()
            if expected_token != actual_token:
                raise RuntimeError("runtime host token identity mismatch")
        described = await self.describe_session(session_id)
        if str(described.get("session_id")) != session_id:
            raise RuntimeError("runtime host returned a different session identity")
        if record.get("runtime") and str(described.get("runtime")) != str(record["runtime"]):
            raise RuntimeError("runtime identity proof failed")
        if record.get("task_id") and str(described.get("task_id")) != str(record["task_id"]):
            raise RuntimeError("runtime task ownership proof failed")
        raw_process_identity = record.get("process_identity") or metadata.get("process_identity")
        if raw_process_identity and str(described.get("process_identity")) != str(
            raw_process_identity
        ):
            raise RuntimeError("runtime process identity proof failed")
        raw_start_identity = record.get("start_identity") or metadata.get("start_identity")
        if raw_start_identity and str(described.get("start_identity")) != str(raw_start_identity):
            raise RuntimeError("runtime start identity proof failed")
        raw_network_policy = record.get("network_policy") or metadata.get("network_policy")
        if raw_network_policy and str(described.get("network_policy")) != str(raw_network_policy):
            raise RuntimeError("runtime network policy proof failed")
        raw_workspace = record.get("workspace_identity") or metadata.get("workspace_identity")
        if raw_workspace and str(described.get("workspace_identity")) != str(raw_workspace):
            raise RuntimeError("runtime workspace identity proof failed")
        self._sessions[session_id] = str(described.get("runtime") or record.get("runtime") or "")
        return session_id

    async def shutdown(self) -> None:
        for session_id in list(self._sessions):
            try:
                await self.destroy_session(session_id)
            except (ConnectionError, OSError, RuntimeError, ValueError):
                self._sessions.pop(session_id, None)
        self._execution_sessions.clear()
        await self.supervisor.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Athena local runtime host")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    asyncio.run(_RuntimeHost(args.socket, args.token_file).run())


if __name__ == "__main__":  # pragma: no cover
    main()
