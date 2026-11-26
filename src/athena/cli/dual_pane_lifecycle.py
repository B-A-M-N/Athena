"""Terminal and animation lifecycle for the retained dual-pane surface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from athena.cli.animation import AnimationClock

__all__ = ["DualPaneLifecycle"]


class DualPaneLifecycle:
    """Own terminal teardown and Glass-only animation invalidation."""

    def __init__(
        self,
        *,
        output: Any,
        terminal_session: Any,
        glass_presentation: Any,
        animator: Any,
        mascot: Any,
        full_screen: Callable[[], bool],
        display: Callable[[], str],
        prepare_display: Callable[[], None],
        repaint: Callable[..., Any],
    ) -> None:
        self._output = output
        self._terminal_session = terminal_session
        self._glass_presentation = glass_presentation
        self._animator = animator
        self._mascot = mascot
        self._full_screen = full_screen
        self._display = display
        self._prepare_display = prepare_display
        self._repaint = repaint
        self._animation_clock: AnimationClock | None = None

    def bind_animation_clock(self, clock: AnimationClock) -> None:
        self._animation_clock = clock

    def open(self) -> None:
        if not self._full_screen():
            return
        self._terminal_session.open()
        self._prepare_display()
        if self._animation_clock is not None:
            self._animation_clock.start()
        self._repaint(force=True)

    def close(self) -> None:
        if self._animation_clock is not None:
            self._animation_clock.stop()
        self._close_terminal()

    async def aclose(self) -> None:
        if self._animation_clock is not None:
            await self._animation_clock.stop_async()
        self._close_terminal()

    def tick(self, dt: float) -> bool:
        if not self._full_screen() or self._display() != "glass":
            return False
        if not self._animator.tick(dt):
            return False
        self._mascot.advance(dt)
        self._repaint(force=False)
        return True

    def _close_terminal(self) -> None:
        cleanup = self._glass_presentation.cleanup()
        if cleanup and self._terminal_session.active:
            self._output.write(cleanup)
            self._output.flush()
        self._terminal_session.close()
