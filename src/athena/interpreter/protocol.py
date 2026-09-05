"""Interpreter fusion protocol types (audit P0.2).

Pure data. No I/O, no kernel imports, no provider imports — this module is
the stable contract between the computational body (runtimes/terminals/
processes) and the kernel-owned interpreter reasoning extension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from athena.protocol.capabilities import CapabilityRequestOrigin

__all__ = [
    "BodyObservationKind",
    "InterpreterObservation",
    "InterpreterProposal",
    "ProposalStatus",
]


class BodyObservationKind(str):
    """Standard observation kinds the computational body can emit.

    The interpreter's purpose is COMPRESSION: turning body state too large
    or too ambient for the primary context (a screen buffer, a huge
    traceback, a debugger stop) into one bounded proposal. Kinds the body
    emits must be drawn from here so triggering policy (what warrants a
    subturn) can be reasoned about per kind instead of per payload.

    Each kind documents the payload convention its producer follows.
    """

    # An execution (execute/runtime call) completed. Payload:
    #   exit_code: int | None, timed_out: bool, interrupted: bool,
    #   output_chars: int, stdout_tail / stderr_tail: str (bounded),
    #   artifact_uri: str | None (full output artifactized when large)
    RUNTIME_COMPLETED = "runtime.completed"
    # A terminal (PTY) screen changed materially. Payload:
    #   screen_text: str (the rendered screen, already bounded),
    #   session: str, rows / cols: int
    TERMINAL_SCREEN_CHANGED = "terminal.screen_changed"
    # A debugger halted (breakpoint, exception pause). Payload:
    #   reason: str (e.g. "breakpoint", "exception"), frames_head: str
    DEBUGGER_STOPPED = "debugger.stopped"
    # The same capability failed repeatedly after the primary loop's normal
    # tool-correction path already ran. Payload:
    #   capability_id: str, attempts: int, last_error: str (bounded)
    REPEATED_FAILURE = "capability.repeated_failure"
    # A capability call failed. Payload:
    #   call_id, capability_id, error, output (both bounded),
    #   generated_failure: dict
    CAPABILITY_FAILED = "capability.failed"


@dataclass(frozen=True)
class InterpreterObservation:
    """One execution-grounded observation offered to the extension.

    Producers (runtime sessions, terminal sessions, process trees) emit
    these after an execution completes; the kernel decides whether the
    observation warrants interpreter reasoning (see
    ``observation_warrants_subturn``).
    """

    kind: str  # a BodyObservationKind value
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    session_id: str | None = None
    execution_id: str | None = None
    runtime_session_id: str | None = None
    process_ref: str | None = None
    artifact_uri: str | None = None  # large outputs live in artifacts


# Bounds used by triggering policy: a payload field at or under these sizes
# is "concise" — the primary loop can read it directly and an interpreter
# subturn would only duplicate context the transcript already carries.
CONCISE_ERROR_CHARS = 300
CONCISE_OUTPUT_CHARS = 2_000


@dataclass(frozen=True)
class InterpreterProposal:
    """A capability proposal derived from an observation.

    The extension returns this (not a CapabilityRequest) so the kernel
    remains the sole constructor of canonical requests, and so a proposal
    carries no execution authority of its own.
    """

    capability_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    origin: CapabilityRequestOrigin = CapabilityRequestOrigin.MODEL

    def is_executable(self) -> bool:
        """A proposal is executable only when it names a real capability."""
        return bool(self.capability_id)


class ProposalStatus(str):
    """Outcome of a proposal attempt — inspectable, never swallowed."""

    PROPOSED = "PROPOSED"
    DISPATCHED = "DISPATCHED"
    DENIED = "DENIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
