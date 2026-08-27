"""Event-truthful presentation animation for the OI scene."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable


_logger = logging.getLogger("athena.cli.animation")
_ACTIVE_STATES = frozenset({
    "THINKING", "READING", "SEARCHING", "TOOLS", "EXECUTING", "APPROVAL",
    "FAILURE", "RECOVERING", "DELEGATED",
})


@dataclass
class OIVisualState:
    semantic_state: str = "idle"
    buddy_anchor: str = "center"
    previous_anchor: str = "center"
    phase: float = 0.0
    transition: float = 1.0
    dirty: bool = True


class OIAnimator:
    def __init__(self, *, reduced_motion: bool = False) -> None:
        self.reduced_motion = reduced_motion
        self.visual = OIVisualState()

    def set_state(self, semantic_state: str, buddy_anchor: str) -> None:
        changed = semantic_state != self.visual.semantic_state or buddy_anchor != self.visual.buddy_anchor
        if changed:
            self.visual.previous_anchor = self.visual.buddy_anchor
            self.visual.semantic_state = semantic_state
            self.visual.buddy_anchor = buddy_anchor
            self.visual.transition = 1.0 if self.reduced_motion else 0.0
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
        self.visual.dirty = True
        return True


class AnimationClock:
    """Low-rate async clock that invalidates only the OI viewport."""

    def __init__(self, callback: Callable[[float], bool | None], *, enabled: bool = True, reduced_motion: bool = False) -> None:
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
