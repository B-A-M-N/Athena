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
from collections import deque
from typing import Any, Mapping

from athena.cli.animation import AnimationClock, OIAnimator
from athena.cli.event_projection import DualPaneEventProjection
from athena.cli.chassis_composition import DualPaneChassisComposer
from athena.cli.glass_presentation import GlassPresentation
from athena.presentation.ansi import CellGridDiffRenderer
from athena.presentation.layout import compute_layout
from athena.presentation.projection import OperationNode, ProjectionState
from athena.presentation.scene import build_oi_scene
from athena.presentation.semantics import VisualActionKind, classify_event
from athena.cli.frame_composition import DualPaneFrameComposer
from athena.cli.framebuffer import OIFrameBuffer, pillow_available
from athena.cli.dual_pane_lifecycle import DualPaneLifecycle
from athena.cli.input import PromptController
from athena.cli.render.kitty import (
    KittyCapabilityProbe,
    KittyGraphicsProtocol,
    select_renderer,
)
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
        "idle",
        "listening",
        "thinking",
        "responding",
        "inspecting",
        "searching",
        "reading",
        "coding",
        "tools",
        "executing",
        "waiting",
        "approval",
        "delegated",
        "success",
        "warning",
        "failure",
        "interrupted",
        "recovering",
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
       ,___,    ∿∿∿
       (◉,◉)   ∿∿∿ forming…
       /)_)   ⚡
      """,
            r"""
       ,___,   ~∿∿
       (◦,◦)   ∿∿∿ ideas
       /)_)  ⚡
      """,
        ),
        "executing": (
            r"""
       ,___,   ▤▓▒
       (◉‿◉)  ▤▓▒▒ running
       /|_\   ▒▓▤ hacking
      """,
            r"""
       ,___,  ▒▓▤
       (^,^)  ▒▓▒▒ pouncing
       /|_\  ▤▓▧
      """,
        ),
        "waiting": (
            r"""
       ,___,
       (⊙,⊙)  ⏸ awaiting
       /)_)  permission…
      """,
            r"""
       ,___,
       (•,•)  ⏸ may I?
       /)_)  pretty please?
      """,
        ),
        "done": (
            r"""
       ,___,
       (★,★)  ✓ caught it
       /)_)  purr…
      """,
            r"""
       ,___,
       (^,^)  ✓ done!
       /)_)  ∿
      """,
        ),
        "failed": (
            r"""
       ,___,  ✗ oops
       (✕,✕)
       /)_)  ears flat
      """,
            r"""
       ,___,  ✗ mrow.
       (✕,✕)
       /)_)  ears flat
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
        self.object = ""  # carried activity object, e.g. "[>]"
        self.speech = ""  # short deterministic operational line
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
            self.state, self.object, self.speech = (
                "inspecting",
                "[?]",
                "Keeping the thread compact.",
            )
        elif event_type in {"ModelRequestStarted", "ModelReasoningDelta"}:
            self.state, self.object, self.speech = "thinking", "", "Thinking…"
        elif event_type == "ModelDelta":
            self.state, self.object, self.speech = "responding", "", ""
        elif event_type == "ModelRequestFailed":
            self.state, self.object, self.speech = (
                "failure",
                self.OBJ_FAIL,
                "The model needs attention.",
            )
        elif event_type == "ModelResponseCompleted":
            self.state, self.speech = "tools" if payload.get("tool_calls") else "responding", ""
        elif event_type in {"SearchStarted", "ResearchStarted"}:
            self.state, self.object = "searching", self.OBJ_TERMINAL
        elif event_type in {"FileRead", "InspectionStarted"}:
            self.state, self.object = "reading", self.OBJ_ARTIFACT
        elif event_type == "CapabilityRequested":
            action = classify_event(event_type, payload)
            state_by_action = {
                VisualActionKind.CODE: "coding",
                # Keep direct execution's established mascot choreography,
                # while the shared classifier distinguishes tests/verification
                # for the OI and native surfaces.
                VisualActionKind.EXECUTE: "coding",
                VisualActionKind.TEST: "executing",
                VisualActionKind.VERIFY: "tools",
                VisualActionKind.READ: "reading",
                VisualActionKind.INSPECT: "inspecting",
                VisualActionKind.SEARCH: "searching",
                VisualActionKind.GENERATE: "tools",
                VisualActionKind.APPROVAL: "approval",
                VisualActionKind.FAILURE: "failure",
                VisualActionKind.RECOVER: "recovering",
            }
            self.state = state_by_action.get(action, "tools")
            self.object = (
                self.OBJ_TERMINAL
                if action in {VisualActionKind.EXECUTE, VisualActionKind.TEST}
                else self.OBJ_VERIFY
                if action is VisualActionKind.VERIFY
                else self.OBJ_ARTIFACT
                if action
                in {VisualActionKind.READ, VisualActionKind.INSPECT, VisualActionKind.SEARCH}
                else self.OBJ_CODE
            )
            self.speech = ""
        elif event_type == "CapabilityValidated":
            self.state, self.object = "tools", self.OBJ_CODE
        elif event_type == "PolicyDecisionMade":
            decision = str(payload.get("decision") or "").lower()
            if decision in {"deny", "denied"}:
                self.state, self.object = "warning", self.OBJ_FAIL
            else:
                self.state, self.object = "tools", self.OBJ_CODE
        elif event_type in {
            "CapabilityStarted",
            "CapabilityProgress",
            "ExecutionStarted",
            "StdoutChunk",
            "StderrChunk",
        }:
            self.state, self.object, self.speech = "executing", self.OBJ_TERMINAL, ""
        elif event_type == "ApprovalRequested":
            self.state, self.object, self.speech = "approval", self.OBJ_APPROVAL, "Waiting on you."
        elif event_type == "ApprovalResolved":
            decision = str(payload.get("decision") or payload.get("status") or "").lower()
            if decision in {"denied", "deny", "rejected"}:
                self.state, self.object, self.speech = "warning", self.OBJ_FAIL, "Approval denied."
            else:
                self.state, self.object, self.speech = "executing", self.OBJ_TERMINAL, ""
        elif event_type == "VerificationStarted":
            self.state, self.object, self.speech = (
                "executing",
                self.OBJ_VERIFY,
                "Checking the candidate.",
            )
        elif event_type == "VerificationCheckCompleted":
            status = str(payload.get("status") or "").casefold()
            if status in {"failed", "failure", "error"}:
                self.state, self.object, self.speech = (
                    "failure",
                    self.OBJ_FAIL,
                    "Verification found a mismatch.",
                )
            else:
                self.state, self.object, self.speech = (
                    "executing",
                    self.OBJ_VERIFY,
                    "Checking the candidate.",
                )
        elif event_type == "VerificationCompleted":
            status = str(payload.get("status") or "").casefold()
            if status in {"passed", "complete", "completed"}:
                self.state, self.object, self.speech = (
                    "success",
                    self.OBJ_VERIFY,
                    "Candidate verified.",
                )
            else:
                self.state, self.object, self.speech = (
                    "failure",
                    self.OBJ_FAIL,
                    "Candidate verification failed.",
                )
        elif event_type == "DiagnosticsProduced":
            self.state, self.object, self.speech = (
                "failure",
                self.OBJ_FAIL,
                "Diagnostics need attention.",
            )
        elif event_type == "ArtifactCreated":
            self.state, self.object, self.speech = "success", self.OBJ_ARTIFACT, "Artifact ready."
        elif event_type in {
            "ChildTaskCreated",
            "DelegationStarted",
            "BackgroundTaskStarted",
        }:
            self.state, self.object, self.speech = (
                "delegated",
                self.OBJ_PROCESS,
                "Delegated work is active.",
            )
        elif event_type in {"ChildTaskCompleted", "BackgroundTaskCompleted"}:
            self.state, self.object, self.speech = (
                "success",
                self.OBJ_VERIFY,
                "Delegated work returned.",
            )
        elif event_type == "BackgroundTaskFailed":
            self.state, self.object, self.speech = (
                "failure",
                self.OBJ_FAIL,
                "Background work needs attention.",
            )
        elif event_type in {"ToolRepaired", "MutationRecorded"}:
            self.state, self.object, self.speech = (
                "tools",
                self.OBJ_CODE,
                "Recording the next step.",
            )
        elif event_type in {"MutationRecordFailed", "ToolInputCorrectionExhausted"}:
            self.state, self.object, self.speech = "failure", self.OBJ_FAIL, "That needs attention."
        elif event_type in {"InterpreterProposalDispatched", "RuntimeSessionCreated"}:
            self.state, self.object, self.speech = (
                "tools",
                self.OBJ_CODE,
                "Recording the next step.",
            )
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
        elif event_type in {
            "ExecutionExited",
            "CapabilityCompleted",
            "TaskCompleted",
            "TaskPartial",
        }:
            if event_type == "TaskPartial":
                self.state, self.object, self.speech = (
                    "warning",
                    self.OBJ_FAIL,
                    "Needs a follow-up.",
                )
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
            self.state, self.object, self.speech = (
                "recovering",
                self.OBJ_PROCESS,
                "Restoring the run.",
            )
        elif event_type == "RecoveryCompleted":
            self.state, self.object, self.speech = (
                "listening",
                self.OBJ_VERIFY,
                "Recovery complete.",
            )

    def render(self, max_width: int = 24) -> list[str]:
        if not Mascot.CHARACTERS:
            Mascot._register_characters()
        frames = self.CHARACTERS[self.character]["frames"]
        frame_state = (
            self.state if self.state in frames else self._FRAME_FALLBACKS.get(self.state, "idle")
        )
        art = frames.get(frame_state, frames["idle"])[self._frame % 2]
        # Every emitted line is hard-bounded to max_width so the mascot can
        # never overflow its column and break the pane layout.
        lines = [ln.rstrip()[:max_width] for ln in art.strip("\n").splitlines()]
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
    def register_character(cls, name: str, label: str, frames: Mapping[str, Any]) -> bool:
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
        self.model_label = (model_label or os.environ.get("OPENROUTER_MODEL") or "—").strip() or "—"
        self.layout = compute_layout(self._term_cols, self._term_rows, self.display_requested)
        self._kitty_confirmed = os.environ.get("ATHENA_KITTY_CONFIRMED", "").lower() in {
            "1",
            "true",
            "yes",
        }
        self.display = select_renderer(
            self.display_requested,
            capability_confirmed=self._kitty_confirmed and pillow_available(),
        )
        self.dual = self.layout.mode.value != "plain"
        self._full_screen = self._supports_full_screen()
        self.projection = ProjectionState()
        self._frame_composer = DualPaneFrameComposer(self.projection, self.window.snapshot)
        self._chassis_composer = DualPaneChassisComposer()
        self._event_projection = DualPaneEventProjection(
            self.projection,
            self.mascot,
            self.window,
        )
        self.scene = build_oi_scene(
            self.projection, self.layout.oi, character=self.mascot.character
        )
        self.animator = OIAnimator(reduced_motion=reduced_motion)
        self.frame_renderer = CellGridDiffRenderer(self.output)
        self.terminal_session = TerminalSession(self.output, enabled=self._full_screen)
        self.oi_framebuffer = OIFrameBuffer()
        self.kitty = KittyGraphicsProtocol()
        self.glass_presentation = GlassPresentation(
            self.output,
            self.oi_framebuffer,
            self.kitty,
        )
        self._lifecycle = DualPaneLifecycle(
            output=self.output,
            terminal_session=self.terminal_session,
            glass_presentation=self.glass_presentation,
            animator=self.animator,
            mascot=self.mascot,
            full_screen=lambda: self._full_screen,
            display=lambda: self.display,
            prepare_display=self._prepare_display,
            repaint=self.repaint_oi,
        )
        self.animation_clock = AnimationClock(
            self._lifecycle.tick,
            enabled=animations,
            reduced_motion=reduced_motion,
        )
        self._lifecycle.bind_animation_clock(self.animation_clock)
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
    def _maintenance(self) -> deque[tuple[str, str]]:
        return self.projection.maintenance

    @property
    def _pending_approval(self) -> dict[str, Any] | None:
        return self.projection.pending_approval

    @property
    def _pending_approvals(self) -> list[dict[str, Any]]:
        """Expose the complete ordered approval set to the hosted renderer."""
        return self.projection.ordered_pending_approvals()

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
        self._lifecycle.open()

    def close(self) -> None:
        """Stop animation and restore the terminal, even after partial setup."""
        self._lifecycle.close()

    async def aclose(self) -> None:
        """Async teardown that waits for the animation task to exit."""
        await self._lifecycle.aclose()

    def _prepare_display(self) -> None:
        if self.display_requested in {"auto", "glass"} and not self._kitty_confirmed:
            self._kitty_confirmed = KittyCapabilityProbe.probe(self.output, self.prompt.stdin)
            self.display = select_renderer(
                self.display_requested,
                capability_confirmed=self._kitty_confirmed and pillow_available(),
            )

    def _animation_tick(self, dt: float) -> bool:
        return self._lifecycle.tick(dt)

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
        self.projection.set_status("READY", "Type a request below.")
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
        self.projection.set_status("THINKING", "Athena is reading your request.")
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
            self.projection.set_status(
                str(status).upper(), "Task finished; send another request when ready."
            )
        self.repaint_oi(force=True)

    def render_notice(self, text: str, *, status: str | None = None) -> None:
        if not self._full_screen:
            super().render_notice(text, status=status)
            return
        notice = _terminal_text(text)
        self.projection.set_status_message(notice)
        if notice:
            self.projection.add_recent("·", notice)
        if status:
            self.projection.set_status(str(status).upper())
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
    def _ingest_event(
        self, etype: str, payload: dict[str, Any], *, task_id: str | None = None
    ) -> None:
        self._event_projection.ingest(etype, payload, task_id=task_id)

    async def render_event(self, event: Any) -> None:
        async def parent_render(item: Any) -> None:
            await super(DualPaneSurface, self).render_event(item)

        await self._event_projection.render_event(
            event,
            full_screen=self._full_screen,
            details=self.details,
            parent_render=parent_render,
            set_details=lambda value: setattr(self, "details", value),
            repaint=self.repaint_oi,
        )

    async def choose_approval(self, event: Any) -> Any:
        async def parent_choose(item: Any) -> Any:
            return await super(DualPaneSurface, self).choose_approval(item)

        return await self._event_projection.choose_approval(
            event,
            full_screen=self._full_screen,
            parent_choose=parent_choose,
            repaint=self.repaint_oi,
        )

    # -- frame composition ----------------------------------------------
    @staticmethod
    def _fit(text: Any, width: int) -> str:
        return DualPaneFrameComposer.fit(text, width)

    def _frame_lines(self, *, refresh: bool = True) -> list[str]:
        if refresh:
            self._refresh_terminal_size()
        layout = self.layout
        cols = layout.columns
        if layout.mode.value == "plain":
            return [self._fit(f"ATHENA  {self.projection.status_message}", cols)]

        self.scene = build_oi_scene(self.projection, layout.oi, character=self.mascot.character)
        self.animator.set_state(
            self.mascot.state,
            self.scene.buddy_anchor,
            action_kind=self.scene.mode.value,
            code_lines=(len(self.scene.code_view.lines) if self.scene.code_view else 0),
        )
        left_w, right_w = layout.operator.width, layout.oi.width
        inner_h = max(layout.outer_operator.height - 2, 1)
        inner_w_left = max(left_w - 2, 1)
        inner_w_right = max(right_w - 2, 1)
        left = self._frame_composer.left_lines(
            inner_h,
            inner_w_left,
            model_text=self._model_text,
            details=self.details,
            left_scroll=self._left_scroll,
        )
        right = self._frame_composer.right_lines(
            inner_h,
            inner_w_right,
            display=self.display,
            right_scroll=self._right_scroll,
            details=self.details,
            scene=self.scene,
            mascot=self.mascot,
            mascot_enabled=self.mascot_enabled,
        )
        return self._chassis_composer.compose(
            layout=layout,
            left=left,
            right=right,
            status=self.projection.status,
            display=self.display,
            full_screen=self._full_screen,
            oi_enabled=self.oi_enabled,
            model_label=self.model_label,
            prompt_text=self._prompt_text,
            right_scroll=self._right_scroll,
        )

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
        frame = self._frame_lines(refresh=False)
        self.frame_renderer.draw(frame, columns=self._term_cols)
        if self.display == "glass":
            self._present_glass()

    def _present_glass(self) -> None:
        """Present the retained CRT layers through the owned pixel seam."""
        self.glass_presentation.present(
            layout=self.layout,
            scene=self.scene,
            visual=self.animator.visual,
        )

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
                self.projection.reduce("StdoutChunk", {"call_id": call_id, "data": stdout})
            if stderr:
                self.projection.reduce("StderrChunk", {"call_id": call_id, "data": stderr})
            self.projection.reduce(
                "ExecutionExited",
                {"call_id": call_id, "exit_code": result.get("exit_code")},
            )
            self.mascot.observe(
                "TaskCompleted" if ok else "TaskFailed", {"exit_code": result.get("exit_code")}
            )
            self.projection.set_status(
                "SUCCESS" if ok else "FAILURE",
                "Direct command complete." if ok else "Direct command failed.",
            )
            if self._full_screen:
                self.repaint_oi(force=True)
