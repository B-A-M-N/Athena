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
from typing import Any, Callable, Protocol, Sequence

from athena.protocol.messages import (
    CapabilityCallBlock,
    ContentBlock,
    TextBlock,
)
from athena.protocol.models import ModelResponse
from athena.protocol.tasks import Criterion, TaskSpec, TaskStatus
from athena.protocol.tasks import MutationMode
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


@dataclass(frozen=True)
class WorkEvidence:
    """Typed proof that a capability crossed the requested work boundary."""

    kind: str
    capability_id: str
    operation: str = ""
    call_id: str = ""
    mutation_ref: str | None = None
    artifact_ref: str | None = None
    external_receipt: str | None = None


_CONTROL_CAPABILITIES = frozenset(
    {
        "capabilities",
        "skills",
        "workflows",
        "reflection",
        "request_input",
        "delegate.status",
        "delegate.collect",
        "schedule.describe",
        "schedule.list",
        "workflow.describe",
        "workflow.list",
    }
)
_READ_OPERATIONS = frozenset({"read", "list", "stat", "get", "exists", "open", "diff", "status", "inspect"})
_MUTATION_OPERATIONS = frozenset(
    {"write", "patch", "mkdir", "copy", "move", "delete", "remove", "update", "create", "apply", "save"}
)


def result_qualifies_as_work_evidence(
    result: Any,
    *,
    call: Any = None,
    required_kind: str | None = None,
) -> WorkEvidence | None:
    """Convert one successful result into qualifying, typed work evidence.

    Discovery/control results are intentionally not ordinary work evidence:
    finding a capability, recalling memory, listing a workflow, or asking a
    delegate for status cannot satisfy a task that required the underlying
    observation, execution, mutation, artifact, or external receipt.
    """
    if not bool(getattr(result, "ok", False)):
        status = getattr(getattr(result, "status", None), "value", None)
        if status != "ok":
            return None
    capability_id = str(getattr(result, "capability_id", "") or "")
    base_capability = capability_id.split(".", 1)[0]
    if not capability_id or capability_id in _CONTROL_CAPABILITIES:
        return None
    metadata = dict(getattr(result, "metadata", None) or {})
    arguments = dict(getattr(call, "arguments", None) or {})
    operation = str(
        arguments.get("operation")
        or arguments.get("action")
        or metadata.get("operation")
        or ""
    ).casefold()
    if capability_id.startswith("capabilities."):
        return None
    if capability_id.startswith("skills."):
        return None
    if capability_id.startswith("workflows."):
        return None
    if base_capability == "memory" and operation in {
        "",
        "recall",
        "search",
        "list",
        "get",
        "inspect",
    }:
        return None
    if base_capability == "workflow" and operation in {
        "",
        "list",
        "describe",
        "inspect",
    }:
        return None
    if base_capability == "delegate" and operation == "status":
        return None
    mutation = metadata.get("mutation")
    mutation_ref = None
    if isinstance(mutation, dict):
        mutation_ref = str(mutation.get("mutation_id") or mutation.get("id") or "") or None
    mutation_ref = mutation_ref or str(
        metadata.get("mutation_ref") or metadata.get("mutation_id") or ""
    ) or None
    artifact_ref = str(getattr(result, "ref_uri", None) or metadata.get("artifact_uri") or "") or None
    receipt = metadata.get("external_receipt") or metadata.get("receipt_id")
    external_receipt = str(receipt) if receipt else None

    capability_leaf = capability_id.rsplit(".", 1)[-1]
    # Canonical receipt path: the dispatcher has already resolved the exact
    # effects.  Prefer this over operation-name heuristics — it is the
    # authority for what the capability was allowed to cause. Match
    # case-insensitively: the dispatcher stamps EffectClass enum values
    # (uppercase), while policy metadata may carry lowercase effect names.
    resolved = metadata.get("resolved_effects")
    if resolved:
        resolved_set = {str(item).casefold() for item in resolved}
        if resolved_set & {"write_local", "delete"}:
            kind = MUTATION
        elif resolved_set & {"network_write", "external_message", "external_publish"}:
            kind = EXTERNAL_ACTION
        elif resolved_set & {"execute", "spawn_process"}:
            kind = EXECUTION
        elif resolved_set & {"read_local", "network_read"}:
            kind = OBSERVATION
        elif artifact_ref is not None and required_kind in {None, "artifact"}:
            kind = "artifact"
        else:
            kind = OBSERVATION
    elif capability_id in {"execute", "shell", "process"} or capability_leaf in {
        "execute", "shell", "process"
    } or operation in {"run", "exec", "execute", "pytest"}:
        kind = EXECUTION
    elif operation in _MUTATION_OPERATIONS or mutation_ref is not None:
        kind = MUTATION
    elif external_receipt is not None:
        kind = EXTERNAL_ACTION
    elif artifact_ref is not None and required_kind in {None, "artifact"}:
        kind = "artifact"
    elif operation in _READ_OPERATIONS or capability_id in {"fs", "git", "research", "http"}:
        kind = OBSERVATION
    else:
        kind = OBSERVATION

    evidence = WorkEvidence(
        kind=kind,
        capability_id=capability_id,
        operation=operation,
        call_id=str(getattr(result, "call_id", "") or ""),
        mutation_ref=mutation_ref,
        artifact_ref=artifact_ref,
        external_receipt=external_receipt,
    )
    if required_kind is not None and required_kind not in {kind, "artifact" if artifact_ref else kind}:
        return None
    return evidence


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
        default_max_iterations: int = 50,
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
            or work_evidence_satisfies(
                work_evidence, task=task, completion_mode=completion_mode
            )
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
