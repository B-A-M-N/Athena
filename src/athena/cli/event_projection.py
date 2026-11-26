"""Canonical-event translation for the dual-pane terminal surface.

The surface owns rendering and input lifecycle; this helper owns only the
presentation-side fan-out from one canonical event into the shared projection,
mascot state, and raw OI stream. It never executes, approves, or persists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from athena.presentation.projection import ProjectionState
from athena.cli.terminal import sanitize_terminal_text

__all__ = ["DualPaneEventProjection"]


class DualPaneEventProjection:
    """Translate canonical events into dual-pane presentation state."""

    def __init__(self, projection: ProjectionState, mascot: Any, window: Any) -> None:
        self._projection = projection
        self._mascot = mascot
        self._window = window

    def ingest(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> None:
        """Reduce one event and retain only the raw stream tail needed by OI."""
        values = dict(payload)
        self._projection.reduce(event_type, values, task_id=task_id)
        self._mascot.observe(event_type, values)
        if event_type == "ExecutionStarted":
            self._window.feed(f"$ {sanitize_terminal_text(values.get('runtime') or 'runtime')}\n")
        elif event_type in {"StdoutChunk", "StderrChunk"}:
            data = sanitize_terminal_text(values.get("data"))
            if data:
                self._window.feed(("[err] " if event_type == "StderrChunk" else "") + data)

    async def render_event(
        self,
        event: Any,
        *,
        full_screen: bool,
        details: bool,
        parent_render: Callable[[Any], Awaitable[Any]],
        set_details: Callable[[bool], None],
        repaint: Callable[..., None],
    ) -> None:
        """Project an event, then delegate inherited line-mode rendering."""
        event_type = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        self.ingest(event_type, payload, task_id=getattr(event, "task_id", None))
        old_details = details
        if full_screen and event_type == "ModelDelta" and old_details:
            set_details(False)
        try:
            await parent_render(event)
        finally:
            set_details(old_details)
        if full_screen:
            repaint(force=event_type not in {"ModelDelta", "StdoutChunk", "StderrChunk"})

    async def choose_approval(
        self,
        event: Any,
        *,
        full_screen: bool,
        parent_choose: Callable[[Any], Awaitable[Any]],
        repaint: Callable[..., None],
    ) -> Any:
        """Delegate approval input and reflect the result in presentation state."""
        choice = await parent_choose(event)
        if full_screen:
            self._projection.acknowledge_approval(
                granted=choice.granted,
                scope=choice.scope,
            )
            repaint(force=True)
        return choice
