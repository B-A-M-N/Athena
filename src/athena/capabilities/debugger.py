"""Governed Python debugging through the Debug Adapter Protocol (DAP).

The debuggee is launched by :class:`ExecutionManager`; this capability only
owns the localhost DAP client and session bookkeeping.  It never creates a
raw application subprocess, and a debug session is task-owned just like an
ordinary runtime session.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import socket
from typing import Any

from athena.protocol.capabilities import (
    Availability,
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    InvocationContext,
)
from athena.protocol.execution import ExecutionRequest
from athena.protocol.tasks import NetworkPolicy

_debugpy: Any = None
try:
    _debugpy = importlib.import_module("debugpy")
except ImportError:
    pass
debugpy: Any = _debugpy

_DEBUGGER_AVAILABILITY = Availability.AVAILABLE if debugpy is not None else Availability.UNAVAILABLE
_logger = logging.getLogger("athena.debugger")
_DAP_TIMEOUT = 15.0


def _result(request, ok=True, output="", error="", meta=None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=None if ok else error,
        metadata=dict(meta or {}),
    )


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


class _DAPClient:
    """Small asynchronous DAP client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._seq = 0
        self._lock = asyncio.Lock()
        self.events: list[dict[str, Any]] = []

    @classmethod
    async def connect(cls, port: int, *, timeout: float = _DAP_TIMEOUT) -> "_DAPClient":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
        return cls(reader, writer)

    async def _read_message(self) -> dict[str, Any]:
        length: int | None = None
        while True:
            line = await self.reader.readline()
            if not line:
                raise ConnectionError("DAP adapter closed the connection")
            decoded = line.decode("ascii", "replace").strip()
            if not decoded:
                break
            key, separator, value = decoded.partition(":")
            if separator and key.lower() == "content-length":
                length = int(value.strip())
        if length is None:
            raise ValueError("DAP response omitted Content-Length")
        try:
            payload = await self.reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("truncated DAP response") from exc
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("DAP response must be an object")
        return value

    async def request(
        self, command: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            self._seq += 1
            request_seq = self._seq
            payload = {
                "seq": request_seq,
                "type": "request",
                "command": command,
                "arguments": arguments or {},
            }
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.writer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
            self.writer.write(encoded)
            await self.writer.drain()
            while True:
                message = await asyncio.wait_for(self._read_message(), timeout=_DAP_TIMEOUT)
                if message.get("type") == "event":
                    self.events.append(message)
                    continue
                if message.get("type") != "response":
                    continue
                if message.get("request_seq") != request_seq:
                    continue
                if not message.get("success", False):
                    error = message.get("message") or f"DAP request failed: {command}"
                    raise RuntimeError(str(error))
                return message.get("body") or {}

    def close(self) -> None:
        try:
            self.writer.close()
        except (OSError, RuntimeError):
            pass


class DebuggerCapability:
    """Launch and inspect Python programs through a governed DAP session."""

    descriptor = CapabilityDescriptor(
        id="debugger",
        description=(
            "Launch Python scripts under debugpy and use the Debug Adapter "
            "Protocol to set breakpoints, continue, pause, inspect stack and "
            "variables, evaluate expressions, step, and detach. Operations: "
            "launch/breakpoint/continue/pause/stack/variables/evaluate/step/"
            "detach/status."
        ),
        input_schema={
            "type": "object",
            "required": ["operation"],
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "launch",
                        "breakpoint",
                        "continue",
                        "pause",
                        "stack",
                        "variables",
                        "evaluate",
                        "step",
                        "detach",
                        "status",
                    ],
                },
                "session": {"type": "string", "minLength": 1},
                "script": {"type": "string", "maxLength": 4096},
                "file": {"type": "string", "maxLength": 4096},
                "line": {"type": "integer", "minimum": 1},
                "condition": {"type": "string", "maxLength": 4096},
                "expression": {"type": "string", "maxLength": 4096},
                "frame_id": {"type": "integer", "minimum": 0},
                "variables_reference": {"type": "integer", "minimum": 0},
                "thread_id": {"type": "integer", "minimum": 0},
                "granularity": {"type": "string", "enum": ["over", "into", "out"]},
            },
        },
        effects=frozenset(
            {
                EffectClass.EXECUTE,
                EffectClass.SPAWN_PROCESS,
                EffectClass.READ_LOCAL,
                EffectClass.WRITE_LOCAL,
            }
        ),
        origin=CapabilityOrigin.NATIVE,
        availability=_DEBUGGER_AVAILABILITY,
    )

    def __init__(self, execution_manager=None, workspace=None) -> None:
        self._execution = execution_manager
        self._workspace = workspace
        self._sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def available() -> bool:
        return _DEBUGGER_AVAILABILITY is Availability.AVAILABLE

    def _own(self, request: CapabilityRequest) -> dict[str, Any] | None:
        sid = str((request.arguments or {}).get("session") or "")
        session = self._sessions.get(sid)
        if session is None or session["task_id"] != request.task_id:
            return None
        return session

    @staticmethod
    def _path_in_workspace(path: str, root: str) -> str | None:
        absolute = os.path.realpath(
            os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
        )
        if absolute != root and not absolute.startswith(root + os.sep):
            return None
        if not os.path.isfile(absolute):
            return None
        return absolute

    @staticmethod
    def _dap_path(path: str, root: str) -> str:
        return "/workspace" + path[len(root) :]

    async def _connect(self, port: int) -> _DAPClient:
        async def connect_and_initialize_async() -> _DAPClient:
            client = await _DAPClient.connect(port)
            try:
                await client.request(
                    "initialize",
                    {
                        "adapterID": "athena",
                        "clientID": "athena",
                        "linesStartAt1": True,
                        "columnsStartAt1": True,
                        "pathFormat": "path",
                    },
                )
                await client.request("attach", {"justMyCode": False})
                await client.request("configurationDone")
                return client
            except Exception:
                client.close()
                raise

        deadline = asyncio.get_running_loop().time() + _DAP_TIMEOUT
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await connect_and_initialize_async()
            except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        raise RuntimeError(f"debugpy DAP adapter did not become ready: {last_error}")

    @staticmethod
    async def _request(
        client: _DAPClient, command: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        value = client.request(command, arguments)
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _thread_id(args: dict[str, Any], session: dict[str, Any]) -> int:
        value = args.get("thread_id") or session.get("thread_id")
        if value is None:
            raise ValueError("thread_id is required after the debugger stops")
        return int(value)

    @staticmethod
    def _refresh_events(session: dict[str, Any]) -> None:
        client = session.get("client")
        if client is None:
            return
        for event in client.events[session.get("events_seen", 0) :]:
            session["events_seen"] = session.get("events_seen", 0) + 1
            body = event.get("body") or {}
            if event.get("event") == "stopped":
                session["paused"] = True
                session["thread_id"] = body.get("threadId")
            elif event.get("event") in {"continued", "running"}:
                session["paused"] = False
                session["thread_id"] = body.get("threadId", session.get("thread_id"))
            elif event.get("event") in {"terminated", "exited"}:
                session["terminated"] = True

    async def invoke(
        self,
        request: CapabilityRequest,
        *,
        context: InvocationContext | None = None,
        **kwargs,
    ) -> CapabilityResult:
        args = dict(request.arguments or {})
        op = str(args.get("operation") or "")
        task_id = request.task_id
        if task_id is None:
            return _result(request, ok=False, error="debugger requires task scope")
        if not self.available():
            return _result(
                request,
                ok=False,
                error="debugger unavailable: install athena-agent[debug]",
            )
        if self._execution is None:
            return _result(request, ok=False, error="debugger requires ExecutionManager")
        workspace = getattr(context, "workspace", None) or self._workspace
        backend = getattr(workspace, "execution_backend", None) or "local"

        if op == "launch":
            if workspace is None:
                return _result(
                    request, ok=False, error="debugger launch requires workspace context"
                )
            if backend not in {"local", "shadow", "sandbox", "verification"}:
                return _result(
                    request,
                    ok=False,
                    error="debugger launch does not support this execution backend",
                )
            if workspace.network_policy is not NetworkPolicy.ALLOW:
                return _result(
                    request,
                    ok=False,
                    error="debugger launch requires NetworkPolicy.ALLOW for its loopback DAP channel",
                )
            root = os.path.realpath(os.path.abspath(workspace.root))
            script = self._path_in_workspace(str(args.get("script") or ""), root)
            if script is None:
                return _result(
                    request, ok=False, error="script must be an existing file inside the workspace"
                )
            if not self._execution.has_runtime("python"):
                return _result(request, ok=False, error="python runtime unavailable")
            sid = f"dbg_{len(self._sessions) + 1}_{task_id}"
            port = _free_port()
            runtime_session_id = await self._execution.create_session(
                task_id=task_id,
                runtime="python",
                backend=backend,
                cwd=root,
                workspace_root=root,
                network_policy=workspace.network_policy.value,
            )
            exec_id = f"debug_exec_{sid}"
            namespace_script = self._dap_path(script, root)
            source = (
                "import debugpy, runpy\n"
                f"debugpy.listen(('127.0.0.1', {port}))\n"
                "debugpy.wait_for_client()\n"
                f"runpy.run_path({namespace_script!r}, run_name='__main__')\n"
            )
            execution_request = ExecutionRequest(
                runtime="python",
                source=source,
                task_id=task_id,
                workspace_id=workspace.id,
                backend=backend,
                runtime_session_id=runtime_session_id,
                cwd=root,
                network_policy=workspace.network_policy,
                workspace_root=root,
            )
            session: dict[str, Any] = {
                "task_id": task_id,
                "port": port,
                "session_id": sid,
                "runtime_session_id": runtime_session_id,
                "execution_id": exec_id,
                "script": script,
                "paused": False,
                "terminated": False,
                "breakpoints": {},
                "events_seen": 0,
                "result": None,
            }
            self._sessions[sid] = session

            async def run_debuggee() -> None:
                session["result"] = await self._execution.execute(
                    execution_request, execution_id=exec_id
                )
                session["terminated"] = True

            session["execution_task"] = asyncio.create_task(run_debuggee())
            try:
                session["client"] = await self._connect(port)
            except Exception as exc:
                await self._close_session(sid, session)
                return _result(request, ok=False, error=str(exc))
            return _result(
                request,
                output=f"debugger attached to {script}; session={sid}",
                meta={"session": sid, "port": port, "script": script},
            )

        owned_session = self._own(request)
        if owned_session is None:
            return _result(request, ok=False, error="unknown or unowned debugger session")
        session = owned_session
        self._refresh_events(session)
        client: _DAPClient | None = session.get("client")

        if op == "status":
            task = session.get("execution_task")
            return _result(
                request,
                output=json.dumps(
                    {
                        "session": session["session_id"],
                        "script": session["script"],
                        "paused": session["paused"],
                        "terminated": session["terminated"],
                        "execution_done": bool(task and task.done()),
                        "breakpoints": session["breakpoints"],
                    },
                    sort_keys=True,
                ),
            )
        if client is None:
            return _result(request, ok=False, error="debugger DAP session is not connected")

        try:
            if op == "breakpoint":
                workspace_root = os.path.realpath(
                    os.path.abspath(workspace.root if workspace else "")
                )
                file_path = self._path_in_workspace(str(args.get("file") or ""), workspace_root)
                if file_path is None:
                    return _result(
                        request, ok=False, error="breakpoint file must be inside the workspace"
                    )
                line = int(args.get("line") or 0)
                if line <= 0:
                    return _result(request, ok=False, error="line must be positive")
                dap_file = self._dap_path(file_path, workspace_root)
                points = session["breakpoints"].setdefault(dap_file, [])
                point: dict[str, Any] = {"line": line}
                if args.get("condition"):
                    point["condition"] = str(args["condition"])
                if point not in points:
                    points.append(point)
                body = await self._request(
                    client,
                    "setBreakpoints",
                    {
                        "source": {"path": dap_file},
                        "breakpoints": points,
                    },
                )
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)

            if op == "continue":
                body = await self._request(
                    client,
                    "continue",
                    {
                        "threadId": self._thread_id(args, session),
                    },
                )
                self._refresh_events(session)
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)
            if op == "pause":
                body = await self._request(
                    client,
                    "pause",
                    {
                        "threadId": self._thread_id(args, session),
                    },
                )
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)
            if op == "stack":
                body = await self._request(
                    client,
                    "stackTrace",
                    {
                        "threadId": self._thread_id(args, session),
                    },
                )
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)
            if op == "variables":
                reference = int(args.get("variables_reference") or 0)
                if reference <= 0:
                    return _result(request, ok=False, error="variables_reference is required")
                body = await self._request(
                    client,
                    "variables",
                    {
                        "variablesReference": reference,
                    },
                )
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)
            if op == "evaluate":
                expression = str(args.get("expression") or "")
                if not expression:
                    return _result(request, ok=False, error="expression is required")
                arguments: dict[str, Any] = {"expression": expression, "context": "repl"}
                if args.get("frame_id") is not None:
                    arguments["frameId"] = int(args["frame_id"])
                body = await self._request(client, "evaluate", arguments)
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)
            if op == "step":
                command = {"over": "next", "into": "stepIn", "out": "stepOut"}.get(
                    str(args.get("granularity") or "over")
                )
                if command is None:
                    return _result(
                        request, ok=False, error="granularity must be over, into, or out"
                    )
                body = await self._request(
                    client,
                    command,
                    {
                        "threadId": self._thread_id(args, session),
                    },
                )
                return _result(request, output=json.dumps(body, sort_keys=True), meta=body)
            if op == "detach":
                await self._request(client, "disconnect", {"terminateDebuggee": True})
                await self._close_session(session["session_id"], session)
                return _result(request, output=f"detached {session['session_id']}")
            return _result(request, ok=False, error=f"unknown debugger operation: {op}")
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            return _result(request, ok=False, error=f"DAP {op} failed: {exc}")

    async def _close_session(self, sid: str, session: dict[str, Any]) -> None:
        client = session.get("client")
        if client is not None:
            client.close()
        execution_task = session.get("execution_task")
        if execution_task is not None and not execution_task.done():
            execution_task.cancel()
        if self._execution is not None:
            try:
                await self._execution.interrupt(session["execution_id"])
                await self._execution.destroy_session(session["runtime_session_id"])
            except Exception as exc:
                _logger.debug("debugger session cleanup failed: %s", exc)
        self._sessions.pop(sid, None)

    def close_all(self) -> None:
        """Schedule governed cleanup without making shutdown hooks sync-only."""
        sessions = list(self._sessions.items())
        for sid, session in sessions:
            client = session.get("client")
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    _logger.debug("debugger DAP cleanup failed: %s", exc)
            task = session.get("execution_task")
            if task is not None and not task.done():
                task.cancel()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and self._execution is not None:
                loop.create_task(self._execution.destroy_session(session["runtime_session_id"]))
            self._sessions.pop(sid, None)


__all__ = ["DebuggerCapability"]
