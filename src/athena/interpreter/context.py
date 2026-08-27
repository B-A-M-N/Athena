"""Interpreter context: the shared execution scope (audit P0.2).

Groups the task/session/RunState/cancellation/budget identity that
interpreter subturns MUST share with the primary loop (audit P0.4):
one task, one budget envelope, one cancellation token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["InterpreterContext"]


@dataclass
class InterpreterContext:
    """The shared authority scope for interpreter reasoning.

    ``run_state`` is the kernel's RunState for the owning task — interpreter
    subturns increment the SAME counters (model_calls, tokens, cost) and
    observe the SAME cancellation Event, so there is no unmetered
    interpreter reasoning and no un-cancellable second loop.
    """

    task_id: str
    session_id: str | None
    run_state: Any                     # kernel.RunState (kept Any to avoid import cycle)
    role: str = "interpreter"

    # Durable handles the extension may reference (never mutate).
    execution_id: str | None = None
    runtime_session_id: str | None = None

    def cancel_requested(self) -> bool:
        cancel = getattr(self.run_state, "cancel", None)
        return bool(cancel is not None and cancel.is_set())
