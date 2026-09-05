"""Interpreter triggering policy (P1-13).

Which observations warrant an interpreter subturn? The extension condenses
body state that genuinely needs interpretation: screens, huge outputs,
debugger stops, failures that survive the primary loop's own correction
path. A concise failure is NOT such state — the primary transcript already
carries it verbatim, and an extra subturn would only re-derive context the
loop already has (cost amplification with no compression).

This module is pure policy over observation data: no I/O, no kernel
imports, fully table-driven so tests can pin the boundary per kind.
"""

from __future__ import annotations

from athena.interpreter.protocol import (
    CONCISE_ERROR_CHARS,
    CONCISE_OUTPUT_CHARS,
    BodyObservationKind,
    InterpreterObservation,
)

__all__ = ["observation_warrants_subturn"]

# How many consecutive failures of the same capability (after the primary
# loop's tool-correction path) turn the failure into an observation worth
# interpreting. The first failure is ordinary — the model sees the error
# and repairs. By the third, the repair loop is circling.
_REPEATED_FAILURE_THRESHOLD = 3

# A screen smaller than this is compact enough for the primary transcript;
# a larger one (busy TUI, long scrollback render) is interpreter material.
_SCREEN_CHARS = 4_000


def observation_warrants_subturn(observation: InterpreterObservation) -> bool:
    """Whether this observation justifies spending one interpreter subturn.

    Per-kind policy:

    - ``runtime.completed``: only when the run ended abnormally (timeout,
      interrupt, nonzero exit) or its output is too large for the primary
      transcript (the payload then carries bounded tails + artifact ref).
    - ``terminal.screen_changed``: screens are ambient state by definition —
      always interpret, bounded by size.
    - ``debugger.stopped``: always — a halt is uninterpretable from a
      transcript alone.
    - ``capability.repeated_failure``: always — the primary correction path
      has demonstrably stopped making progress.
    - ``capability.failed``: only when the failure is NOT concise. A short,
      legible error belongs to the primary loop; a sprawling traceback with
      a large output dump is body state that needs condensing.

    Unknown kinds default to False (fail closed): a producer must declare
    its kind in the protocol before its observations can spend subturns.
    """
    payload = observation.payload or {}

    if observation.kind == BodyObservationKind.RUNTIME_COMPLETED:
        abnormal = bool(
            payload.get("timed_out")
            or payload.get("interrupted")
            or _nonzero_exit(payload.get("exit_code"))
        )
        large = int(payload.get("output_chars") or 0) > CONCISE_OUTPUT_CHARS
        return abnormal or large

    if observation.kind == BodyObservationKind.TERMINAL_SCREEN_CHANGED:
        return len(str(payload.get("screen_text") or "")) >= _SCREEN_CHARS

    if observation.kind == BodyObservationKind.DEBUGGER_STOPPED:
        return True

    if observation.kind == BodyObservationKind.REPEATED_FAILURE:
        try:
            attempts = int(payload.get("attempts") or 0)
        except (TypeError, ValueError):
            return False
        return attempts >= _REPEATED_FAILURE_THRESHOLD

    if observation.kind == BodyObservationKind.CAPABILITY_FAILED:
        error_len = len(str(payload.get("error") or ""))
        output_len = len(str(payload.get("output") or ""))
        return error_len > CONCISE_ERROR_CHARS or output_len > CONCISE_OUTPUT_CHARS

    return False


def _nonzero_exit(exit_code: object) -> bool:
    if exit_code is None:
        return False
    try:
        return int(exit_code) != 0
    except (TypeError, ValueError):
        return False
