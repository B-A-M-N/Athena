"""Terminal lifecycle and safe text projection helpers."""

from __future__ import annotations

import atexit
import os
import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any, TextIO, TypeAlias

from athena.presentation.text import sanitize_terminal_text


SignalHandler: TypeAlias = Callable[[int, FrameType | None], Any] | int | None


class TerminalSession:
    """Idempotent alternate-screen/cursor lifecycle for interactive surfaces."""

    ENTER = "\x1b[?1049h\x1b[?25l"
    LEAVE = "\x1b[?25h\x1b[?1049l"

    def __init__(self, output: TextIO | None = None, *, enabled: bool = True) -> None:
        self.output = output or sys.stdout
        self.enabled = bool(enabled and getattr(self.output, "isatty", lambda: False)())
        self.active = False
        self._previous_handlers: dict[int, SignalHandler] = {}
        self._atexit_registered = False

    def open(self) -> None:
        if self.active or not self.enabled:
            return
        self.output.write(self.ENTER)
        self.output.flush()
        self.active = True
        atexit.register(self.close)
        self._atexit_registered = True
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        for sig in signals:
            try:
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except (OSError, RuntimeError, ValueError):
                pass

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        previous = self._previous_handlers.get(signum)
        self.close()
        if callable(previous):
            previous(signum, _frame)
        elif previous == signal.SIG_DFL:
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            # SIGTERM/SIGHUP must retain their normal process semantics after
            # the alternate screen has been restored; do not translate them
            # into a Python exception or leave a damaged terminal behind.
            os.kill(os.getpid(), signum)

    def close(self) -> None:
        if not self.active:
            return
        try:
            self.output.write(self.LEAVE)
            self.output.flush()
        finally:
            self.active = False
            if self._atexit_registered:
                atexit.unregister(self.close)
                self._atexit_registered = False
            for sig, previous in self._previous_handlers.items():
                try:
                    signal.signal(sig, previous)
                except (OSError, RuntimeError, ValueError):
                    pass
            self._previous_handlers.clear()


__all__ = ["TerminalSession", "sanitize_terminal_text"]
