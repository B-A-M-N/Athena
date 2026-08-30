"""Termination evaluation for AgentKernel (BUILDSPEC §18, EVALUATE_TERMINATION).

The kernel MUST NOT report ``complete`` purely because the model emitted final
language (BHV-005). Claimed completion is audited against the task's mandatory
acceptance criteria before ``COMPLETE`` is returned. When the model stops but
required acceptance evidence is missing or unverifiable, the honest outcome is
``PARTIAL`` with the unresolved criteria recorded (BHV-006: unknown stays
unknown).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from athena.protocol.messages import (
    CapabilityCallBlock,
    ContentBlock,
    TextBlock,
)
from athena.protocol.models import ModelResponse
from athena.protocol.tasks import Criterion, TaskSpec, TaskStatus
from athena.protocol.tasks import MutationMode

__all__ = [
    "TerminationDecision",
    "TerminationEvaluator",
    "AcceptanceVerifier",
]


@dataclass(frozen=True)
class TerminationDecision:
    """Outcome of evaluating one model turn.

    ``terminal`` is True only when the reasoning loop must exit after this
    iteration. If terminal, ``status`` is the target terminal status and
    ``reason`` explains the outcome.
    """

    terminal: bool
    reason: str = ""
    status: TaskStatus | None = None
    unresolved: tuple[str, ...] = ()
    summary: str = ""


class AcceptanceVerifier(Protocol):
    """Delegated acceptance-criteria verification (BUILDSPEC §10, §21).

    Implementations resolve the ``Criterion.verification`` specifications into
    observed evidence. The kernel calls this only as an interpreter of evidence;
    it never runs commands itself (INV-007 / §16 MUST NOT list).
    """

    async def verify(self, task: TaskSpec, criteria: tuple[Criterion, ...]) -> list[bool]:
        """Return, in order, whether each criterion is satisfied.

        A False result (unsatisfied / unverifiable) means that criterion is
        treated as unresolved (BHV-005, BHV-006).
        """
        ...


class TerminationEvaluator:
    """Decides whether the reasoning loop should stop after a model turn.

    The evaluator is intentionally conservative about ``COMPLETE``:

    * a response that still contains capability calls is never terminal;
    * cancellation, budget exhaustion and iteration limits are terminal;
    * a claimed-complete task whose mandatory acceptance criteria have not been
      verified is ``PARTIAL`` (not ``COMPLETE``).
    """

    def __init__(
        self,
        *,
        acceptance_verifier: AcceptanceVerifier | None = None,
        default_max_iterations: int = 100,
        defer_reality_verification: bool | Callable[[TaskSpec], bool] = False,
    ) -> None:
        self._verifier = acceptance_verifier
        self._default_max_iterations = default_max_iterations
        self._defer_reality_verification = defer_reality_verification

    async def evaluate(
        self,
        task: TaskSpec,
        response: ModelResponse,
        *,
        iterations: int,
        max_iterations: int | None = None,
        budget_exhausted: bool = False,
        cancelled: bool = False,
    ) -> TerminationDecision:
        # Cancellation is terminal at turn boundary if signalled.
        if cancelled:
            return TerminationDecision(
                terminal=True,
                reason="task cancelled",
                status=TaskStatus.CANCELLED,
            )

        # Budget exhaustion always stops the loop (partial, not failed).
        if budget_exhausted:
            return TerminationDecision(
                terminal=True,
                reason="resource budget exhausted",
                status=TaskStatus.PARTIAL,
            )

        if _any_cap(response.blocks):
            # Model still wants to act; keep looping.
            return TerminationDecision(terminal=False, reason="capability_calls_present")

        cap = max_iterations if max_iterations is not None else self._default_max_iterations
        if iterations >= cap:
            return TerminationDecision(
                terminal=True,
                reason=f"max_agent_iterations reached ({cap})",
                status=TaskStatus.PARTIAL,
            )

        if not _claims_complete(response):
            # Model stopped without asserting completion; continue looping.
            return TerminationDecision(
                terminal=False,
                reason="model did not signal completion",
            )

        # Candidate proof has one owner.  For speculative tasks the reality
        # coordinator verifies the exact candidate once; running command
        # criteria here first would duplicate expensive work against a
        # different verification view.
        if _should_defer_reality_verification(self._defer_reality_verification, task):
            return TerminationDecision(
                terminal=True,
                reason="candidate verification delegated to reality coordinator",
                status=TaskStatus.COMPLETE,
                summary=response_summary(response),
            )

        # The model claims completion. Audit acceptance criteria (BHV-005).
        unresolved = await self._unresolved_criteria(task)
        if unresolved:
            return TerminationDecision(
                terminal=True,
                reason="claimed completion with unverified criteria",
                status=TaskStatus.PARTIAL,
                unresolved=unresolved,
                summary=response_summary(response),
            )

        return TerminationDecision(
            terminal=True,
            reason="objective satisfied",
            status=TaskStatus.COMPLETE,
        )

    async def _unresolved_criteria(self, task: TaskSpec) -> tuple[str, ...]:
        required = [c for c in task.acceptance_criteria if c.required]
        if not required:
            return ()
        if self._verifier is None:
            # No verifier configured; satisfaction cannot be proven. BHV-006.
            return tuple(c.id for c in required)
        try:
            results = await self._verifier.verify(task, tuple(required))
        except Exception:
            return tuple(c.id for c in required)
        unresolved = [c.id for c, ok in zip(required, results) if not ok]
        return tuple(unresolved)


def _claims_complete(response: ModelResponse) -> bool:
    return response.finish_reason in ("stop", "end_turn", None)


def _any_cap(blocks: tuple[ContentBlock, ...]) -> bool:
    return any(isinstance(b, CapabilityCallBlock) for b in blocks)


def _reality_owns_verification(task: TaskSpec) -> bool:
    workspace = task.workspace
    return bool(workspace is not None and workspace.mutation_mode is MutationMode.SPECULATIVE)


def _should_defer_reality_verification(
    setting: bool | Callable[[TaskSpec], bool], task: TaskSpec
) -> bool:
    if callable(setting):
        try:
            return bool(setting(task))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False
    return bool(setting and _reality_owns_verification(task))


def response_summary(response: ModelResponse) -> str:
    parts = [b.text for b in response.blocks if isinstance(b, TextBlock) and b.text]
    return "\n".join(parts)
