"""Runtime-session lifecycle coordination beneath ``ExecutionManager``.

This module coordinates session creation and durable identity adoption.  The
``CancellationRegistry`` remains the only owner of live session indexes, while
``ExecutionManager`` remains the only execution authority and persistence
facade.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Mapping, cast

from athena.concurrency import run_blocking
from athena.execution.backend import ExecutionBackend
from athena.execution.cancellation import CancellationRegistry
from athena.protocol.execution import Runtime

_logger = logging.getLogger("athena.execution.session_lifecycle")

SelectBackend = Callable[[str], ExecutionBackend | None]
ResolveRuntime = Callable[[str], Runtime]
LookupBackend = Callable[[str], ExecutionBackend | None]
PersistSessionStart = Callable[..., Awaitable[None]]


class ExecutionSessionCoordinator:
    """Coordinate runtime-session lifecycle without owning session indexes."""

    def __init__(
        self,
        *,
        registry: CancellationRegistry,
        select_backend: SelectBackend,
        resolve_runtime: ResolveRuntime,
        lookup_backend: LookupBackend,
        persist_session_start: PersistSessionStart,
    ) -> None:
        self._registry = registry
        self._select_backend = select_backend
        self._resolve_runtime = resolve_runtime
        self._lookup_backend = lookup_backend
        self._persist_session_start = persist_session_start

    async def create_session(
        self,
        *,
        task_id: str,
        runtime: str,
        backend: str = "local",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        workspace_root: str | None = None,
        network_policy: str | None = None,
    ) -> str:
        self._registry.cancel_requested_tasks.discard(task_id)
        selected_backend = self._select_backend(backend)
        if selected_backend is not None:
            sid = await selected_backend.create_session(
                task_id=task_id,
                runtime=runtime,
                cwd=cwd,
                env=env,
                workspace_root=workspace_root,
                network_policy=network_policy,
            )
            self._registry.task_sessions.setdefault(task_id, []).append((selected_backend, sid))
            self._registry.runtime_by_session[sid] = selected_backend
            identity: dict[str, Any] = {}
            describe = getattr(selected_backend, "describe_session", None)
            if callable(describe):
                try:
                    identity = dict(await describe(sid))
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    _logger.warning("failed to describe runtime session %s: %s", sid, exc)
            try:
                await self._persist_session_start(
                    sid,
                    task_id,
                    backend=getattr(selected_backend, "name", backend),
                    runtime=runtime,
                    cwd=cwd,
                    metadata={
                        "workspace_root": workspace_root,
                        "network_policy": network_policy,
                        "env": dict(env or {}),
                        **identity,
                    },
                )
            except Exception:  # rationale: owned shutdown/persistence boundary must record failure and preserve task state
                await self.destroy_unpersisted_session(selected_backend, sid, task_id)
                raise
            return sid

        runtime_impl = self._resolve_runtime(runtime)
        kwargs: dict[str, Any] = {"task_id": task_id}
        if env is not None:
            kwargs["env"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd
        if workspace_root is not None:
            kwargs["workspace_root"] = workspace_root
        if network_policy is not None:
            kwargs["network_policy"] = network_policy
        try:
            signature = inspect.signature(runtime_impl.create_session)
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
        except (TypeError, ValueError):
            pass
        if asyncio.iscoroutinefunction(runtime_impl.create_session):
            sid = await runtime_impl.create_session(**kwargs)
        else:
            sid = cast(str, runtime_impl.create_session(**kwargs))
        self._registry.task_sessions.setdefault(task_id, []).append((runtime_impl, sid))
        self._registry.runtime_by_session[sid] = runtime_impl
        try:
            await self._persist_session_start(
                sid,
                task_id,
                backend=backend,
                runtime=runtime,
                cwd=cwd,
                metadata={
                    "workspace_root": workspace_root,
                    "network_policy": network_policy,
                    "env": dict(env or {}),
                },
            )
        except Exception:  # rationale: owned shutdown/persistence boundary must record failure and preserve task state
            await self.destroy_unpersisted_session(runtime_impl, sid, task_id)
            raise
        return sid

    async def reattach_session(self, record: Mapping[str, Any]) -> bool:
        """Reattach one persisted backend session after identity proof."""
        backend_name = str(record.get("backend") or "")
        backend = self._lookup_backend(backend_name)
        if backend is None or not bool(getattr(backend, "supports_reattach", False)):
            return False
        reattach = getattr(backend, "reattach_session", None)
        if not callable(reattach):
            return False
        session_id = str(record.get("id") or "")
        task_id = str(record.get("task_id") or "")
        attached_id = await reattach(record)
        if str(attached_id) != session_id:
            raise RuntimeError(
                f"backend {backend_name!r} returned unexpected runtime session id {attached_id!r}"
            )
        self._registry.runtime_by_session[session_id] = backend
        rooms = self._registry.task_sessions.setdefault(task_id, [])
        if not any(sid == session_id for _runtime, sid in rooms):
            rooms.append((backend, session_id))
        return True

    async def adopt_runtime_session(
        self,
        runtime: Runtime,
        runtime_session_id: str,
        task_id: str,
        *,
        backend: str,
        runtime_name: str,
        cwd: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist and register a runtime-emitted session identity."""
        if runtime_session_id in self._registry.runtime_by_session:
            return
        try:
            await self._persist_session_start(
                runtime_session_id,
                task_id,
                backend=backend,
                runtime=runtime_name,
                cwd=cwd,
                metadata=dict(metadata or {}),
            )
        except Exception:  # rationale: owned shutdown/persistence boundary must record failure and preserve task state
            await self.destroy_unpersisted_session(runtime, runtime_session_id, task_id)
            raise
        self._registry.runtime_by_session[runtime_session_id] = runtime
        rooms = self._registry.task_sessions.setdefault(task_id, [])
        if not any(sid == runtime_session_id for _runtime, sid in rooms):
            rooms.append((runtime, runtime_session_id))

    async def destroy_unpersisted_session(
        self, runtime: Any, session_id: str, task_id: str
    ) -> None:
        """Destroy a session whose durable start could not be recorded."""
        close = getattr(runtime, "close", None) or getattr(runtime, "destroy_session", None)
        if close is not None:
            try:
                if asyncio.iscoroutinefunction(close):
                    await close(session_id)
                else:
                    await run_blocking(close, session_id)
            except Exception as exc:  # rationale: owned shutdown/persistence boundary must record failure and preserve task state
                _logger.error(
                    "runtime session %s remained live after persistence failure: %s",
                    session_id,
                    exc,
                )
        self._registry.runtime_by_session.pop(session_id, None)
        rooms = [
            (candidate, sid)
            for candidate, sid in self._registry.task_sessions.get(task_id, [])
            if sid != session_id
        ]
        if rooms:
            self._registry.task_sessions[task_id] = rooms
        else:
            self._registry.task_sessions.pop(task_id, None)


__all__ = ["ExecutionSessionCoordinator"]
