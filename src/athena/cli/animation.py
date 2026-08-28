"""Event-truthful presentation animation for the OI scene."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable


_logger = logging.getLogger("athena.cli.animation")
_ACTIVE_STATES = frozenset(
    {
        "THINKING",
        "READING",
        "SEARCHING",
        "TOOLS",
        "EXECUTING",
        "APPROVAL",
        "FAILURE",
        "RECOVERING",
        "DELEGATED",
        "CODING",
        "VERIFYING",
        "TESTING",
    }
)


@dataclass
class OIVisualState:
    semantic_state: str = "idle"
    action_kind: str = "idle"
    code_line_count: int = 0
    buddy_anchor: str = "center"
    previous_anchor: str = "center"
    phase: float = 0.0
    transition: float = 1.0
    scene_transition: float = 1.0
    cursor_phase: float = 0.0
    scan_phase: float = 0.0
    pulse_phase: float = 0.0
    grid_phase: float = 0.0
    code_reveal: float = 1.0
    activity_phase: float = 0.0
    dirty: bool = True


class OIAnimator:
    def __init__(self, *, reduced_motion: bool = False) -> None:
        self.reduced_motion = reduced_motion
        self.visual = OIVisualState()

    def set_state(
        self,
        semantic_state: str,
        buddy_anchor: str,
        *,
        action_kind: str | None = None,
        code_lines: int = 0,
    ) -> None:
        action_kind = action_kind or semantic_state
        changed = (
            semantic_state != self.visual.semantic_state
            or action_kind != self.visual.action_kind
            or buddy_anchor != self.visual.buddy_anchor
            or (action_kind.casefold() == "code" and code_lines != self.visual.code_line_count)
        )
        if changed:
            self.visual.previous_anchor = self.visual.buddy_anchor
            self.visual.semantic_state = semantic_state
            self.visual.action_kind = action_kind
            self.visual.code_line_count = max(int(code_lines), 0)
            self.visual.buddy_anchor = buddy_anchor
            self.visual.transition = 1.0 if self.reduced_motion else 0.0
            self.visual.scene_transition = 1.0 if self.reduced_motion else 0.0
            self.visual.code_reveal = 1.0 if self.reduced_motion or not code_lines else 0.0
            self.visual.dirty = True

    def tick(self, dt: float) -> bool:
        """Advance presentation only; never changes semantic state."""
        if self.reduced_motion or (
            self.visual.semantic_state.upper() not in _ACTIVE_STATES
            and self.visual.transition >= 1.0
        ):
            self.visual.dirty = False
            return False
        self.visual.phase = (self.visual.phase + max(float(dt), 0.0) * 2.0) % 1.0
        self.visual.transition = min(1.0, self.visual.transition + max(float(dt), 0.0) * 3.0)
        delta = max(float(dt), 0.0)
        self.visual.scene_transition = min(1.0, self.visual.scene_transition + delta * 2.5)
        self.visual.cursor_phase = (self.visual.cursor_phase + delta * 5.0) % 1.0
        self.visual.scan_phase = (self.visual.scan_phase + delta * 0.9) % 1.0
        self.visual.pulse_phase = (self.visual.pulse_phase + delta * 2.2) % 1.0
        self.visual.grid_phase = (self.visual.grid_phase + delta * 0.35) % 1.0
        self.visual.activity_phase = (self.visual.activity_phase + delta * 1.6) % 1.0
        if self.visual.action_kind.casefold() == "code":
            self.visual.code_reveal = min(1.0, self.visual.code_reveal + delta * 3.5)
        self.visual.dirty = True
        return True


class AnimationClock:
    """Low-rate async clock that invalidates only the OI viewport."""

    def __init__(
        self,
        callback: Callable[[float], bool | None],
        *,
        enabled: bool = True,
        reduced_motion: bool = False,
    ) -> None:
        self.callback = callback
        self.enabled = enabled and not reduced_motion
        self.reduced_motion = reduced_motion
        self._task: asyncio.Task[None] | None = None
        self._last = 0.0

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        self._last = time.monotonic()
        interval = 0.10
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                dt = now - self._last
                self._last = now
                try:
                    active = self.callback(dt)
                    # A settled scene can sleep at a lower cadence. Callback
                    # compatibility is intentional: legacy callbacks that
                    # return None remain active rather than silently freezing.
                    interval = 0.10 if active is not False else 0.50
                except Exception:  # noqa: BLE001 - animation must not kill the REPL
                    _logger.warning("OI animation callback failed", exc_info=True)
                    interval = 0.10
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        """Request cancellation for synchronous teardown callers."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    async def stop_async(self) -> None:
        """Cancel and await the clock task before a loop is torn down."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - teardown must remain best-effort
            _logger.warning("OI animation task failed during shutdown", exc_info=True)


__all__ = ["AnimationClock", "OIAnimator", "OIVisualState"]
