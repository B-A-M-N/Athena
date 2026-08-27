"""Dual-pane terminal projection: calm conversation (left) + OI machine (right).

Design (INV-007 preserved): BOTH panes are read-only projections of the same
canonical event stream.  Neither pane executes, approves, or owns state.

Pane ownership (UI mission §4/§24):

* LEFT  — the calm operator surface: user/assistant conversation, final
  explanations, approvals as readable cards, task outcome lines.  It never
  becomes an event firehose; the base :class:`OperatorSurface` coalescing is
  retained unchanged.
* RIGHT — the OI machine: a retro operator-console chassis
  (:mod:`athena.cli.chassis`) framing a structured activity model
  (:mod:`athena.cli.activity`: ACTIVE OPERATION / RECENT ACTIVITY /
  AUTHORIZATION / artifacts / background work), a bounded stream viewport
  (:mod:`athena.cli.stream`), and the semantic buddy
  (:mod:`athena.cli.buddy`) as a first-class reactive component.

Rendering strategy: the right pane repaints its chassis in place with ANSI
cursor save/restore; the left pane writes linearly.  In dumb environments
(pipes, tests) the renderer degrades to sequential tagged lines and exposes
``snapshot_oi()`` for deterministic assertions.

Back-compat: ``Mascot`` and ``_OIWindow`` are retained as thin adapters over
the new :class:`Buddy` and :class:`StreamWindow` so existing consumers
(``oi_stream`` viewer, demos, tests) keep working unchanged.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from athena.cli.activity import ActivityModel
from athena.cli.buddy import Buddy, BuddyState
from athena.cli.chassis import BUDDY_WIDTH, MIN_WIDTH, ChassisView, render_chassis
from athena.cli.stream import StreamLine, StreamWindow
from athena.cli.surface import OperatorSurface

__all__ = ["DualPaneSurface", "Mascot"]


# ---------------------------------------------------------------------------
# Buddy ASCII art — one compact frame per semantic state.
# Drop-in replaceable: set higher-resolution art here without touching the
# state machine.  (Final production artwork is an external asset task; see
# docs/ui-mission-report.md.)
# ---------------------------------------------------------------------------
BUDDY_ART: dict[str, tuple[str, ...]] = {
    BuddyState.IDLE: (
        "   ,___,",
        "   (O,O)",
        "   /)_)",
    ),
    BuddyState.LISTENING: (
        "   ,___,",
        "   (-,-)  …",
        "   /)_)",
    ),
    BuddyState.THINKING: (
        "   ,___,  ∿∿∿",
        "   (@,@) ∿∿∿",
        "   /)_)   ⚡",
    ),
    BuddyState.EXECUTING: (
        "  ▄▄▄▄▄",
        "  █◉_◉█  ▤▓▒",
        "  █ ▽ █ ▸run",
        "  ▀▀▀▀▀",
    ),
    BuddyState.READING: (
        "   ,___,  ▤",
        "   (o,o) ▤▤",
        "   /)_)   read",
    ),
    BuddyState.WAITING: (
        "   ,___,",
        "   (⊙︵⊙)  ⏸",
        "   /)_)   hold",
    ),
    BuddyState.APPROVAL: (
        "  ▄▄▄▄▄",
        "  █⊙‿⊙█  [?]",
        "  █ ⌣ █  may i?",
        "  ‛‛‛‛‛",
    ),
    BuddyState.SUCCESS: (
        "  ▄▄▄▄▄",
        "  █★‿★█  ✓",
        "  █ ◡ █  done",
        "  ▀▀▀▀▀",
    ),
    BuddyState.FAILURE: (
        "  ▄▄▄▄▄",
        "  █✕︵✕█  ✗",
        "  █ ─ █  oops",
        "  ▀▀▀▀▀",
    ),
    BuddyState.INTERRUPTED: (
        "   ,___,",
        "   (✕﹏✕)  ⊘",
        "   /)_)   stop",
    ),
    BuddyState.RECOVERING: (
        "   ,___,  ↻",
        "   (◔,◔) ↻↻",
        "   /)_)   retry",
    ),
}


class Mascot:
    """Back-compat adapter: the classic animated mascot API over ``Buddy``.

    The original multi-frame ASCII sets are preserved verbatim for the
    standalone ``oi-stream`` viewer; the semantic state mapping now flows
    through :class:`athena.cli.buddy.Buddy` so both surfaces agree on what
    the mascot means.
    """

    OBJ_TERMINAL = "[>]"
    OBJ_PROCESS = "[#]"
    OBJ_CODE = "{ }"
    OBJ_VERIFY = "[✓]"
    OBJ_FAIL = "[!]"
    OBJ_APPROVAL = "[?]"
    OBJ_ARTIFACT = "[*]"

    CHARACTERS: dict[str, dict[str, Any]] = {}

    # Legacy frame sets, keyed by legacy state names.  The buddy's semantic
    # states map onto these for display (see _LEGACY_FOR).
    FRAMES: dict[str, tuple[str, str]] = {}
    CAT_FRAMES: dict[str, tuple[str, str]] = {}
    ROBOT_FRAMES: dict[str, tuple[str, str]] = {}

    _LEGACY_FOR = {
        BuddyState.IDLE: "idle",
        BuddyState.LISTENING: "idle",
        BuddyState.THINKING: "thinking",
        BuddyState.EXECUTING: "executing",
        BuddyState.READING: "thinking",
        BuddyState.WAITING: "waiting",
        BuddyState.APPROVAL: "waiting",
        BuddyState.SUCCESS: "done",
        BuddyState.FAILURE: "failed",
        BuddyState.INTERRUPTED: "failed",
        BuddyState.RECOVERING: "thinking",
    }

    def __init__(self, character: str = "owl") -> None:
        _register_legacy_frames()
        self.character = character if character in self.CHARACTERS else "owl"
        self.buddy = Buddy()
        self.object = ""
        self.speech = ""
        self._frame = 0

    # The legacy API exposes ``state`` as a mutable attribute; keep it as a
    # property bridged to the buddy so ``mascot.state = "executing"`` in old
    # call sites keeps working with legacy names.
    _NEW_FOR = {
        "idle": BuddyState.IDLE,
        "thinking": BuddyState.THINKING,
        "executing": BuddyState.EXECUTING,
        "waiting": BuddyState.APPROVAL,
        "done": BuddyState.SUCCESS,
        "failed": BuddyState.FAILURE,
    }

    @property
    def state(self) -> str:  # legacy name
        return self._LEGACY_FOR.get(self.buddy.state, "idle")

    @state.setter
    def state(self, legacy: str) -> None:
        self.buddy._enter(self._NEW_FOR.get(legacy, BuddyState.IDLE))

    def observe(self, event_type: str) -> None:
        self.buddy.observe(event_type)
        if event_type in {"StdoutChunk", "StderrChunk", "ExecutionStarted"}:
            self.object = self.OBJ_TERMINAL
        elif event_type == "CapabilityRequested":
            self.object = self.OBJ_CODE
        elif event_type == "ApprovalRequested":
            self.object = self.OBJ_APPROVAL
            self.speech = "Waiting on you."
        elif event_type == "ApprovalResolved":
            self.object = ""
            self.speech = ""
        elif event_type in {"TaskCompleted", "TaskPartial"}:
            self.object = self.OBJ_VERIFY
        elif event_type == "TaskFailed":
            self.object = self.OBJ_FAIL
            self.speech = "That did not work."

    def render(self, max_width: int = 24) -> list[str]:
        frames = self.CHARACTERS[self.character]["frames"]
        art = frames.get(self.state, frames["idle"])[self._frame % 2]
        self._frame += 1
        self.buddy.tick()
        lines = [ln.rstrip() for ln in art.strip("\n").splitlines()]
        width = max((len(ln) for ln in lines), default=0)
        pad = max(max_width - width, 0)
        left = " " * (pad // 2)
        out = [f"{left}{ln}" for ln in lines]
        tag = f"{self.object} {self.speech}".strip()
        if tag:
            out.append(f"{left}{tag}"[:max_width])
        return out

    def set_character(self, name: str) -> bool:
        if name not in self.CHARACTERS:
            return False
        self.character = name
        return True

    @classmethod
    def available(cls) -> dict[str, str]:
        return {k: v["label"] for k, v in cls.CHARACTERS.items()}


class _OIWindow:
    """Back-compat adapter: the original ring-buffer API over ``StreamWindow``.

    Exposes ``lines`` (plain strings), ``_partial``, ``feed``, ``feed_delta``,
    ``seal_partial`` and ``snapshot`` exactly as before, so the ``oi-stream``
    viewer's stderr-rejoin logic keeps working unchanged — now with CR
    progress handling and ANSI hygiene underneath.
    """

    def __init__(self, *, max_lines: int = 500, width: int | None = None) -> None:
        self._win = StreamWindow(max_lines=max_lines)
        self.width = width

    @property
    def lines(self):  # deque[str] view for legacy callers
        return _LinesView(self._win.lines)

    @property
    def _partial(self) -> str:
        return self._win._partial

    @_partial.setter
    def _partial(self, value: str) -> None:
        self._win._partial = value

    def feed(self, text: str) -> None:
        self._win.feed(text)

    def feed_delta(self, text: str) -> None:
        self._win.feed_delta(text)

    def seal_partial(self) -> None:
        self._win.seal_partial()

    def snapshot(self, height: int, width: int) -> list[str]:
        return self._win.snapshot_text(height, width)


class _LinesView:
    """Minimal deque-of-str facade over deque[StreamLine]."""

    def __init__(self, backing) -> None:
        self._backing = backing

    def __iter__(self):
        return (ln.text for ln in self._backing)

    def __len__(self):
        return len(self._backing)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return [ln.text for ln in list(self._backing)[idx]]
        return list(self._backing)[idx].text

    def __bool__(self):
        return bool(self._backing)


# Legacy frame registration: the multi-frame ASCII sets moved to
# ``athena.cli.buddy_art``-style module data but are kept here to avoid
# breaking imports; populated lazily below.
def _register_legacy_frames() -> None:
    if Mascot.CHARACTERS:
        return
    from athena.cli.mascot_art import CAT_FRAMES, FRAMES, ROBOT_FRAMES

    Mascot.FRAMES = FRAMES
    Mascot.CAT_FRAMES = CAT_FRAMES
    Mascot.ROBOT_FRAMES = ROBOT_FRAMES
    Mascot.CHARACTERS = {
        "owl": {"label": "Athena's owl", "frames": FRAMES},
        "cat": {"label": "Terminal cat", "frames": CAT_FRAMES},
        "bot": {"label": "Little robot", "frames": ROBOT_FRAMES},
    }


# ---------------------------------------------------------------------------
# The dual-pane surface
# ---------------------------------------------------------------------------
class DualPaneSurface(OperatorSurface):
    """Side-by-side: calm conversation (left) + OI machine chassis (right)."""

    PANE_GAP = 3          # columns between panes (borders + gap)
    MIN_DUAL_COLS = 100   # below this, degrade to single-column rendering

    def __init__(self, *args, oi_height: int = 14, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.oi_enabled = True
        self.oi_height = oi_height
        self.window = _OIWindow()
        self.stream: StreamWindow = self.window._win
        self.activity = ActivityModel()
        self.buddy = Buddy()
        self.mascot = Mascot(character=os.environ.get("ATHENA_MASCOT", "owl"))
        self.mascot.buddy = self.buddy  # one semantic machine for both APIs
        self._term_cols, self._term_rows = self._terminal_size()
        self.dual = self._term_cols >= self.MIN_DUAL_COLS
        self._repaints = 0

    # -- terminal geometry ---------------------------------------------------
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
        self.dual = self._term_cols >= self.MIN_DUAL_COLS

    def _left_width(self) -> int:
        if not self.dual:
            return self._term_cols
        return max(40, (self._term_cols - self.PANE_GAP) // 2)

    def _write(self, text: str, *, end: str = "\n", stream=None) -> None:
        """Keep calm-pane output inside the left pane in wide terminals."""
        if self.dual and self.interactive:
            text = text[: self._left_width()]
        super()._write(text, end=end, stream=stream)

    # -- event routing --------------------------------------------------------
    async def render_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})

        # 1. Derived projections: semantic buddy + structured activity.
        self.buddy.observe(etype, payload)
        self.mascot.observe(etype)
        self.activity.observe(etype, payload)

        # 2. Raw stream viewport (right pane).
        if self.oi_enabled:
            self._route_stream(etype, payload)

        # 3. Calm conversation pane (left) — coalesced cards only.  Raw
        # stdout/stderr chunks live exclusively in the machine pane (§4/§25:
        # no cross-pane duplication, no execution firehose in conversation);
        # ``details=True`` re-enables the legacy mirror for debugging.
        if self.oi_enabled and not self.details and etype in {
            "StdoutChunk", "StderrChunk",
        }:
            pass
        else:
            await super().render_event(event)

        # 4. Repaint the machine when anything it shows could have changed.
        if self.oi_enabled and self.interactive and self._oi_visible_event(etype):
            self.repaint_oi()

    def _route_stream(self, etype: str, payload: dict[str, Any]) -> None:
        if etype == "ModelDelta":
            self.stream.feed_delta(str(payload.get("text") or ""))
        elif etype == "StdoutChunk":
            self.stream.feed(str(payload.get("data") or ""))
        elif etype == "StderrChunk":
            self.stream.feed(str(payload.get("data") or ""), err=True)
        elif etype == "ExecutionStarted":
            self.stream.seal_partial()
            self.stream.feed(f"$ {payload.get('runtime') or 'runtime'}\n")
        elif etype == "CapabilityRequested":
            args = payload.get("arguments") or {}
            code = args.get("code")
            if code:
                self.stream.seal_partial()
                first = str(code).splitlines()[0][:80]
                self.stream.feed(f"$ {first}\n")

    def _oi_visible_event(self, etype: str) -> bool:
        """Events that can change the machine's rendered frame."""
        return etype in {
            "ModelDelta", "StdoutChunk", "StderrChunk",
            "ExecutionStarted", "ExecutionExited", "ExecutionTimedOut",
            "ExecutionInterrupted",
            "CapabilityRequested", "CapabilityStarted", "CapabilityProgress",
            "CapabilityCompleted", "CapabilityFailed",
            "ApprovalRequested", "ApprovalResolved",
            "ArtifactCreated", "ChildTaskCreated", "ChildTaskCompleted",
            "PolicyDecisionMade", "TaskBlocked",
        } or etype.startswith("Task")

    # -- right-pane painting ----------------------------------------------------
    def _chassis_lines(self) -> list[str]:
        """Deterministic frame for the current state (pure; also used by tests)."""
        self._refresh_terminal_size()
        cols, rows = self._term_cols, self._term_rows
        if self.dual:
            right_x = self._left_width() + self.PANE_GAP + 1
            width = cols - right_x + 1
        else:
            right_x, width = 1, cols
        height = min(max(self.oi_height + 4, 8), max(rows - 4, 5))
        self.buddy.tick()
        art = [ln[:BUDDY_WIDTH] for ln in BUDDY_ART.get(
            self.buddy.state, BUDDY_ART[BuddyState.IDLE])]
        snap_h = min(6, max(height - 8, 2))
        view = ChassisView(
            activity=self.activity,
            buddy_lines=art,
            buddy_state=self.buddy.state,
            stream=self.stream.snapshot(snap_h, max(width - 4, 12)),
            dropped=self.stream.dropped,
        )
        return render_chassis(view, width, height)

    def repaint_oi(self) -> None:
        """Repaint the machine chassis in place (ANSI save/restore).

        Skipped in non-tty environments; tests use ``chassis_text()`` /
        ``snapshot_oi()``.  Repaints are coalesced by the caller's event
        filter, not by dropping events (INV: presentation-only batching).
        """
        if not (self.interactive and getattr(self.output, "isatty", lambda: False)()):
            return
        lines = self._chassis_lines()
        rows = self._term_rows
        right_x = (self._left_width() + self.PANE_GAP + 1) if self.dual else 1
        top = max(rows - len(lines) - 1, 1)
        out = ["\x1b[s"]
        for i, line in enumerate(lines):
            out.append(f"\x1b[{top + i};{right_x}H\x1b[K{line}")
        out.append("\x1b[u")
        self.output.write("".join(out))
        self.output.flush()
        self._repaints += 1

    # -- inspection hooks (tests / non-tty) --------------------------------------
    def snapshot_oi(self, height: int | None = None) -> list[str]:
        """Exact current stream-viewport contents (legacy test hook)."""
        return self.window.snapshot(height or self.oi_height, self._term_cols)

    def chassis_text(self) -> list[str]:
        """Current full machine frame as text (test/inspection hook)."""
        return self._chassis_lines()

    # -- flush points ---------------------------------------------------------
    def finish(self) -> None:
        super().finish()
        self.stream.seal_partial()
        self.repaint_oi()

    def render_direct_execution(self, source, result, *, inject_into_context):
        # Buddy: mind -> body -> result -> mind for direct `!cmd` escapes.
        self.buddy._enter(BuddyState.EXECUTING)
        self.mascot.object = Mascot.OBJ_TERMINAL
        self.mascot.speech = f"└── {source[:40]}"
        super().render_direct_execution(source, result, inject_into_context=inject_into_context)
        if self.oi_enabled:
            self.stream.feed(f"$ {source}\n")
            self.stream.feed(str(result.get("stdout") or ""))
            err = str(result.get("stderr") or "")
            if err:
                self.stream.feed(err if err.endswith("\n") else err + "\n", err=True)
            ok = result.get("status") == "completed" and result.get("exit_code") in (0, None)
            self.buddy._enter(BuddyState.SUCCESS if ok else BuddyState.FAILURE)
            self.mascot.object = Mascot.OBJ_VERIFY if ok else Mascot.OBJ_FAIL
            self.mascot.speech = "" if ok else "That did not work."
            self.repaint_oi()


_register_legacy_frames()
