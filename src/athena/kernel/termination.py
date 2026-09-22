"""Termination evaluation for AgentKernel (BUILDSPEC §18, EVALUATE_TERMINATION).

The kernel MUST NOT report ``complete`` purely because the model emitted final
language (BHV-005). Claimed completion is audited against the task's mandatory
acceptance criteria before ``COMPLETE`` is returned. When the model stops but
required acceptance evidence is missing or unverifiable, the honest outcome is
``PARTIAL`` with the unresolved criteria recorded (BHV-006: unknown stays
unknown).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence

from athena.protocol.messages import (
    CapabilityCallBlock,
    ContentBlock,
    TextBlock,
)
from athena.evidence import WorkEvidence, result_qualifies_as_work_evidence
from athena.protocol.models import ModelResponse
from athena.protocol.tasks import Criterion, TaskSpec, TaskStatus
from athena.protocol.tasks import MutationMode
from athena.protocol.termination import TerminationDecision
from athena.strategy import (
    EXECUTION,
    EXTERNAL_ACTION,
    MUTATION,
    OBSERVABLE_WORK_REQUIRED,
    OBSERVATION,
    RESPONSE,
    RESPONSE_ONLY,
    resolve_turn_intent,
)

__all__ = [
    "TerminationDecision",
    "TerminationEvaluator",
    "AcceptanceVerifier",
    "WorkEvidence",
    "result_qualifies_as_work_evidence",
    "work_evidence_satisfies",
]


def work_evidence_satisfies(
    evidence: Sequence[WorkEvidence],
    *,
    task: TaskSpec,
    completion_mode: str,
) -> bool:
    if completion_mode != OBSERVABLE_WORK_REQUIRED:
        return True
    expected = resolve_turn_intent(task.objective).kind
    if expected == MUTATION:
        return any(item.kind == MUTATION and item.mutation_ref for item in evidence)
    if expected == EXTERNAL_ACTION:
        return any(item.kind == EXTERNAL_ACTION and item.external_receipt for item in evidence)
    if expected == EXECUTION:
        return any(item.kind == EXECUTION for item in evidence)
    if expected == OBSERVATION:
        return any(item.kind in {OBSERVATION, "artifact"} for item in evidence)
    # A compatibility caller may request the generic observable gate for a
    # novel action; any non-control typed evidence is sufficient.
    return bool(evidence)


def _criterion_research_evidence_satisfied(
    criterion: Criterion,
    evidence: Sequence[WorkEvidence],
    *,
    total_required: int,
) -> bool:
    requirement_id = str(criterion.evidence_requirement_id or criterion.id)
    for item in evidence:
        if not item.research_ready:
            continue
        if requirement_id in item.research_requirement_ids:
            return True
        # A legacy single-bundle criterion has no stable requirement id. Keep
        # that compatibility path only when there cannot be cross-criterion
        # evidence confusion; multiple evidence criteria fail closed.
        if total_required == 1 and not item.research_requirement_ids:
            return True
    return False


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
        default_max_iterations: int = 50,
        defer_reality_verification: bool | Callable[[TaskSpec], bool] = False,
        required_child_state: Callable[[str], Any] | None = None,
    ) -> None:
        self._verifier = acceptance_verifier
        self._default_max_iterations = default_max_iterations
        self._defer_reality_verification = defer_reality_verification
        self._required_child_state = required_child_state

    async def evaluate(
        self,
        task: TaskSpec,
        response: ModelResponse,
        *,
        iterations: int,
        max_iterations: int | None = None,
        budget_exhausted: bool = False,
        cancelled: bool = False,
        completion_mode: str = RESPONSE_ONLY,
        observed_work: bool = False,
        work_evidence: Sequence[WorkEvidence] = (),
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

        if self._required_child_state is not None:
            child_state = self._required_child_state(task.id)
            if hasattr(child_state, "__await__"):
                child_state = await child_state
            pending, failed = child_state or ((), ())
            if failed:
                return TerminationDecision(
                    terminal=True,
                    reason="required child did not complete successfully",
                    status=TaskStatus.PARTIAL,
                    unresolved=tuple(failed),
                    summary=response_summary(response),
                )
            if pending:
                return TerminationDecision(
                    terminal=False,
                    reason="required child tasks are still running",
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

        # Acceptance criteria audit (BHV-005): a verified criterion proves
        # the *desired state* is true.  But state proof is not causal proof:
        # "config.json exists" passing does not mean Athena *created* it.
        # Separate the two completion requirements:
        #   - state-shaped objectives (observation/response): verified criteria
        #     ARE the work — the model observed and reported faithfully.
        #   - action-shaped objectives (execution/mutation/external): verified
        #     criteria alone are insufficient — require causal work receipts
        #     that prove Athena performed the requested action.
        required_criteria = [c for c in task.acceptance_criteria if c.required]
        unresolved = await self._unresolved_criteria(task)
        evidence_criteria = [
            criterion for criterion in required_criteria if criterion.evidence_required
        ]
        evidence_unresolved = tuple(
            criterion.id
            for criterion in evidence_criteria
            if not _criterion_research_evidence_satisfied(
                criterion, work_evidence, total_required=len(evidence_criteria)
            )
        )
        unresolved = tuple(dict.fromkeys((*unresolved, *evidence_unresolved)))
        if unresolved:
            return TerminationDecision(
                terminal=True,
                reason="claimed completion with unverified criteria",
                status=TaskStatus.PARTIAL,
                unresolved=unresolved,
                summary=response_summary(response),
            )

        expected = resolve_turn_intent(task.objective).kind
        state_only_objective = expected in {OBSERVATION, RESPONSE}
        verified_criteria_sufficient = (
            bool(required_criteria) and not unresolved and state_only_objective
        )

        # Action-shaped objectives need evidence from the shared execution
        # path. A fluent final paragraph is not proof that a file was read,
        # a command ran, or a mutation was applied.
        if completion_mode == OBSERVABLE_WORK_REQUIRED and not (
            observed_work
            or verified_criteria_sufficient
            or work_evidence_satisfies(work_evidence, task=task, completion_mode=completion_mode)
        ):
            return TerminationDecision(
                terminal=True,
                reason="observable work required but no qualifying evidence was observed",
                status=TaskStatus.PARTIAL,
                unresolved=("observable_work",),
                summary=response_summary(response),
            )

        return TerminationDecision(
            terminal=True,
            reason="objective satisfied",
            status=TaskStatus.COMPLETE,
            # The model's final text is the durable answer. Every terminal
            # branch preserves it: delegate.collect formats exactly this
            # summary back to the parent, so a fresh-context child that
            # solved its objective must hand up its conclusion, not a bare
            # status word.
            summary=response_summary(response),
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
        except Exception:  # rationale: verifier failure must leave every criterion unresolved
            return tuple(c.id for c in required)
        if not isinstance(results, list) or len(results) != len(required):
            return tuple(c.id for c in required)
        unresolved = [
            criterion.id
            for criterion, outcome in zip(required, results)
            if type(outcome) is not bool or not outcome
        ]
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
