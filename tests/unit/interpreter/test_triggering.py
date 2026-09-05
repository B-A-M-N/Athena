"""Typed body-observation triggering policy (P1-13 / P1-15).

Pins the boundary per observation kind: the interpreter condenses body
state that genuinely needs interpretation; concise failures and small
successful runs return directly to the primary loop.
"""

from __future__ import annotations

from athena.interpreter.protocol import (
    BodyObservationKind as K,
    InterpreterObservation,
)
from athena.interpreter.triggering import observation_warrants_subturn


def obs(kind: str, **payload) -> InterpreterObservation:
    return InterpreterObservation(kind=kind, payload=payload, task_id="t1")


class TestCapabilityFailed:
    def test_concise_failure_declined(self):
        assert not observation_warrants_subturn(
            obs(K.CAPABILITY_FAILED, error="TypeError: boom", output="")
        )

    def test_sprawling_traceback_accepted(self):
        assert observation_warrants_subturn(
            obs(K.CAPABILITY_FAILED, error="Traceback...\n" * 50, output="")
        )

    def test_large_output_accepted_even_with_short_error(self):
        assert observation_warrants_subturn(
            obs(K.CAPABILITY_FAILED, error="boom", output="x" * 5000)
        )


class TestRuntimeCompleted:
    def test_clean_small_run_declined(self):
        assert not observation_warrants_subturn(
            obs(K.RUNTIME_COMPLETED, exit_code=0, output_chars=500)
        )

    def test_nonzero_exit_accepted(self):
        assert observation_warrants_subturn(
            obs(K.RUNTIME_COMPLETED, exit_code=1, output_chars=100)
        )

    def test_timeout_accepted(self):
        assert observation_warrants_subturn(obs(K.RUNTIME_COMPLETED, timed_out=True))

    def test_huge_output_accepted(self):
        assert observation_warrants_subturn(
            obs(K.RUNTIME_COMPLETED, exit_code=0, output_chars=100_000)
        )


class TestTerminalScreen:
    def test_small_screen_declined(self):
        assert not observation_warrants_subturn(
            obs(K.TERMINAL_SCREEN_CHANGED, screen_text="tiny")
        )

    def test_large_screen_accepted(self):
        assert observation_warrants_subturn(
            obs(K.TERMINAL_SCREEN_CHANGED, screen_text="s" * 4000)
        )


class TestDebuggerStopped:
    def test_always_accepted(self):
        assert observation_warrants_subturn(
            obs(K.DEBUGGER_STOPPED, reason="exception")
        )


class TestRepeatedFailure:
    def test_below_threshold_declined(self):
        assert not observation_warrants_subturn(
            obs(K.REPEATED_FAILURE, attempts=2)
        )

    def test_at_threshold_accepted(self):
        assert observation_warrants_subturn(obs(K.REPEATED_FAILURE, attempts=3))

    def test_garbage_attempts_declined(self):
        assert not observation_warrants_subturn(
            obs(K.REPEATED_FAILURE, attempts="many")
        )


class TestUnknownKind:
    def test_fails_closed(self):
        """A producer must declare its kind before spending subturns."""
        assert not observation_warrants_subturn(obs("mystery.kind"))
