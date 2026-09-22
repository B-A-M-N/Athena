"""Body-observation bridge between runtime events and the reasoning kernel.

The lifecycle owns startup sequencing and teardown. This module owns only the
narrow translation and task-set mechanics for terminal/debugger observations;
it never decides whether an observation changes autonomous reasoning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from athena.kernel.kernel import AgentKernel
from athena.state.events import EventStore

_logger = logging.getLogger("athena.service")


def screen_observation_from_event(event, payload: dict):
    """Convert a runtime screen event into a typed interpreter observation."""
    from athena.interpreter.protocol import BodyObservationKind, InterpreterObservation

    session_id = str(payload.get("session") or "")
    if not session_id:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.TERMINAL_SCREEN_CHANGED,
        payload={
            "session": session_id,
            "screen_chars": int(payload.get("screen_chars") or 0),
            "screen_text": str(payload.get("screen_text") or "")[:16_000],
            "rows": payload.get("rows"),
            "cols": payload.get("cols"),
        },
        task_id=getattr(event, "task_id", None),
        session_id=getattr(event, "session_id", None),
        runtime_session_id=session_id,
    )


def bind_body_observation_bridge(
    background_tasks: set[asyncio.Task], events: EventStore, kernel: AgentKernel
) -> list[Any]:
    """Subscribe terminal/debugger events and own their observation tasks."""

    def _spawn_observation(coro) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(background_tasks.discard)
        task.add_done_callback(_log_observation_failure)
        background_tasks.add(task)

    def _log_observation_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.warning("body-observation offer failed: %s", exc, exc_info=exc)

    def _on_runtime_screen_changed(event) -> None:
        payload = getattr(event, "payload", None) or {}
        observation = screen_observation_from_event(event, payload)
        if observation is not None:
            _spawn_observation(kernel.offer_body_observation(observation))

    def _on_debugger_stopped(event) -> None:
        payload = dict(getattr(event, "payload", None) or {})
        if not payload.get("session"):
            return
        from athena.interpreter.protocol import BodyObservationKind, InterpreterObservation

        observation = InterpreterObservation(
            kind=BodyObservationKind.DEBUGGER_STOPPED,
            payload={
                "reason": payload.get("reason"),
                "thread_id": payload.get("thread_id"),
                "location": payload.get("location"),
                "frames": list(payload.get("frames") or [])[:8],
                "frames_head": "\n".join(
                    str(frame.get("name") or "") for frame in list(payload.get("frames") or [])[:8]
                ),
            },
            task_id=getattr(event, "task_id", None),
            session_id=getattr(event, "session_id", None),
            runtime_session_id=payload.get("runtime_session_id"),
        )
        _spawn_observation(kernel.offer_body_observation(observation))

    callbacks = [_on_runtime_screen_changed, _on_debugger_stopped]
    events.subscribe(_on_runtime_screen_changed, event_types={"RuntimeScreenChanged"})
    events.subscribe(_on_debugger_stopped, event_types={"DebuggerStopped"})
    return callbacks
