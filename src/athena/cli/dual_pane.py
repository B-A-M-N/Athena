"""Dual-pane terminal projection: calm Hermes surface + live OI window.

Design (INV-007 preserved): BOTH panes are read-only projections of the same
canonical event stream. Neither pane executes, approves, or owns state.

Left pane — the calm operator surface (execution cards, approvals, task
state). Right pane — the OI window: an unbuffered, character-faithful stream
of model deltas and runtime stdout/stderr in a ring buffer, so the fast raw
output is visible live without polluting the calm surface.

Rendering strategy: the OI window keeps its own line buffer and repaints a
bordered region on each update using ANSI cursor saves; the calm pane writes
linearly above/below as before. In dumb environments (pipes, tests) the dual
renderer degrades to writing both streams sequentially with pane tags.
"""

from __future__ import annotations

import os
import shutil
from collections import deque
from typing import Any

from athena.cli.surface import OperatorSurface

__all__ = ["DualPaneSurface", "Mascot"]


class Mascot:
    """Athena's owl — the embodiment of the computational body.

    State is driven by REAL kernel events only (never decoration):
    idle -> thinking (model streaming) -> executing (runtime output)
    -> waiting (approval needed) -> done / failed.

    The mascot "carries" an activity object ([>] terminal, [?] approval,
    {*} artifact, ...) identifying WHAT it's operating, per the visual
    language spec. For `!cmd` direct execution the mascot takes the
    command itself: mind -> body -> result -> back to mind.
    """

    # activity objects keyed by context
    OBJ_TERMINAL = "[>]"
    OBJ_PROCESS = "[#]"
    OBJ_CODE = "{ }"
    OBJ_VERIFY = "[✓]"
    OBJ_FAIL = "[!]"
    OBJ_APPROVAL = "[?]"
    OBJ_ARTIFACT = "[*]"

    # ------------------------------------------------------------------
    # Character registry. Each entry: {"label", "frames": {state: (a, b)}}
    # Two animation frames per state; render() alternates them.
    # ------------------------------------------------------------------
    CHARACTERS: dict[str, dict[str, Any]] = {}

    FRAMES = {
        "idle": (
            r"""
       ,___,
       (O,O)
       /)_)
      """,
            r"""
       ,___,
       (o,o)
       /)_)
      """,
        ),
        "thinking": (
            r"""
      ╔══════╗
      ║ ◉ ▄ ◉║   ∿∿∿
      ║  ‗‗  ║  ∿∿∿  ideas
      ╚══════╝   ⚡
      """,
            r"""
      ╔══════╗
      ║ ◎ ≈ ◎║ ~ ∿∿
      ║  ‗‗  ║  ∿∿∿  forming…
      ╚══════╝   ⚡
      """
        ),
        "executing": (
            r"""
        ▄▄▄▄▄
       █ ◉_◉ █    ▤▤▓▒
       █  ▽  █ ▧▤▓▒▒ running
        ▀▀▀▀▀
      """,
            r"""
        ▄▄▄▄▄
       █ ◉^◉ █    ▒▒▓▤
       █  ≡  █ ▒▒▓▤▧ hacking
        ▀▀▀▀▀
      """,
        ),
        "waiting": (
            r"""
        ▄▄▄▄▄
       █ ⊙︵⊙ █
       █  ▽  █   ⏸ awaiting permission
        ‛‛‛‛‛
      """,
            r"""
        ▄▄▄▄▄
       █ ⊙‿⊙ █
       █  ⌣  █   ⏸ may i?
        ‛‛‛‛‛
      """,
        ),
        "done": (
            r"""
        ▄▄▄▄▄
       █ ★‿★ █
       █  ◡  █   ✓ complete
        ▀▀▀▀▀
      """,
            r"""
        ▄▄▄▄▄
       █ ^‿^ █
       █  ◡  █   ✓ done!
        ▀▀▀▀▀
      """,
        ),
        "failed": (
            r"""
        ▄▄▄▄▄
       █ ✕︵✕ █
       █  ─  █   ✗ oops
        ▀▀▀▀▀
      """,
            r"""
        ▄▄▄▄▄
       █ ✕﹏✕ █
       █  ˘  █   ✗ failed
        ▀▀▀▀▀
      """,
        ),
    }

    CAT_FRAMES = {
        "idle": (
            r"""
     /\_/\
    ( -.- )   zZ
     > ^ <
      """,
            r"""
     /\_/\
    ( •‿• )
     > ~ <   *blink*
      """,
        ),
        "thinking": (
            r"""
     /\_/\   ?????
    ( ⊙﹏⊙ )  ┌─┐
     |    |  └─┤ think…
      """,
            r"""
     /\_/\   ????
    ( ⊙▽⊙ )  ┌─┐
     |    |  └─┤ hmm…
      """,
        ),
        "executing": (
            r"""
     /\_/\   ▨▤▹
    ( =⍤= )  ▹▨▤  typing
     /|   |\
      """,
            r"""
     /\_/\   ◃▤▨
    ( =◔= )  ▨◃▤  pouncing on bugs
     /|   |\
      """,
        ),
        "waiting": (
            r"""
     /\_/\
    ( ˇ︵ˇ )
     > ? <   ⏸ let me in…
      """,
            r"""
     /\_/\
    ( •︵• )
     > ? <   ⏸ pretty please?
      """,
        ),
        "done": (
            r"""
     /\_/\
    ( ★‿★ )  ✓ caught it
     \_~_/
      """,
            r"""
     /\_/\   ✓ purr…
    ( ‿‿‿ )
     \_~_/
      """,
        ),
        "failed": (
            r"""
     /\_/\   ✗ hiss
    ( ✕﹏✕ )
     > ~ <
      """,
            r"""
     /\_/\   ✗ mrow.
    ( ✕︵✕ )
     > ~ <   ears flat
      """,
        ),
    }

    ROBOT_FRAMES = {
        "idle": (
            r"""
      ┌───┐
      │ ‿ │  [standby]
     ╭┴─┴╮
      """,
            r"""
      ┌───┐
      │ ° │  [standby]
     ╭┴─┴╮  ·
      """,
        ),
        "thinking": (
            r"""
      ┌───┐
      │ ▓▓│  [CPU 97%]
     ╭┴─┴╮ ⟨⟨⟨
      """,
            r"""
      ┌───┐
      │ ▒▓│  [CPU 84%]
     ╭┴─┴╮ ⟩⟩⟩ computing
      """,
        ),
        "executing": (
            r"""
      ┌───┐  ▸▸
      │ ◉ │  EXEC
     ╭┴─┴╮  ▸▸▸
      """,
            r"""
      ┌───┐  ▸▸
      │ ◎ │  RUN
     ╰┬─┬╯  ▸ ▸
      """,
        ),
        "waiting": (
            r"""
      ┌───┐
      │ ○?│  [HALT]
     ╭┴─┴╮  awaiting input
      """,
            r"""
      ┌───┐
      │ ◇?│  [HALT]
     ╭┴─┴╮  …authorization?
      """,
        ),
        "done": (
            r"""
      ┌───┐
      │ ^^│  ✓ EXIT 0
     ╭┴─┴╮  task complete
      """,
            r"""
      ┌───┐  ♪
      │ ‿ │  ✓ SUCCESS
     ╰┬─┬╯
      """,
        ),
        "failed": (
            r"""
      ┌───┐
      │ ✕✕│  ✗ SEGFAULT
     ╭┴─┴╮  stack trace…
      """,
            r"""
      ┌───┐
      │ ❧❧│  ✗ ERROR
     ╰┬─┬╯  dumping core…
      """,
        ),
    }

    def __init__(self, character: str = "owl") -> None:
        self.character = character if character in self.CHARACTERS else "owl"
        self.state = "idle"
        self.object = ""       # carried activity object, e.g. "[>]"
        self.speech = ""       # short deterministic operational line
        self._frame = 0

    @classmethod
    def _register_characters(cls) -> None:
        cls.CHARACTERS = {
            "owl": {"label": "Athena's owl", "frames": cls.FRAMES},
            "cat": {"label": "Terminal cat", "frames": cls.CAT_FRAMES},
            "bot": {"label": "Little robot", "frames": cls.ROBOT_FRAMES},
        }

    def observe(self, event_type: str) -> None:
        if event_type == "ModelDelta":
            self.state = "thinking"
            self.object = ""
        elif event_type in {"StdoutChunk", "StderrChunk", "ExecutionStarted"}:
            if self.state != "executing":
                self.state = "executing"
                self.object = self.OBJ_TERMINAL
        elif event_type == "CapabilityRequested":
            self.state = "executing"
            self.object = self.OBJ_CODE
        elif event_type == "ApprovalRequested":
            self.state = "waiting"
            self.object = self.OBJ_APPROVAL
            self.speech = "Waiting on you."
        elif event_type == "ApprovalResolved":
            self.object = ""
            self.speech = ""
        elif event_type in {"TaskCompleted", "TaskPartial"}:
            self.state = "done"
            self.object = self.OBJ_VERIFY
        elif event_type == "TaskFailed":
            self.state = "failed"
            self.object = self.OBJ_FAIL
            self.speech = "That did not work."

    def render(self, max_width: int = 24) -> list[str]:
        if not Mascot.CHARACTERS:
            Mascot._register_characters()
        frames = self.CHARACTERS[self.character]["frames"]
        art = frames.get(self.state, frames["idle"])[
            self._frame % 2]
        self._frame += 1
        lines = [ln.rstrip() for ln in art.strip("\n").splitlines()]
        width = max((len(ln) for ln in lines), default=0)
        pad = max(max_width - width, 0)
        left = " " * (pad // 2)
        out = [f"{left}{ln}" for ln in lines]
        # Carried object + speech ride under the character.
        tag = f"{self.object} {self.speech}".strip()
        if tag:
            out.append(f"{left}{tag}"[:max_width])
        return out

    def set_character(self, name: str) -> bool:
        """Switch mascot character; returns False for unknown names."""
        if not Mascot.CHARACTERS:
            Mascot._register_characters()
        if name not in self.CHARACTERS:
            return False
        self.character = name
        return True

    @classmethod
    def available(cls) -> dict[str, str]:
        """Character id -> label."""
        if not cls.CHARACTERS:
            cls._register_characters()
        return {k: v["label"] for k, v in cls.CHARACTERS.items()}


class _OIWindow:
    """Ring-buffered, unbuffered-flush view of raw model/runtime output."""

    def __init__(self, *, max_lines: int = 500, width: int | None = None) -> None:
        self.lines: deque[str] = deque(maxlen=max_lines)
        self._partial = ""
        self.width = width

    def feed(self, text: str) -> None:
        """Append raw output, splitting on newlines; keep partial last line."""
        self._partial += text
        while "\n" in self._partial:
            line, _, rest = self._partial.partition("\n")
            self.lines.append(line)
            self._partial = rest

    def feed_delta(self, text: str) -> None:
        """Model deltas arrive WITHOUT newlines: update the partial tail only.

        Pure w.r.t. committed lines — the partial tail is rendered by
        snapshot() as a VIEW of _partial, never appended to lines. This
        prevents the duplicated-fragment bug (P2-44).
        """
        if not text:
            return
        self._partial += text

    def seal_partial(self) -> None:
        """Commit any trailing partial line (end of a turn / execution)."""
        if self._partial:
            self.lines.append(self._partial)
            self._partial = ""

    def snapshot(self, height: int, width: int) -> list[str]:
        """Last ``height`` lines INCLUDING the live partial tail.

        PURE: does not mutate committed state (no seal_partial here).
        """
        committed = list(self.lines)
        if self._partial:
            committed.append(self._partial)
        out: list[str] = []
        for line in committed[-height:]:
            if width > 0 and len(line) > width:
                line = line[: width - 1] + "…"
            out.append(line)
        while len(out) < height:
            out.insert(0, "")
        return out


class DualPaneSurface(OperatorSurface):
    """Side-by-side: calm Hermes surface (left) + live OI window (right).

    The left column renders exactly like :class:`OperatorSurface` (same cards,
    approvals, buffering). The right column mirrors every ModelDelta and
    Stdout/StderrChunk unbuffered inside a bordered window with a header.
    """

    PANE_GAP = 3  # columns between panes (borders + gap)

    def __init__(self, *args, oi_height: int = 12, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.oi_enabled = True
        self.oi_height = oi_height
        self.window = _OIWindow()
        self.mascot = Mascot(
            character=os.environ.get("ATHENA_MASCOT", "owl"))
        self._term_cols, self._term_rows = self._terminal_size()
        # When the terminal is too narrow for two readable panes, degrade to
        # single-column calm rendering (OI content still tagged inline).
        self.dual = self._term_cols >= 100

    @staticmethod
    def _terminal_size() -> tuple[int, int]:
        try:
            size = shutil.get_terminal_size((120, 30))
            return size.columns, size.lines
        except Exception:
            return 120, 30

    def _refresh_terminal_size(self) -> None:
        """Refresh dimensions before every repaint (SIGWINCH-safe)."""
        self._term_cols, self._term_rows = self._terminal_size()
        self.dual = self._term_cols >= 100

    def _left_width(self) -> int:
        if not self.dual:
            return self._term_cols
        return max(40, (self._term_cols - self.PANE_GAP) // 2)

    def _write(self, text: str, *, end: str = "\n", stream=None) -> None:
        """Keep calm-pane output inside the left pane in wide terminals."""
        if self.dual and self.interactive:
            text = text[: self._left_width()]
        super()._write(text, end=end, stream=stream)

    # ------------------------------------------------------------------
    # Routing: mirror raw events into the OI window
    # ------------------------------------------------------------------
    async def render_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        self.mascot.observe(etype)
        if self.oi_enabled:
            if etype == "ModelDelta":
                self.window.feed_delta(str(payload.get("text") or ""))
            elif etype == "StdoutChunk":
                self.window.feed(str(payload.get("data") or ""))
            elif etype == "StderrChunk":
                data = str(payload.get("data") or "")
                for i, line in enumerate(data.splitlines()):
                    self.window.feed(f"[err] {line}" if line else "[err]")
            elif etype == "ExecutionStarted":
                self.window.feed(f"$ {payload.get('runtime') or 'runtime'}\n")
            elif etype == "CapabilityRequested":
                args = payload.get("arguments") or {}
                code = args.get("code")
                if code:
                    first = str(code).splitlines()[0][:80]
                    self.window.feed(f"$ {first}\n")
        await super().render_event(event)
        if self.oi_enabled and self._raw_event_arrived(etype):
            self.repaint_oi()
        elif etype.startswith("Task") and self.oi_enabled and self.interactive:
            # Mascot state changed on a task-level event; refresh window.
            self.repaint_oi()

    def _raw_event_arrived(self, etype: str) -> bool:
        return etype in {
            "ModelDelta",
            "StdoutChunk",
            "StderrChunk",
            "ExecutionStarted",
            "CapabilityRequested",
            "CapabilityCompleted",
            "ExecutionExited",
        }

    # ------------------------------------------------------------------
    # OI window painting
    # ------------------------------------------------------------------
    MASCOT_WIDTH = 16   # dedicated mascot column inside the OI pane

    def repaint_oi(self) -> None:
        """Repaint the bordered OI region at the bottom of the screen.

        Layout inside the window:  [ live output stream | mascot column ]
        The mascot column is a fixed-width status sidebar driven by kernel
        state; the rest belongs to the unbuffered output stream.
        Uses ANSI save/restore so calm scrollback above is untouched.
        Skipped in non-tty environments (tests use snapshot_oi()).
        """
        if not (self.interactive and getattr(self.output, "isatty", lambda: False)()):
            return
        self._refresh_terminal_size()
        cols = self._term_cols
        rows = self._term_rows
        left_width = self._left_width()
        right_x = left_width + self.PANE_GAP + 1 if self.dual else 1
        right_width = cols - right_x + 1
        stream_w = max(right_width - 4 - self.MASCOT_WIDTH, 10)

        header = "┌─ OI · live ".ljust(stream_w + 2, "─")
        header += "┬" + " ATHENA ".center(self.MASCOT_WIDTH, "─") + "┐"
        footer = ("└" + "─" * (stream_w + 1) + "┴"
                  + "─" * self.MASCOT_WIDTH + "┘")

        body = self.window.snapshot(self.oi_height, stream_w)
        mascot = self.mascot.render(max_width=self.MASCOT_WIDTH)
        # Vertically center the mascot beside the stream.
        pad_top = max((self.oi_height - len(mascot)) // 2, 0)

        out = ["\x1b7"]          # save cursor
        out.append(
            f"\x1b[{rows - self.oi_height - 2};{right_x}H\x1b[K" + header
        )
        for i in range(self.oi_height):
            stream_line = body[i] if i < len(body) else ""
            padded = (stream_line + " " * stream_w)[:stream_w]
            row = f"│ {padded} │"
            m_idx = i - pad_top
            if 0 <= m_idx < len(mascot):
                mline = mascot[m_idx]
                row += (mline + " " * self.MASCOT_WIDTH)[: self.MASCOT_WIDTH]
            else:
                row += " " * self.MASCOT_WIDTH
            out.append(
                f"\x1b[{rows - self.oi_height - 1 + i};{right_x}H\x1b[K{row}"
            )
        out.append(f"\x1b[{rows - 1};{right_x}H\x1b[K" + footer)
        out.append("\x1b8")      # restore cursor
        self.output.write("".join(out))
        self.output.flush()

    def snapshot_oi(self, height: int | None = None) -> list[str]:
        """Test/inspection hook: exact current OI window contents."""
        return self.window.snapshot(height or self.oi_height, self._term_cols)

    # ------------------------------------------------------------------
    # Overrides that must also flush the window
    # ------------------------------------------------------------------
    def finish(self) -> None:
        super().finish()
        self.window.seal_partial()
        self.repaint_oi()

    def render_direct_execution(self, source, result, *, inject_into_context):
        # The mascot TAKES the command (mind -> body -> result -> mind):
        # show the handoff in its column before rendering the card.
        self.mascot.state = "executing"
        self.mascot.object = self.mascot.OBJ_TERMINAL
        self.mascot.speech = f"└── {source[:40]}"
        super().render_direct_execution(source, result, inject_into_context=inject_into_context)
        if self.oi_enabled:
            self.window.feed(f"$ {source}\n")
            self.window.feed(str(result.get("stdout") or ""))
            err = str(result.get("stderr") or "")
            if err:
                for line in err.splitlines():
                    self.window.feed(f"[err] {line}")
            ok = result.get("status") == "completed" and result.get("exit_code") in (0, None)
            self.mascot.state = "done" if ok else "failed"
            self.mascot.object = self.mascot.OBJ_VERIFY if ok else self.mascot.OBJ_FAIL
            self.mascot.speech = ""
            if not ok:
                self.mascot.speech = "That did not work."
            self.repaint_oi()
