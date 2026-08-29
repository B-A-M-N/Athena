"""Persistent external delegate sessions with governed host calls."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from athena.delegates.models import DelegateSession, DelegateSpec
from athena.delegates.registry import DelegateRegistry
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
    DispatchDirectives,
    EffectClass,
)
from athena.protocol.ids import new_id
from athena.protocol.tasks import CapabilityPolicy, ResourceBudget, WorkspaceSpec

_logger = logging.getLogger("athena.delegates")


class DelegateTransport(Protocol):
    async def request(self, payload: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]: ...
    async def close(self) -> None: ...


class ExternalDelegateManager:
    """Own session lifetime; remote agents never receive Athena internals."""

    def __init__(self, registry: DelegateRegistry, store, *, dispatcher=None) -> None:
        self._registry = registry
        self._store = store
        self._dispatcher = dispatcher
        self._transports: dict[str, DelegateTransport] = {}

    def bind_dispatcher(self, dispatcher) -> None:
        self._dispatcher = dispatcher

    async def list(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        return [session.to_record() for session in await self._store.list(task_id=task_id)]

    async def start(
        self,
        delegate_id: str,
        *,
        task_id: str,
        session_id: str | None,
        workspace: WorkspaceSpec,
        context: tuple[Mapping[str, Any], ...] = (),
    ) -> DelegateSession:
        spec = self._registry.get(delegate_id)
        _check_workspace(spec, workspace)
        session = DelegateSession(
            id=new_id("delegate-session"),
            delegate_id=delegate_id,
            task_id=task_id,
            session_id=session_id,
            remote_session_id=None,
            workspace_root=os.path.realpath(workspace.root),
            state="starting",
            created_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
            launch_signature=_launch_signature(spec),
        )
        await self._store.save(session)
        transport = await self._connect(spec, session, workspace)
        self._transports[session.id] = transport
        response = await transport.request(
            {
                "type": "session.start",
                "objective": "",
                "context": list(context),
            },
            timeout=spec.timeout_seconds,
        )
        remote_id = (
            str(response.get("session_id") or response.get("remote_session_id") or "") or None
        )
        active = DelegateSession(
            **{
                **session.__dict__,
                "remote_session_id": remote_id,
                "state": "active",
                "last_seen_at": datetime.now(timezone.utc),
            }
        )
        await self._store.save(active)
        return active

    async def send(
        self,
        session_id: str,
        *,
        task_id: str,
        objective: str,
        workspace: WorkspaceSpec,
        context: tuple[Mapping[str, Any], ...] = (),
        task_policy: CapabilityPolicy | None = None,
        task_budget: ResourceBudget | None = None,
    ) -> Mapping[str, Any]:
        session = await self._store.get(session_id, task_id=task_id)
        if session is None:
            raise KeyError("delegate session not found or not owned by task")
        if session.state not in {"active", "starting", "interrupted"}:
            raise ValueError(f"delegate session is {session.state}")
        spec = self._registry.get(session.delegate_id)
        _check_workspace(spec, workspace)
        transport = self._transports.get(session_id)
        if transport is None:
            transport = await self._connect(spec, session, workspace)
            self._transports[session_id] = transport
            if session.remote_session_id:
                resumed = await transport.request(
                    {
                        "type": "session.resume",
                        "session_id": session.remote_session_id,
                    },
                    timeout=spec.timeout_seconds,
                )
                if resumed.get("ok", True) is False:
                    raise RuntimeError(
                        str(resumed.get("error") or "delegate session resume failed")
                    )
        payload = {
            "type": "session.message",
            "session_id": session.remote_session_id,
            "objective": objective,
            "context": list(context),
        }
        response = await self._request_with_host_calls(
            transport,
            payload,
            spec=spec,
            session=session,
            workspace=workspace,
            task_policy=task_policy,
            task_budget=task_budget,
        )
        await self._store.update_state(session_id, "active", task_id=task_id)
        return response

    async def status(self, session_id: str, *, task_id: str) -> dict[str, Any]:
        session = await self._store.get(session_id, task_id=task_id)
        if session is None:
            raise KeyError("delegate session not found or not owned by task")
        return session.to_record()

    async def close(self, session_id: str, *, task_id: str) -> bool:
        session = await self._store.get(session_id, task_id=task_id)
        if session is None:
            raise KeyError("delegate session not found or not owned by task")
        transport = self._transports.pop(session_id, None)
        try:
            if transport is not None:
                await transport.close()
        finally:
            # The durable session is no longer resumable by this task after an
            # explicit close, even if a connector reports a teardown error.
            await self._store.update_state(session_id, "closed", task_id=task_id)
        return True

    async def close_task(self, task_id: str) -> int:
        """Close every local transport owned by a terminal task."""
        sessions = await self._store.list(task_id=task_id)
        closed = 0
        for session in sessions:
            if session.state == "closed":
                self._transports.pop(session.id, None)
                continue
            try:
                await self.close(session.id, task_id=task_id)
                closed += 1
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                # Task cleanup must keep walking the other sessions.  The
                # transport was removed before close(), and the durable state
                # is marked closed in close()'s finally block when possible.
                _logger.warning(
                    "delegate session %s cleanup failed: %s",
                    session.id,
                    exc,
                )
        return closed

    async def close_all(self) -> int:
        """Close all local transports during service shutdown."""
        sessions = await self._store.list(task_id=None)
        closed = 0
        for session in sessions:
            if session.state == "closed":
                self._transports.pop(session.id, None)
                continue
            try:
                await self.close(session.id, task_id=session.task_id)
                closed += 1
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                _logger.warning(
                    "delegate session %s shutdown failed: %s",
                    session.id,
                    exc,
                )
        # A connector can disappear from durable storage between list() and
        # close(); never leave an in-memory transport alive in that case.
        self._transports.clear()
        return closed

    async def _connect(
        self, spec: DelegateSpec, session: DelegateSession, workspace: WorkspaceSpec
    ) -> DelegateTransport:
        connector = self._registry.connector_for(spec.id)
        if connector is None:
            if spec.command:
                return await _SubprocessTransport.start(spec, workspace)
            raise RuntimeError(
                f"delegate {spec.id!r} has no trusted transport connector; "
                "register an ACP/A2A/OpenAI connector with the host"
            )
        value = connector(spec=spec, session=session, workspace=workspace)
        if inspect.isawaitable(value):
            value = await value
        if not hasattr(value, "request") or not hasattr(value, "close"):
            raise TypeError("delegate connector must return request/close transport")
        return value

    async def _request_with_host_calls(
        self,
        transport: DelegateTransport,
        payload: Mapping[str, Any],
        *,
        spec: DelegateSpec,
        session: DelegateSession,
        workspace: WorkspaceSpec,
        task_policy: CapabilityPolicy | None,
        task_budget: ResourceBudget | None,
    ) -> Mapping[str, Any]:
        # A transport can implement a multi-message protocol by exposing
        # request_host_call. The default request path remains one response.
        requester = getattr(transport, "request_host_call", None)
        if requester is None:
            return await transport.request(payload, timeout=spec.timeout_seconds)

        async def host_call(item: Mapping[str, Any]) -> Mapping[str, Any]:
            capability_id = str(item.get("capability_id") or "")
            if spec.capability_ceiling and capability_id not in spec.capability_ceiling:
                return {"ok": False, "error": "delegate capability ceiling denied request"}
            request = CapabilityRequest(
                capability_id=capability_id,
                arguments=dict(item.get("arguments") or {}),
                task_id=session.task_id,
                session_id=session.session_id,
                call_id=str(item.get("call_id") or new_id("remote-call")),
                origin=CapabilityRequestOrigin.REMOTE,
            )
            if self._dispatcher is None:
                return {"ok": False, "error": "dispatcher unavailable"}
            effect_error = self._effect_ceiling_error(
                spec,
                request,
                workspace,
            )
            if effect_error is not None:
                return {"ok": False, "error": effect_error}
            result = await self._dispatcher.dispatch(
                request,
                workspace=workspace,
                task_policy=task_policy,
                task_budget=task_budget,
                _directives=DispatchDirectives(),
            )
            return {
                "ok": result.status.value == "ok",
                "output": result.output,
                "error": result.error,
                "metadata": dict(result.metadata or {}),
            }

        value = requester(payload, host_call=host_call, timeout=spec.timeout_seconds)
        if inspect.isawaitable(value):
            value = await value
        return value

    def _effect_ceiling_error(
        self,
        spec: DelegateSpec,
        request: CapabilityRequest,
        workspace: WorkspaceSpec,
    ) -> str | None:
        if not spec.effect_ceiling:
            return None
        allowed = {EffectClass(effect).value for effect in spec.effect_ceiling}
        resolver = getattr(self._dispatcher, "resolve_effects", None)
        try:
            if callable(resolver):
                effects = resolver(request, workspace)
            else:
                executor = self._dispatcher._executor_for(request, workspace)
                effects = self._dispatcher._resolve_effects_for(
                    executor.descriptor, request.arguments or {}
                )
            outside = sorted({effect.value for effect in effects} - allowed)
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            return f"delegate effect ceiling could not be resolved: {exc}"
        if outside:
            return "delegate effect ceiling denied effects: " + ", ".join(outside)
        return None


def _check_workspace(spec: DelegateSpec, workspace: WorkspaceSpec) -> None:
    if spec.allowed_workspace is None:
        return
    expected = os.path.realpath(os.path.abspath(spec.allowed_workspace))
    actual = os.path.realpath(os.path.abspath(workspace.root))
    if os.path.commonpath((expected, actual)) != expected:
        raise PermissionError("delegate is not allowed to operate in this workspace")


def _launch_signature(spec: DelegateSpec) -> str:
    return json.dumps(
        {
            "protocol": spec.protocol_value,
            "command": list(spec.command),
            "endpoint": spec.endpoint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class _SubprocessTransport:
    """Host-launched JSON-lines connector for trusted specialist commands.

    The command is supplied by host configuration, never by model input. The
    specialist receives protocol messages only; any Athena capability request
    it emits is answered by :meth:`ExternalDelegateManager` through the
    canonical dispatcher.
    """

    _MAX_FRAME_BYTES = 4 * 1024 * 1024

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @classmethod
    async def start(cls, spec: DelegateSpec, workspace: WorkspaceSpec):
        allowed_env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING")
            if key in os.environ
        }
        allowed_env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "ATHENA_DELEGATE_PROTOCOL": spec.protocol_value,
                "ATHENA_WORKSPACE_ROOT": os.path.realpath(workspace.root),
            }
        )
        process = await asyncio.create_subprocess_exec(  # architecture-lint: allow subprocess-outside-approved-backends reason=owned specialist transport
            *spec.command,
            cwd=os.path.realpath(workspace.root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=allowed_env,
        )
        return cls(process)

    async def request(self, payload: Mapping[str, Any], *, timeout: float):
        return await asyncio.wait_for(self._request(payload), timeout=timeout)

    async def request_host_call(
        self,
        payload: Mapping[str, Any],
        *,
        host_call,
        timeout: float,
    ):
        async def exchange():
            await self._send(payload)
            while True:
                message = await self._receive()
                message_type = str(message.get("type") or "")
                if message_type not in {"host.call", "capability_request"}:
                    return message
                response = await host_call(message)
                await self._send(
                    {
                        "type": "host.response",
                        "request_id": message.get("request_id") or message.get("id"),
                        **dict(response),
                    }
                )

        return await asyncio.wait_for(exchange(), timeout=timeout)

    async def _request(self, payload: Mapping[str, Any]):
        await self._send(payload)
        return await self._receive()

    async def _send(self, payload: Mapping[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise RuntimeError("delegate process stdin is unavailable")
        frame = (json.dumps(dict(payload), separators=(",", ":")) + "\n").encode()
        if len(frame) > self._MAX_FRAME_BYTES:
            raise ValueError("delegate protocol frame exceeds maximum size")
        stdin.write(frame)
        await stdin.drain()

    async def _receive(self) -> Mapping[str, Any]:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("delegate process stdout is unavailable")
        line = await stdout.readline()
        if not line:
            code = await self._process.wait()
            raise RuntimeError(f"delegate process exited before response ({code})")
        if len(line) > self._MAX_FRAME_BYTES:
            raise ValueError("delegate protocol frame exceeds maximum size")
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("delegate response must be a JSON object")
        return value

    async def close(self) -> None:
        if self._process.returncode is not None:
            return
        try:
            self._process.terminate()
        except ProcessLookupError:
            # The child may have exited between the returncode check and the
            # signal call.  Closing a durable delegate session is idempotent.
            return
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                self._process.kill()
            except ProcessLookupError:
                return
            try:
                await self._process.wait()
            except ProcessLookupError:
                # Some event-loop child watchers observe the reap slightly
                # after the OS has accepted the signal.  The process is gone;
                # there is no remaining transport authority to recover.
                return


__all__ = ["DelegateTransport", "ExternalDelegateManager"]
