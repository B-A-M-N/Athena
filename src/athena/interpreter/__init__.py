"""Interpreter fusion package (audit P0.2).

The kernel-owned Interpreter Reasoning Extension. Execution observations
from Athena's computational body (runtimes, terminals, processes) flow:

    execution observation
            ↓
    InterpreterExtension (this package)
            ↓
    AgentKernel inference broker (kernel._invoke)
            ↓
    same ModelRouter  →  same effective Athena model
            ↓
    capability proposal (CapabilityRequest)
            ↓
    CapabilityDispatcher  →  PolicyEngine  →  ExecutionManager

The extension NEVER touches a provider, subprocess, or the database
directly. All inference goes through the kernel's single inference path
(same budgets, cancellation, usage accounting, replay state); all effects
go through the canonical capability dispatch path (repair → policy →
approval → execution → durable evidence).

Import surface:
    InterpreterExtension    the kernel-side hook
    InterpreterObservation  one body observation offered to the extension
    InterpreterProposal     the capability proposal it returns
"""

from athena.interpreter.context import InterpreterContext
from athena.interpreter.extension import InterpreterExtension
from athena.interpreter.protocol import (
    InterpreterObservation,
    InterpreterProposal,
    ProposalStatus,
)

__all__ = [
    "InterpreterContext",
    "InterpreterExtension",
    "InterpreterObservation",
    "InterpreterProposal",
    "ProposalStatus",
]
