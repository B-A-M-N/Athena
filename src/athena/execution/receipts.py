"""Durable execution/session receipt persistence.

Subordinate to :class:`athena.execution.manager.ExecutionManager`. This module
owns only storage mechanics for execution and runtime-session receipts; it does
not select backends, authorize effects, or own cancellation strategy.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Mapping

_logger = logging.getLogger("athena.execution")


class ExecutionReceiptPersistence:
    """Persist runtime-session and execution receipts through owned stores."""

    def __init__(
        self,
        *,
        runtime_session_store,
        execution_store,
        event_sink=None,
        recovery_sink=None,
        durability_mandatory: bool,
    ) -> None:
        self._rt_sessions = runtime_session_store
        self._exec_store = execution_store
        self._event_sink = event_sink
        self._recovery_sink = recovery_sink
        self._durability_mandatory = durability_mandatory

    def set_recovery_sink(self, sink) -> None:
        self._recovery_sink = sink

    async def _emit_event(self, event_type: str, payload: dict, task_id: str | None = None) -> None:
        if self._event_sink is None:
            return
        from athena.protocol.events import make_event

        event = make_event(event_type, payload, task_id=task_id)
        await self._event_sink(event)

    async def session_start(
        self,
        session_id: str,
        task_id: str,
        *,
        backend: str,
        runtime: str,
        cwd: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        store = self._rt_sessions
        if store is None:
            if self._durability_mandatory:
                raise RuntimeError(
                    "runtime session durability is mandatory but no store is configured"
                )
            return
        try:
            await store.start(
                session_id,
                task_id=task_id,
                backend=backend,
                runtime=runtime,
                cwd=cwd,
                metadata=dict(metadata or {}),
            )
        except Exception as exc:
            _logger.warning("failed to persist session start %s: %s", session_id, exc)
            await self._emit_event(
                "RuntimeSessionStartPersistFailed",
                {"session_id": session_id, "task_id": task_id, "error": str(exc)},
                task_id=task_id,
            )
            if self._durability_mandatory:
                raise

    async def session_closed(self, session_id: str) -> None:
        if self._rt_sessions is None:
            return
        await self._rt_sessions.mark_closed(session_id)

    async def update_execution_runtime_session(
        self, execution_id: str, runtime_session_id: str
    ) -> None:
        if self._exec_store is None:
            return
        try:
            await self._exec_store.update_runtime_session(execution_id, runtime_session_id)
        except Exception as exc:
            _logger.warning("failed to update execution runtime session %s: %s", execution_id, exc)

    async def execution_start(
        self,
        execution_id: str,
        *,
        task_id: str,
        runtime_session_id: str | None,
        source: str,
        cwd: str | None = None,
        env=None,
    ) -> None:
        """Durability-mandatory pre-execution receipt."""
        store = self._exec_store
        if store is None:
            return
        try:
            await store.start(
                execution_id,
                task_id=task_id,
                runtime_session_id=runtime_session_id,
                command=source,
                cwd=cwd,
                env=dict(env) if env else None,
            )
        except Exception as exc:
            _logger.error("failed to persist execution start %s: %s", execution_id, exc)
            await self._emit_event(
                "ExecutionStartPersistFailed",
                {"execution_id": execution_id, "task_id": task_id, "error": str(exc)},
                task_id=task_id,
            )
            if self._durability_mandatory:
                raise

    async def execution_finish(
        self,
        execution_id: str,
        *,
        task_id: str,
        runtime: str,
        backend: str,
        cwd: str | None,
        exit_status,
        exit_code: int | None,
        timed_out: bool = False,
        interrupted: bool = False,
        execution_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Best-effort post-effect receipt plus explicit recovery marker."""
        store = self._exec_store
        if store is None:
            return
        try:
            await store.finish(
                execution_id,
                status=exit_status,
                exit_code=exit_code,
                metadata={
                    "task_id": task_id,
                    "runtime": runtime,
                    "backend": backend,
                    "cwd": cwd,
                    "timed_out": timed_out,
                    "interrupted": interrupted,
                    **dict(execution_metadata or {}),
                },
            )
        except Exception as exc:
            _logger.warning("failed to persist execution finish %s: %s", execution_id, exc)
            await self._emit_event(
                "ExecutionFinishPersistFailed",
                {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "error": str(exc),
                },
                task_id=task_id,
            )
            if self._recovery_sink is not None:
                try:
                    marker = {
                        "kind": "execution_finish_persist_failed",
                        "execution_id": execution_id,
                        "runtime": runtime,
                        "backend": backend,
                        "exit_status": getattr(exit_status, "value", str(exit_status)),
                        "exit_code": exit_code,
                        "error": str(exc),
                    }
                    result = self._recovery_sink(task_id, marker)
                    if inspect.isawaitable(result):
                        await result
                except Exception as recovery_error:
                    _logger.error(
                        "could not persist execution recovery marker %s: %s",
                        execution_id,
                        recovery_error,
                    )
