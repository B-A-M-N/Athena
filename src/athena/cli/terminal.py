"""Terminal lifecycle and safe text projection helpers."""

from __future__ import annotations

import atexit
import os
import re
import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any, TextIO, TypeAlias


SignalHandler: TypeAlias = Callable[[int, FrameType | None], Any] | int | None


# CSI, OSC, DCS, SOS, PM, APC and Kitty graphics payloads are all terminal
# protocols, never display content.  The terminators cover both 7-bit ST and
# their 8-bit C1 forms.
_PROTOCOL = re.compile(
    r"(?:"
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x9d[^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b(?:P|X|\^|_)[^\x1b]*(?:\x1b\\)"
    r"|\x90[^\x1b]*(?:\x1b\\)"
    r"|\x98[^\x1b]*(?:\x1b\\)"
    r"|\x9e[^\x1b]*(?:\x1b\\)"
    r"|\x9f[^\x1b]*(?:\x1b\\)"
    r")",
    re.DOTALL,
)


def sanitize_terminal_text(value: object) -> str:
    """Return printable projection text while retaining canonical input raw.

    Newline and tab are intentionally retained.  Carriage returns become
    newlines so progress output cannot rewrite a prior screen row.  All other
    C0/C1 controls and terminal protocols are discarded.
    """
    text = _PROTOCOL.sub("", str(value or "")).replace("\r", "\n")
    return "".join(
        char
        for char in text
        if char in {"\n", "\t"} or (ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F)
    )


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
