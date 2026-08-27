"""Bounded, stream-faithful OI output window.

Successor to the original ``_OIWindow`` with the stream-handling repairs
required by the UI mission (§18):

* partial lines are rejoined across chunks for BOTH stdout and stderr
  (the old dual-pane window split stderr blindly, corrupting fragments);
* carriage-return progress output updates one line in place instead of
  appending a row per repaint;
* ANSI escape sequences are stripped for display and ignored by width math;
* stdout/stderr stay distinguishable via a per-line ``err`` flag;
* committed history is bounded by the ring buffer; long-line truncation is
  display-only and never mutates committed content;
* binary/unprintable bytes are replaced rather than crashing the renderer.
"""

from __future__ import annotations

import re
from collections import deque
from typing import NamedTuple

__all__ = ["StreamWindow", "StreamLine", "strip_ansi", "display_width", "truncate_ansi"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[^[(\]0-9a-zA-Z]?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and non-printing control bytes."""
    return _CONTROL_RE.sub("�", _ANSI_RE.sub("", text))


# An escape sequence cut off by a chunk boundary (held until the next chunk).
_DANGLING_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07]*)?$")


def display_width(text: str) -> int:
    """Visible width ignoring any escape sequences (defensive double-strip)."""
    return len(_ANSI_RE.sub("", text))


def truncate_ansi(text: str, width: int) -> str:
    """Truncate *display* text to ``width`` columns with an ellipsis."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    plain = strip_ansi(text)
    if width == 1:
        return "…"
    return plain[: width - 1] + "…"


class StreamLine(NamedTuple):
    text: str
    err: bool = False


class StreamWindow:
    """Ring-buffered stream view with in-place CR progress updates."""

    def __init__(self, *, max_lines: int = 500) -> None:
        self.lines: deque[StreamLine] = deque(maxlen=max_lines)
        self._partial = ""
        self._partial_err = False
        # True after a carriage return: the next text overwrites the current
        # line from column 0 (progress bars, spinners, \r-updaters).
        self._at_line_start = False
        self._esc_hold = ""  # escape sequence split across chunk boundaries
        self.dropped = 0  # committed lines evicted by the ring bound

    # -- ingestion ---------------------------------------------------------
    def _sanitize(self, text: str) -> str:
        """Strip escapes; hold a chunk-split trailing escape for next time."""
        text = self._esc_hold + text
        self._esc_hold = ""
        m = _DANGLING_RE.search(text)
        if m:
            self._esc_hold = m.group(0)
            text = text[: m.start()]
        return strip_ansi(text)
    def _append_segment(self, seg: str, err: bool) -> None:
        # Split on carriage returns.  A CR moves the cursor to column 0; the
        # following text overwrites the existing line prefix (a longer old
        # line keeps its tail, exactly as a real terminal behaves).
        for i, part in enumerate(seg.split("\r")):
            if i > 0:
                self._at_line_start = True
            if not part:
                continue
            if self._at_line_start:
                tail = self._partial[len(part):] if len(self._partial) > len(part) else ""
                self._partial = part + tail
                self._at_line_start = False
            else:
                self._partial += part
            self._partial_err = self._partial_err or err

    def feed(self, data: str, *, err: bool = False) -> None:
        """Append raw stream output; newline-terminated lines commit."""
        if not data:
            return
        data = self._sanitize(data)
        parts = data.split("\n")
        for seg in parts[:-1]:
            self._append_segment(seg, err)
            self._commit()
        self._append_segment(parts[-1], err)

    def feed_delta(self, text: str, *, err: bool = False) -> None:
        """Model deltas arrive unterminated: update the partial tail only.

        Pure w.r.t. committed lines — the partial tail is rendered by
        snapshot() as a VIEW, never appended to lines (P2-44 regression).
        """
        if not text:
            return
        text = self._sanitize(text)
        if "\n" in text:
            self.feed(text, err=err)
            return
        self._append_segment(text, err)

    def _commit(self) -> None:
        if len(self.lines) == self.lines.maxlen:
            self.dropped += 1
        self.lines.append(StreamLine(self._partial, self._partial_err))
        self._partial = ""
        self._partial_err = False
        self._at_line_start = False

    def seal_partial(self) -> None:
        """Commit any trailing partial line (end of a turn / execution)."""
        if self._partial:
            self._commit()

    # -- rendering ---------------------------------------------------------
    def snapshot(self, height: int, width: int) -> list[StreamLine]:
        """Last ``height`` lines INCLUDING the live partial tail.  PURE."""
        committed = list(self.lines)
        if self._partial:
            committed.append(StreamLine(self._partial, self._partial_err))
        out = [StreamLine(truncate_ansi(ln.text, width), ln.err)
               for ln in committed[-height:]] if height > 0 else []
        while len(out) < height:
            out.insert(0, StreamLine("", False))
        return out

    def snapshot_text(self, height: int, width: int) -> list[str]:
        """Legacy text-only view (back-compat with existing callers/tests)."""
        return [ln.text for ln in self.snapshot(height, width)]
