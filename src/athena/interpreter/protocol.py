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
    "InterpreterObservation",
    "InterpreterProposal",
    "ProposalStatus",
]


@dataclass(frozen=True)
class InterpreterObservation:
    """One execution-grounded observation offered to the extension.

    Producers (runtime sessions, terminal sessions, process trees) emit
    these after an execution completes; the extension decides whether the
    observation warrants interpreter reasoning.
    """

    kind: str  # "runtime.evaluate" | "terminal.screen" | ...
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    session_id: str | None = None
    execution_id: str | None = None
    runtime_session_id: str | None = None
    process_ref: str | None = None
    artifact_uri: str | None = None  # large outputs live in artifacts


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
