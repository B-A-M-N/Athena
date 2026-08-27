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
import time
import textwrap
from collections import deque
from typing import Any, Mapping

from athena.cli.animation import AnimationClock, OIAnimator
from athena.cli.framebuffer import OIFrameBuffer, pillow_available
from athena.cli.input import PromptController
from athena.cli.layout import compute_layout
from athena.cli.projection import OperationNode, ProjectionState
from athena.cli.render.ansi import CellGridDiffRenderer, fit_cells
from athena.cli.render.scene import render_scene_lines
from athena.cli.render.kitty import (
    KittyAsset,
    KittyCapabilityProbe,
    KittyGraphicsProtocol,
    select_renderer,
)
from athena.cli.scene import build_oi_scene
from athena.cli.surface import OperatorSurface
from athena.cli.terminal import TerminalSession, sanitize_terminal_text

__all__ = [
    "DualPaneSurface",
    "Mascot",
    "configure_mascots",
    "resolve_mascot_name",
]


_MASCOT_OFF = {"off", "none", "hide", "disabled"}
_MASCOT_ON = {"on", "show", "enabled"}


def _terminal_text(value: Any) -> str:
    """Turn streamed process text into safe, printable view text.

    The event log keeps the original payload.  This lossy cleanup belongs only
    to the terminal projection: ANSI control sequences and carriage-return
    progress updates must not corrupt a retained pane.
    """
    return sanitize_terminal_text(value)


def resolve_mascot_name(value: str | None = None) -> str:
    """Resolve the active mascot: explicit value > ``ATHENA_MASCOT`` env > owl."""
    name = (value or os.environ.get("ATHENA_MASCOT") or "owl").strip().lower()
    return name or "owl"


def configure_mascots(definitions: Mapping[str, Any] | None) -> None:
    """Register user-defined mascot characters (``[mascots.<name>]`` in TOML).

    Each definition is a mapping with an optional ``label`` and a ``frames``
    table mapping state names to one or two art strings. Invalid entries are
    skipped so a broken config can never take the CLI down.
    """
    for name, spec in (definitions or {}).items():
        if not isinstance(spec, Mapping):
            continue
        Mascot.register_character(
            str(name),
            str(spec.get("label") or name),
            spec.get("frames") or {},
        )


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

    # Semantic states are intentionally richer than the small built-in art
    # set.  Characters may provide bespoke frames for any of these states;
    # otherwise the nearest built-in frame is used while the textual state
    # remains truthful.
    STATES = (
        "idle", "listening", "thinking", "responding", "inspecting",
        "searching", "reading", "coding", "tools", "executing", "waiting",
        "approval", "delegated", "success", "warning", "failure",
        "interrupted", "recovering",
    )
    _FRAME_FALLBACKS = {
        "listening": "idle",
        "thinking": "thinking",
        "responding": "thinking",
        "inspecting": "thinking",
        "searching": "executing",
        "reading": "thinking",
        "coding": "executing",
        "tools": "executing",
        "approval": "waiting",
        "delegated": "executing",
        "success": "done",
        "warning": "failed",
        "failure": "failed",
        "interrupted": "failed",
        "recovering": "thinking",
    }

    # ------------------------------------------------------------------
    # Character registry. Each entry: {"label", "frames": {state: (a, b)}}
    # Two animation frames per state; ``advance`` alternates them. Rendering is
    # side-effect free so a static terminal never invents animation progress.
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
        if not self.CHARACTERS:
            self._register_characters()
        self.character = character if character in self.CHARACTERS else "owl"
        self.state = "idle"
        self.object = ""       # carried activity object, e.g. "[>]"
        self.speech = ""       # short deterministic operational line
        self._frame = 0
        self._phase = 0.0

    @classmethod
    def _register_characters(cls) -> None:
        cls.CHARACTERS = {
            "owl": {"label": "Athena's owl", "frames": cls.FRAMES},
            "cat": {"label": "Terminal cat", "frames": cls.CAT_FRAMES},
            "bot": {"label": "Little robot", "frames": cls.ROBOT_FRAMES},
        }

    def observe(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        """Derive expressive state from a canonical event.

        There is no timer-driven fake lifecycle here.  A state changes only
        when Athena emits the corresponding event; the renderer may animate a
        frame, but it never invents progress or authority.
        """
        payload = payload or {}
        if event_type in {"TaskCreated", "TaskQueued"}:
            self.state, self.object, self.speech = "listening", "", "Ready when you are."
        elif event_type == "TaskStarted":
            self.state, self.object, self.speech = "thinking", "", "Working through it."
        elif event_type in {"ContextBuildStarted", "ContextBuilt"}:
            self.state, self.object = "inspecting", "[?]"
        elif event_type == "ContextCompressed":
            self.state, self.object, self.speech = "inspecting", "[?]", "Keeping the thread compact."
        elif event_type in {"ModelRequestStarted", "ModelReasoningDelta"}:
            self.state, self.object, self.speech = "thinking", "", "Thinking…"
        elif event_type == "ModelDelta":
            self.state, self.object, self.speech = "responding", "", ""
        elif event_type == "ModelRequestFailed":
            self.state, self.object, self.speech = "failure", self.OBJ_FAIL, "The model needs attention."
        elif event_type == "ModelResponseCompleted":
            self.state, self.speech = "tools" if payload.get("tool_calls") else "responding", ""
        elif event_type in {"SearchStarted", "ResearchStarted"}:
            self.state, self.object = "searching", self.OBJ_TERMINAL
        elif event_type in {"FileRead", "InspectionStarted"}:
            self.state, self.object = "reading", self.OBJ_ARTIFACT
        elif event_type == "CapabilityRequested":
            capability = str(payload.get("capability_id") or "")
            self.state = "coding" if capability in {"execute", "tools.execute", "computer.execute"} else "tools"
            self.object = self.OBJ_CODE
            self.speech = ""
        elif event_type == "CapabilityValidated":
            self.state, self.object = "tools", self.OBJ_CODE
        elif event_type == "PolicyDecisionMade":
            decision = str(payload.get("decision") or "").lower()
            if decision in {"deny", "denied"}:
                self.state, self.object = "warning", self.OBJ_FAIL
            else:
                self.state, self.object = "tools", self.OBJ_CODE
        elif event_type in {"CapabilityStarted", "CapabilityProgress", "ExecutionStarted", "StdoutChunk", "StderrChunk"}:
            self.state, self.object, self.speech = "executing", self.OBJ_TERMINAL, ""
        elif event_type == "ApprovalRequested":
            self.state, self.object, self.speech = "approval", self.OBJ_APPROVAL, "Waiting on you."
        elif event_type == "ApprovalResolved":
            decision = str(payload.get("decision") or payload.get("status") or "").lower()
            if decision in {"denied", "deny", "rejected"}:
                self.state, self.object, self.speech = "warning", self.OBJ_FAIL, "Approval denied."
            else:
                self.state, self.object, self.speech = "executing", self.OBJ_TERMINAL, ""
        elif event_type == "ArtifactCreated":
            self.state, self.object, self.speech = "success", self.OBJ_ARTIFACT, "Artifact ready."
        elif event_type in {
            "ChildTaskCreated", "DelegationStarted", "BackgroundTaskStarted",
        }:
            self.state, self.object, self.speech = "delegated", self.OBJ_PROCESS, "Delegated work is active."
        elif event_type in {"ChildTaskCompleted", "BackgroundTaskCompleted"}:
            self.state, self.object, self.speech = "success", self.OBJ_VERIFY, "Delegated work returned."
        elif event_type == "BackgroundTaskFailed":
            self.state, self.object, self.speech = "failure", self.OBJ_FAIL, "Background work needs attention."
        elif event_type in {"ToolRepaired", "MutationRecorded", "MemoryWritten", "SkillActivated"}:
            self.state, self.object, self.speech = "tools", self.OBJ_CODE, "Recording the next step."
        elif event_type in {"MutationRecordFailed", "ToolInputCorrectionExhausted"}:
            self.state, self.object, self.speech = "failure", self.OBJ_FAIL, "That needs attention."
        elif event_type in {
            "MemoryCandidateCreated", "SkillCandidateCreated",
            "InterpreterProposalDispatched", "RuntimeSessionCreated",
        }:
            self.state, self.object, self.speech = "tools", self.OBJ_CODE, "Recording the next step."
        elif event_type == "TaskStateChanged":
            state = str(payload.get("status") or payload.get("to") or "").upper()
            mapping = {
                "WAITING_APPROVAL": ("approval", self.OBJ_APPROVAL, "Waiting on you."),
                "WAITING_INPUT": ("waiting", self.OBJ_PROCESS, "Waiting for input."),
                "BLOCKED": ("warning", self.OBJ_FAIL, "Blocked; needs attention."),
                "RECOVERY_REQUIRED": ("recovering", self.OBJ_PROCESS, "Restoring the run."),
                "RUNNING": ("executing", self.OBJ_TERMINAL, "Working."),
            }
            if state in mapping:
                self.state, self.object, self.speech = mapping[state]
        elif event_type in {"ExecutionExited", "CapabilityCompleted", "TaskCompleted", "TaskPartial"}:
            if event_type == "TaskPartial":
                self.state, self.object, self.speech = "warning", self.OBJ_FAIL, "Needs a follow-up."
            else:
                ok = event_type != "ExecutionExited" or payload.get("exit_code") in {None, 0}
                self.state = "success" if ok else "failure"
                self.object = self.OBJ_VERIFY if ok else self.OBJ_FAIL
                self.speech = "Complete." if ok else "That needs attention."
        elif event_type in {"ExecutionTimedOut", "CapabilityFailed", "TaskFailed", "TaskBlocked"}:
            self.state, self.object, self.speech = "failure", self.OBJ_FAIL, "That needs attention."
        elif event_type in {"ExecutionInterrupted", "TaskCancelled", "TaskInterrupted"}:
            self.state, self.object, self.speech = "interrupted", self.OBJ_FAIL, "Stopped safely."
        elif event_type == "RecoveryStarted":
            self.state, self.object, self.speech = "recovering", self.OBJ_PROCESS, "Restoring the run."
        elif event_type == "RecoveryCompleted":
            self.state, self.object, self.speech = "listening", self.OBJ_VERIFY, "Recovery complete."

    def render(self, max_width: int = 24) -> list[str]:
        if not Mascot.CHARACTERS:
            Mascot._register_characters()
        frames = self.CHARACTERS[self.character]["frames"]
        frame_state = self.state if self.state in frames else self._FRAME_FALLBACKS.get(self.state, "idle")
        art = frames.get(frame_state, frames["idle"])[self._frame % 2]
        # Every emitted line is hard-bounded to max_width so the mascot can
        # never overflow its column and break the pane layout.
        lines = [
            ln.rstrip()[:max_width] for ln in art.strip("\n").splitlines()
        ]
        width = max((len(ln) for ln in lines), default=0)
        pad = max(max_width - width, 0)
        left = " " * (pad // 2)
        out = [f"{left}{ln}" for ln in lines]
        # Carried object + speech ride under the character.
        tag = f"{self.object} {self.speech}".strip()
        if tag:
            out.append(f"{left}{tag}"[:max_width])
        return out

    def advance(self, dt: float = 0.1) -> bool:
        """Advance presentation phase only; semantic state comes from events."""
        if dt <= 0:
            return False
        self._phase = (self._phase + dt * 2.0) % 1.0
        self._frame = int(self._phase * 2) % 2
        return True

    @classmethod
    def register_character(
        cls, name: str, label: str, frames: Mapping[str, Any]
    ) -> bool:
        """Register (or replace) a custom character; False on invalid input.

        ``frames`` maps a state name (idle, thinking, executing, waiting,
        done, failed) to one art string or a ``[frame_a, frame_b]`` pair.
        An ``idle`` frame is required; states without art fall back to idle.
        """
        if not cls.CHARACTERS:
            cls._register_characters()
        name = str(name).strip().lower()
        if not name or not isinstance(frames, Mapping):
            return False
        normalized: dict[str, tuple[str, str]] = {}
        for state, pair in frames.items():
            if isinstance(pair, str):
                normalized[str(state)] = (pair, pair)
            elif isinstance(pair, (list, tuple)) and pair:
                first = str(pair[0])
                second = str(pair[1]) if len(pair) > 1 else first
                normalized[str(state)] = (first, second)
        if "idle" not in normalized:
            return False
        cls.CHARACTERS[name] = {"label": str(label or name), "frames": normalized}
        return True

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
        self._partial += _terminal_text(text)
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
        self._partial += _terminal_text(text)

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
    """A retained two-pane operator surface.

    The left pane is a conversation projection.  The right pane is a compact
    operational projection of the same canonical event stream.  No execution,
    policy, approval, task, or persistence authority lives here.
    """

    PANE_GAP = 3
    _REPAINT_INTERVAL = 0.04

    def __init__(
        self,
        *args,
        oi_height: int = 12,
        mascot: str | None = None,
        display: str | None = None,
        model_label: str | None = None,
        animations: bool = True,
        reduced_motion: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.oi_enabled = True
        self.oi_height = oi_height
        self.window = _OIWindow()
        self.mascot = Mascot(character="owl")
        self.mascot_enabled = True
        self.set_mascot(resolve_mascot_name(mascot))
        self._term_cols, self._term_rows = self._terminal_size()
        self.display_requested = str(display or os.environ.get("ATHENA_DISPLAY") or "auto").lower()
        self.model_label = model_label or os.environ.get(
            "OPENROUTER_MODEL", "local / fake-1"
        )
        self.layout = compute_layout(self._term_cols, self._term_rows, self.display_requested)
        self._kitty_confirmed = os.environ.get("ATHENA_KITTY_CONFIRMED", "").lower() in {
            "1", "true", "yes"
        }
        self.display = select_renderer(
            self.display_requested,
            capability_confirmed=self._kitty_confirmed and pillow_available(),
        )
        self.dual = self.layout.mode.value != "plain"
        self._full_screen = self._supports_full_screen()
        self.projection = ProjectionState()
        self.scene = build_oi_scene(self.projection, self.layout.oi)
        self.animator = OIAnimator(reduced_motion=reduced_motion)
        self.animation_clock = AnimationClock(
            self._animation_tick,
            enabled=animations,
            reduced_motion=reduced_motion,
        )
        self.frame_renderer = CellGridDiffRenderer(self.output)
        self.terminal_session = TerminalSession(self.output, enabled=self._full_screen)
        self.oi_framebuffer = OIFrameBuffer()
        self.kitty = KittyGraphicsProtocol()
        self._glass_frame_id = 40
        self._prompt_text = ""
        self.prompt = PromptController(
            input_fn=self._input_fn if self._input_supplied else None,
            output=self.output,
        )
        self._last_assistant = ""
        self._last_paint = 0.0
        self._left_scroll = 0
        self._right_scroll = 0

    # Compatibility aliases are read-only views into the canonical projection.
    # No surface-local semantic state is stored here.
    @property
    def _chat(self) -> deque[dict[str, str]]:
        return self.projection.chat

    @property
    def _operations(self) -> dict[str, OperationNode]:
        return self.projection.operations

    @property
    def _execution_to_operation(self) -> dict[str, str]:
        return self.projection.execution_to_operation

    @property
    def _recent(self) -> deque[tuple[str, str]]:
        return self.projection.recent

    @property
    def _pending_approval(self) -> dict[str, Any] | None:
        return self.projection.pending_approval

    @property
    def _status(self) -> str:
        return self.projection.status

    @property
    def _status_message(self) -> str:
        return self.projection.status_message

    @property
    def _thinking(self) -> bool:
        return self.projection.thinking

    @property
    def _active_operation_id(self) -> str | None:
        return self.projection.active_operation_id

    @property
    def _last_operation_id(self) -> str | None:
        return self.projection.last_operation_id

    def open(self) -> None:
        """Enter the composed terminal surface once for the REPL lifetime."""
        if self._full_screen:
            self.terminal_session.open()
            if (
                self.display_requested in {"auto", "glass"}
                and not self._kitty_confirmed
            ):
                self._kitty_confirmed = KittyCapabilityProbe.probe(
                    self.output, self.prompt.stdin
                )
                self.display = select_renderer(
                    self.display_requested,
                    capability_confirmed=self._kitty_confirmed and pillow_available(),
                )
            self.animation_clock.start()
            self.repaint_oi(force=True)

    def close(self) -> None:
        """Stop animation and restore the terminal, even after partial setup."""
        self.animation_clock.stop()
        self._close_terminal()

    async def aclose(self) -> None:
        """Async teardown that waits for the animation task to exit."""
        await self.animation_clock.stop_async()
        self._close_terminal()

    def _close_terminal(self) -> None:
        cleanup = self.kitty.cleanup()
        if cleanup and self.terminal_session.active:
            self.output.write(cleanup)
            self.output.flush()
        self.terminal_session.close()

    def _animation_tick(self, dt: float) -> None:
        # ANSI/plain surfaces are event-driven cell projections; repainting
        # them ten times per second only burns terminal bandwidth because the
        # cell scene has no animated layer.  The retained pixel scene owns the
        # animation clock when Glass is actually active.
        if not self._full_screen or self.display != "glass":
            return
        self.mascot.advance(dt)
        self.animator.tick(dt)
        self.repaint_oi(force=True)

    def read_prompt(self, prompt: str = "athena> ") -> str:
        value = self.prompt.read(prompt)
        self._prompt_text = _terminal_text(value)
        if self._full_screen:
            self.repaint_oi(force=True)
        return value

    def set_mascot(self, name: str) -> bool:
        """Switch the mascot character or toggle its column."""
        normalized = (name or "").strip().lower()
        if normalized in _MASCOT_OFF:
            self.mascot_enabled = False
            return True
        if normalized in _MASCOT_ON:
            self.mascot_enabled = True
            return True
        if self.mascot.set_character(normalized):
            self.mascot_enabled = True
            return True
        return False

    @staticmethod
    def _terminal_size() -> tuple[int, int]:
        try:
            size = shutil.get_terminal_size((120, 30))
            return max(size.columns, 1), max(size.lines, 1)
        except Exception:
            return 120, 30

    def _supports_full_screen(self) -> bool:
        return bool(
            self.interactive
            and getattr(self.output, "isatty", lambda: False)()
            and self.dual
            and self._term_rows >= 16
        )

    def _refresh_terminal_size(self) -> None:
        self._term_cols, self._term_rows = self._terminal_size()
        self.layout = compute_layout(self._term_cols, self._term_rows, self.display_requested)
        self.dual = self.layout.mode.value != "plain"
        self._full_screen = self._supports_full_screen()
        self.scene = build_oi_scene(self.projection, self.layout.oi)

    def _left_width(self) -> int:
        if not self.dual:
            return self._term_cols
        return self.layout.operator.width

    def _write(self, text: str, *, end: str = "\n", stream=None) -> None:
        """Keep inherited line-mode rendering out of a composed TTY frame."""
        if self._full_screen:
            return
        super()._write(text[: self._left_width()] if self.dual else text, end=end, stream=stream)

    # -- presentation helpers -------------------------------------------
    def render_idle(self) -> None:
        if not self._full_screen:
            super().render_idle()
            return
        self.projection.status = "READY"
        self.projection.status_message = "Type a request below."
        self.repaint_oi(force=True)

    def render_user_message(self, text: str) -> None:
        text = _terminal_text(text).strip()
        if not text:
            return
        if not self._full_screen:
            super().render_user_message(text)
            return
        self.projection.add_chat("user", text)
        self._left_scroll = self._right_scroll = 0
        self.projection.status = "THINKING"
        self.projection.status_message = "Athena is reading your request."
        self.repaint_oi(force=True)

    def render_result(self, summary: str = "", *, status: str | None = None) -> None:
        if not self._full_screen:
            super().render_result(summary, status=status)
            return
        self._flush_all()
        summary = _terminal_text(summary).strip()
        if summary and summary != self._last_assistant:
            self._append_chat("assistant", summary)
            self._last_assistant = summary
        if status:
            self.projection.status = str(status).upper()
            self.projection.status_message = "Task finished; send another request when ready."
        self.repaint_oi(force=True)

    def render_notice(self, text: str, *, status: str | None = None) -> None:
        if not self._full_screen:
            super().render_notice(text, status=status)
            return
        notice = _terminal_text(text)
        self.projection.status_message = notice
        if notice:
            self.projection.add_recent("·", notice)
        if status:
            self.projection.status = str(status).upper()
        self.repaint_oi(force=True)

    def _append_chat(self, role: str, text: str) -> None:
        text = _terminal_text(text).strip()
        if text:
            self.projection.add_chat(role, text)

    def set_prompt(self, text: str) -> None:
        self._prompt_text = _terminal_text(text)
        if self._full_screen:
            self.repaint_oi(force=True)

    def scroll(self, pane: str, amount: int) -> bool:
        """Move a pane viewport without changing its retained history.

        The REPL exposes this as ``/scroll`` so operators can inspect history
        even on terminals where mouse reporting is unavailable.  Zero means
        follow live output again.
        """
        pane = str(pane or "").strip().lower()
        if pane not in {"left", "right", "chat", "oi"}:
            return False
        amount = int(amount)
        if pane in {"left", "chat"}:
            self._left_scroll = max(0, self._left_scroll + amount)
        else:
            self._right_scroll = max(0, self._right_scroll + amount)
        if self._full_screen:
            self.repaint_oi(force=True)
        return True

    def scroll_to_bottom(self, pane: str) -> bool:
        pane = str(pane or "").strip().lower()
        if pane in {"left", "chat"}:
            self._left_scroll = 0
        elif pane in {"right", "oi"}:
            self._right_scroll = 0
        else:
            return False
        if self._full_screen:
            self.repaint_oi(force=True)
        return True

    def _flush_model(self) -> None:
        if not self._model_text:
            return
        if self._full_screen:
            self._append_chat("assistant", self._model_text)
            self._last_assistant = _terminal_text(self._model_text).strip()
            self._model_text = ""
            return
        super()._flush_model()

    def _render_capability_request(self, payload: dict[str, Any]) -> None:
        if not self._full_screen:
            super()._render_capability_request(payload)

    def _render_approval(self, payload: dict[str, Any]) -> None:
        if not self._full_screen:
            super()._render_approval(payload)

    # -- canonical event projection -------------------------------------
    def _ingest_event(self, etype: str, payload: dict[str, Any]) -> None:
        """Reduce canonical events once, then update presentation-only tails."""
        self.projection.reduce(etype, payload)
        self.mascot.observe(etype, payload)
        if etype == "ExecutionStarted":
            self.window.feed(
                f"$ {_terminal_text(payload.get('runtime') or 'runtime')}\n"
            )
        elif etype in {"StdoutChunk", "StderrChunk"}:
            data = _terminal_text(payload.get("data"))
            if data:
                self.window.feed(
                    ("[err] " if etype == "StderrChunk" else "") + data
                )

    async def render_event(self, event: Any) -> None:
        etype = str(getattr(event, "type", ""))
        payload = dict(getattr(event, "payload", {}) or {})
        if self._full_screen:
            self._refresh_terminal_size()
        self._ingest_event(etype, payload)

        # In a composed TTY, details mode means expandable thinking content,
        # not raw token output.  Preserve the inherited buffer semantics.
        old_details = self.details
        if self._full_screen and etype == "ModelDelta" and old_details:
            self.details = False
        try:
            await super().render_event(event)
        finally:
            self.details = old_details

        if self._full_screen:
            important = etype not in {"ModelDelta", "StdoutChunk", "StderrChunk"}
            self.repaint_oi(force=important)

    async def choose_approval(self, event: Any) -> Any:
        choice = await super().choose_approval(event)
        if self._full_screen:
            self.projection.acknowledge_approval(
                granted=choice.granted,
                scope=choice.scope,
            )
            self.repaint_oi(force=True)
        return choice

    # -- frame composition ----------------------------------------------
    @staticmethod
    def _fit(text: Any, width: int) -> str:
        width = max(int(width), 0)
        text = _terminal_text(text).replace("\n", " ")
        return fit_cells(text, width)

    @classmethod
    def _wrap(cls, text: str, width: int) -> list[str]:
        width = max(width, 1)
        text = _terminal_text(text)
        result: list[str] = []
        for raw in text.splitlines() or [""]:
            if not raw:
                result.append("")
                continue
            result.extend(textwrap.wrap(
                raw,
                width=width,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""])
        return result or [""]

    def _left_lines(self, height: int, width: int) -> list[str]:
        lines = ["CHAT LOG", "─" * min(width, 18)]
        for entry in self._chat:
            label = "YOU" if entry["role"] == "user" else "ATHENA"
            wrapped = self._wrap(entry["text"], max(width - 9, 1))
            for index, text in enumerate(wrapped):
                lines.append(f"{label:<7} {text}" if index == 0 else f"{'':7} {text}")
            lines.append("")
        if self._model_text:
            lines.append("ATHENA  responding…")
            for text in self._wrap(self._model_text, max(width - 9, 1)):
                lines.append(f"{'':7} {text}")
        elif self._thinking:
            lines.append("ATHENA  thinking…")
            if self.details:
                lines.extend([
                    "         ┌ reasoning ─────────────────",
                    "         │ provider reasoning is active",
                    "         └───────────────────────────",
                ])
            else:
                lines.append("         (details hidden · /details to expand)")
        stop = len(lines) - self._left_scroll if self._left_scroll else len(lines)
        visible = lines[max(0, stop - height):stop]
        return [self._fit(line, width) for line in visible] + ["" for _ in range(max(0, height - len(visible)))]

    @staticmethod
    def _glyph(state: str) -> str:
        return {
            "complete": "✓", "success": "✓", "failed": "!", "failure": "!",
            "approval": "?", "running": "●", "interrupted": "!",
        }.get(state, "·")

    def _operation_history_lines(self) -> list[str]:
        """Return compact completed/parked operation rows.

        The active operation is deliberately excluded.  This keeps the right
        pane honest about what is running now while retaining a short answer
        to "what just happened?" without replaying the event firehose.
        """
        rows = ["OPERATION HISTORY"]
        history = [
            operation
            for operation in reversed(list(self._operations.values()))
            if operation.id != self._active_operation_id
        ][:4]
        if not history:
            rows.append("· no completed operations")
            return rows
        for operation in history:
            line = f"{self._glyph(operation.state)} {operation.label}  {operation.state.upper()}"
            if operation.target:
                line += f" · {operation.target}"
            if operation.artifact:
                line += f" · artifact {operation.artifact}"
            rows.append(line)
        return rows

    def _active_lines(self, height: int, width: int) -> list[str]:
        active_lines: list[str] = []
        active = self._operations.get(self._active_operation_id or "")
        if active:
            active_lines.extend([
                "ACTIVE OPERATION",
                f"{self._glyph(active.state)} {active.label}  {active.state.upper()}",
            ])
            if active.target:
                active_lines.append(f"target  {active.target}")
            if active.command:
                active_lines.append(f"> {active.command}")
            if active.progress:
                active_lines.append(f"progress  {active.progress}")
            active_lines.extend(f"stderr  {item}" for item in list(active.error)[-2:])
            active_lines.extend(f"stdout  {item}" for item in list(active.output)[-2:])
            if active.artifact:
                active_lines.append(f"artifact  {active.artifact}")
        else:
            active_lines.extend(["ACTIVE OPERATION", "· no capability is running"])

        approval_lines: list[str] = []
        if self._pending_approval:
            approval = self._pending_approval
            approval_lines.extend(["", "APPROVAL REQUIRED"])
            label = active.label if active else approval.get("capability_id") or "capability"
            approval_lines.append(f"? {label}  PAUSED")
            target = (
                active.target if active
                else approval.get("target") or approval.get("resource") or approval.get("path") or ""
            )
            if target:
                approval_lines.append(f"target  {target}")
            reason = approval.get("reason") or approval.get("policy_reason") or (active.detail if active else "")
            if reason:
                approval_lines.append(f"reason  {reason}")
            scopes = [str(scope) for scope in approval.get("scopes") or ()]
            choices = " ".join(f"{index}:{scope}" for index, scope in enumerate(scopes, 1))
            approval_lines.append(f"keys  {choices or '1:allow'} d:deny")
            approval_lines.append("paused · choose a scope")

        secondary: list[str] = [""]
        secondary.extend(self._operation_history_lines())
        secondary.extend(["", "RECENT ACTIVITY"])
        secondary.extend(f"{glyph} {text}" for glyph, text in list(self._recent)[-6:])
        secondary.extend(["", "LIVE STREAM"])
        for item in self.window.snapshot(min(5, max(height, 1)), max(width - 2, 1))[-5:]:
            if item.strip():
                secondary.append(f"│ {item}")

        def expanded(lines: list[str]) -> list[str]:
            output: list[str] = []
            for line in lines:
                output.extend(self._wrap(line, width))
            return output

        critical = expanded(active_lines + approval_lines)
        tail = expanded(secondary)
        if self._right_scroll:
            # Explicit history navigation is allowed to inspect the complete
            # projection, including older active/history sections.
            all_lines = critical + tail
            stop = len(all_lines) - self._right_scroll
            visible = all_lines[max(0, stop - height):stop]
        else:
            # At the live edge, keep the current operation and approval in
            # view.  Only lower-priority history/stream detail is trimmed.
            if len(critical) >= height:
                visible = critical[:height]
            else:
                visible = critical + tail[-(height - len(critical)):]
        return [self._fit(line, width) for line in visible] + ["" for _ in range(max(0, height - len(visible)))]

    def _scene_lines(self, height: int, width: int) -> list[str]:
        """Render the live OI as a bounded scene, not a second dashboard.

        The scene deliberately uses the same fixed OI rectangle as the Glass
        framebuffer.  The ANSI renderer only changes how that rectangle is
        expressed; Buddy is placed over the scene and never gets a reserved
        column.  Detailed operation/history text remains available through
        the independent right-pane scroll view.
        """
        return render_scene_lines(
            self.projection,
            self.scene,
            width=width,
            height=height,
            buddy_lines=(
                [f"BUDDY · {self.mascot.state.upper()}"]
                + self.mascot.render(max_width=min(20, width))
                if self.mascot_enabled
                else ()
            ),
            buddy_enabled=self.mascot_enabled,
            recent=self._recent,
        )

    def _right_lines(self, height: int, width: int) -> list[str]:
        if self.display == "glass":
            # Kitty paints the framebuffer into this fixed cell rectangle.
            # Keep the ANSI layer empty so it cannot overwrite the image.
            return [""] * height
        if self._right_scroll:
            return self._active_lines(height, width)
        return self._scene_lines(height, width)

    def _frame_lines(self) -> list[str]:
        self._refresh_terminal_size()
        layout = self.layout
        cols, rows = layout.columns, layout.rows
        if layout.mode.value == "plain":
            return [self._fit(f"ATHENA  {self.projection.status_message}", cols)]

        self.scene = build_oi_scene(self.projection, layout.oi)
        self.animator.set_state(self.mascot.state, self.scene.buddy_anchor)
        lines = [" " * cols for _ in range(rows)]
        left_w, right_w = layout.operator.width, layout.oi.width
        lines[0] = self._fit("ATHENA  //  OPERATOR INSTRUMENT", cols)
        lines[1] = self._fit(
            f"STATUS {self.projection.status:<13}  ·  local-first / event projection  ·  {self.display.upper()}",
            cols,
        )
        lines[2] = self._fit("─" * max(cols - 4, 1), cols)

        op_y, op_h = layout.operator.y, layout.operator.height
        op_inner_h, oi_inner_h = max(op_h - 3, 1), max(layout.oi.height - 2, 1)
        op_inner_w, oi_inner_w = max(left_w - 2, 1), max(right_w - 2, 1)
        left = self._left_lines(op_inner_h, op_inner_w)
        right = self._right_lines(oi_inner_h, oi_inner_w)
        left_title = "CONVERSATION  ·  history" if self._left_scroll else "CONVERSATION  ·  calm transcript"
        right_title = "OI // HISTORY" if self._right_scroll else "ATHENA OI // GLASS COMPUTE"
        cabinet_x = max(layout.operator.x - 1, 0)
        seam = max(self.PANE_GAP - 2, 0)
        prefix = " " * cabinet_x

        # The two apertures are recesses in one instrument chassis.  Keep the
        # seam deliberately quiet: a conventional TUI box around each side
        # makes the surface read as two unrelated applications.  The block
        # seam is cabinet relief, not a third pane.
        seam_fill = "░" * max(seam, 1)

        def cabinet_row(left_value: str, right_value: str) -> str:
            left_panel = "▐" + self._fit(left_value, op_inner_w) + "▌"
            right_panel = "▐" + self._fit(right_value, oi_inner_w) + "▌"
            return prefix + "│" + left_panel + seam_fill + right_panel + "│"

        top = (
            prefix + "╭" + "─" * (left_w + len(seam_fill) + right_w + 2) + "╮"
        )
        bottom = (
            prefix + "╰" + "─" * (left_w + len(seam_fill) + right_w + 2) + "╯"
        )
        lines[op_y] = self._fit(top, cols)
        lines[op_y + 1] = self._fit(cabinet_row(left_title, right_title), cols)
        for index in range(op_inner_h):
            row = op_y + 2 + index
            lines[row] = self._fit(
                cabinet_row(
                    left[index] if index < len(left) else "",
                    right[index] if index < len(right) else "",
                ),
                cols,
            )
        lines[op_y + op_h - 1] = self._fit(bottom, cols)

        controls_y = layout.controls.y
        lamps = "SYS ●   NET ●   IO ●   MODEL " + self._fit(self.model_label, 20) + f"   TASK {self.projection.status}"
        lines[controls_y] = self._fit("─" * max(cols - 4, 1), cols)
        lines[controls_y + 1] = self._fit(lamps, cols)
        prompt_y = layout.prompt.y
        prompt = self._prompt_text or "type a request · /help for controls"
        lines[prompt_y] = self._fit("─" * max(cols - 4, 1), cols)
        if prompt_y + 1 < rows:
            lines[prompt_y + 1] = self._fit(f"❯ {prompt}    Ctrl-C cancel · Ctrl-D exit", cols)
        return lines

    def repaint_oi(self, *, force: bool = False) -> None:
        """Paint a complete frame, eliminating stale overlay borders."""
        if not (self.interactive and getattr(self.output, "isatty", lambda: False)()):
            return
        self._refresh_terminal_size()
        if not self._full_screen:
            return
        now = time.monotonic()
        if not force and now - self._last_paint < self._REPAINT_INTERVAL:
            return
        self._last_paint = now
        frame = self._frame_lines()
        self.frame_renderer.draw(frame, columns=self._term_cols)
        if self.display == "glass":
            self._present_glass()

    def _present_glass(self) -> None:
        """Present the OI framebuffer inside the already-painted right CRT."""
        viewport = self.layout.oi
        frame = self.oi_framebuffer.render(
            self.scene,
            self.animator.visual,
            max(viewport.width * 10, 80),
            max((viewport.height - 2) * 20, 60),
        )
        if frame is None:
            return
        # Reuse one Kitty image identity for the fixed CRT placement.  The
        # transmit action replaces that image's data, so an animation tick
        # does not accumulate placements or emit a delete for the previous
        # frame.  The protocol object owns the single identity until close().
        asset = KittyAsset(self._glass_frame_id, frame.png)
        command = self.kitty.present(
            asset,
            x=viewport.x + 1,
            y=viewport.y + 1,
            columns=max(viewport.width - 2, 1),
            rows=max(viewport.height - 2, 1),
        )
        # Kitty placements with C=1 leave the cursor at the placement origin.
        # Put it back on the integrated prompt before line input resumes.
        command += f"\x1b[{self.layout.prompt.y + 2};1H"
        self.output.write(command)
        self.output.flush()

    def snapshot_oi(self, height: int | None = None) -> list[str]:
        return self.window.snapshot(height or self.oi_height, self._term_cols)

    def finish(self) -> None:
        # Compatibility alias: task flush must not tear down the shared REPL
        # terminal or stop its prompt/animation lifecycle.
        self.flush_task()

    def flush_task(self) -> None:
        super().finish()
        self.window.seal_partial()
        if self._full_screen:
            self.repaint_oi(force=True)

    def render_direct_execution(self, source, result, *, inject_into_context):
        call_id = f"direct-{len(self.projection.operations)}"
        if self._full_screen:
            request_payload = {
                "call_id": call_id,
                "capability_id": "execute",
                "arguments": {"code": source},
            }
            self.projection.reduce("CapabilityRequested", request_payload)
            self.projection.reduce(
                "CapabilityStarted",
                {"call_id": call_id, "capability_id": "execute"},
            )
        super().render_direct_execution(source, result, inject_into_context=inject_into_context)
        if self.oi_enabled:
            stdout = _terminal_text(result.get("stdout"))
            stderr = _terminal_text(result.get("stderr"))
            self.window.feed(f"$ {_terminal_text(source)}\n")
            if stdout:
                self.window.feed(stdout)
            if stderr:
                self.window.feed("[err] " + stderr)
            ok = result.get("status") == "completed" and result.get("exit_code") in (0, None)
            if stdout:
                self.projection.reduce(
                    "StdoutChunk", {"call_id": call_id, "data": stdout}
                )
            if stderr:
                self.projection.reduce(
                    "StderrChunk", {"call_id": call_id, "data": stderr}
                )
            self.projection.reduce(
                "ExecutionExited",
                {"call_id": call_id, "exit_code": result.get("exit_code")},
            )
            self.mascot.observe("TaskCompleted" if ok else "TaskFailed", {"exit_code": result.get("exit_code")})
            self.projection.status = "SUCCESS" if ok else "FAILURE"
            self.projection.status_message = "Direct command complete." if ok else "Direct command failed."
            if self._full_screen:
                self.repaint_oi(force=True)
