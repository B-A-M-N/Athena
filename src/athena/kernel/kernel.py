"""AgentKernel — the single authoritative reasoning loop (INV-001).

This is the *only* loop in Athena. Schedulers, ACP, MCP, interfaces, and all
capabilities route through :meth:`AgentKernel.run_task` (BEHAVIORSPEC BHV-001,
BHV-002). The loop is a faithful, non-obscuring realisation of the canonical
pseudocode (BUILDSPEC §§17-18):

    acquire -> while True:
        assert_runnable
        emit iteration-started
        compile context             (BUILD_CONTEXT)
        select model                (SELECT_MODEL)
        invoke model, streaming     (MODEL_REQUEST -> MODEL_RESPONSE)
        capability calls?
            yes -> dispatch (single path; INV-004) -> record results -> loop
            no  -> evaluate termination
                   -> terminal? -> finalize
                   -> else -> loop
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.context.compiler import CompiledContext, ContextCompiler
from athena.models.registry import ProviderRegistry
from athena.models.tokens import ModelTokenEstimator
from athena.models.router import (
    ModelSelection,
)
from athena.protocol.capabilities import DispatchProvenance
from athena.protocol.errors import (
    ContextIntegrityError,
    ProviderError,
    RequestCancelled,
)
from athena.protocol.ids import new_id
from athena.protocol.messages import (
    CapabilityCallBlock,
    CapabilityResultBlock,
    ContentBlock,
    Message,
    Provenance,
    Role,
    SourceType,
    TextBlock,
    TrustClass,
    utcnow,
)
from athena.protocol.models import (
    ModelDelta,
    ModelRequest,
    ModelResponse,
)
from athena.protocol.policy import DEFAULT_PRINCIPAL_ID
from athena.protocol.tasks import (
    ModelPolicy,
    ResourceBudget,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.tasks import TaskStore

from athena.tasks.budgets import BudgetStateUnavailable
from athena.kernel.inference_broker import InferenceBroker
from athena.kernel.messages import assistant_message as _assistant_message
from athena.kernel.run_finalizer import RunFinalizer
from athena.kernel.continuations_coordinator import (
    ContinuationCoordinator,
)
from athena.kernel.dispatch import DispatchResult
from athena.kernel.lifecycle import TaskLifecycle
from athena.evidence import WorkEvidence, result_qualifies_as_work_evidence
from athena.kernel.termination import TerminationDecision, TerminationEvaluator
from athena.protocol.workflows import WorkflowRunner
from athena.interpreter.context import InterpreterContext  # noqa: F401 (annotation)
from athena.interpreter.protocol import InterpreterProposal  # noqa: F401 (annotation)
from athena.interpreter.triggering import (  # noqa: F401 (re-exported for tests)
    observation_warrants_subturn,
)

if TYPE_CHECKING:
    from athena.models.router import ModelRouter

__all__ = ["AgentKernel"]

_logger = logging.getLogger("athena.kernel")

# Hard process-wide safety ceiling; task/role policy may narrow this further.
_FALLBACK_ATTEMPTS = 8


def _bookkeeping_failure(what: str, task: TaskSpec | str | None, exc: BaseException) -> None:
    """Log a critical-bookkeeping failure visibly (P1-11).

    Cost/audit/telemetry persistence must never fail the task, but silent
    ``except: pass`` means Athena completes work while its evidence
    disappears without a trace. This logs at warning with task identity so
    operators can detect evidence loss; call sites that also own an event
    sink emit a diagnostic as well.
    """
    task_id = task if isinstance(task, str) else getattr(task, "id", None)
    _logger.warning(
        "bookkeeping failure: %s (task=%s): %s: %s",
        what,
        task_id or "?",
        type(exc).__name__,
        exc,
    )


class _ResultTextBlock(CapabilityResultBlock):
    """Text-capable view of a capability-result block.

    The foundation ``Message.text()`` (``athena.protocol.messages``) reads
    ``block.text`` on blocks typed as ``CapabilityResultBlock``, but that type
    exposes ``output``, not ``text``. This subclass bridges the gap so stored
    capability results can be compiled back into context (flagged in report).
    """

    @property
    def text(self) -> str:
        return self.output or self.error or ""


class _CallTextBlock(CapabilityCallBlock):
    """Text-capable view of a capability-call block (see ``_ResultTextBlock``)."""

    @property
    def text(self) -> str:
        return f"[capability:{self.capability_id}]"


def _textable_messages(messages) -> list[Message]:
    """Return messages whose capability blocks expose ``.text``, transaction-safe.

    Kept entirely within the kernel so the reasoning loop is not coupled to the
    foundation ``Message.text`` implementation (flagged in report).
    """
    out: list[Message] = []
    for msg in messages:
        blocks: list[ContentBlock] = []
        changed = False
        for b in msg.blocks:
            if isinstance(b, CapabilityResultBlock) and not isinstance(b, _ResultTextBlock):
                blocks.append(
                    _ResultTextBlock(
                        call_id=b.call_id,
                        capability_id=b.capability_id,
                        ok=b.ok,
                        output=b.output,
                        error=b.error,
                        metadata=b.metadata,
                        ref_uri=b.ref_uri,
                    )
                )
                changed = True
            elif isinstance(b, CapabilityCallBlock) and not isinstance(b, _CallTextBlock):
                blocks.append(
                    _CallTextBlock(
                        call_id=b.call_id,
                        capability_id=b.capability_id,
                        arguments=dict(b.arguments or {}),
                        candidate=b.candidate,
                    )
                )
                changed = True
            else:
                blocks.append(b)
        if changed:
            out.append(
                Message(
                    id=msg.id,
                    role=msg.role,
                    blocks=tuple(blocks),
                    created_at=msg.created_at,
                    provenance=msg.provenance,
                    metadata=dict(msg.metadata or {}),
                )
            )
        else:
            out.append(msg)
    return out


@dataclass
class RunState:
    """Per-``run_task`` accounting + cancellation token (rolled into one)."""

    task: TaskSpec
    start: datetime = field(default_factory=utcnow)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    iterations: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = field(default_factory=Decimal)
    cost_known: bool = True
    request_id: str | None = None
    provider: str | None = None
    inference_attempt_id: str | None = None
    budget_wall_time_remaining_s: float | None = None
    budget_wall_time_checkpoint_s: float = 0.0
    tool_correction_counts: dict[str, int] = field(default_factory=dict)
    # Consecutive failed results per capability, feeding the interpreter's
    # REPEATED_FAILURE trigger (P1-13). Distinct from tool_correction_counts:
    # that one counts input-shape corrections the repair loop handles; this
    # counts every failed result after the normal path has run.
    interpreter_failure_counts: dict[str, int] = field(default_factory=dict)
    work_evidence: list[WorkEvidence] = field(default_factory=list)

    @property
    def elapsed_ms(self) -> int:
        return int((utcnow() - self.start).total_seconds() * 1000)


# Tool-input corrections tolerated before the quality floor escalates one
# tier (P1-16): repeated malformed tool calls are a model-capability signal,
# not a prompt problem.
_QUALITY_ESCALATION_THRESHOLD = 2

# Escalation is one tier per threshold crossing, capped at FRONTIER (the
# ladder's top). "escalated" floors never retreat within the run: a run that
# needed a stronger model keeps it.
_ESCALATION_STEP = 1


def _escalated_quality_floor(policy: ModelPolicy, state: RunState | None) -> ModelPolicy:
    """Raise the policy's quality floor when the run shows correction strain.

    Escalation only ever NARROWS the candidate set one declared tier at a
    time, and only when at least one registered model could be affected —
    with no tier declarations anywhere, this is a no-op and routing is
    unchanged. The task policy object is never mutated (frozen dataclass).
    """
    from dataclasses import replace as _dc_replace

    from athena.protocol.models import ModelQualityTier

    base = getattr(policy, "min_quality_tier", None)
    total_corrections = 0
    if state is not None:
        counts = getattr(state, "tool_correction_counts", None)
        if isinstance(counts, dict):
            total_corrections = sum(int(v) for v in counts.values())
    if total_corrections < _QUALITY_ESCALATION_THRESHOLD:
        return policy
    steps = min(
        total_corrections // _QUALITY_ESCALATION_THRESHOLD,
        2,
    )
    try:
        current = (
            ModelQualityTier(str(base))
            if base
            else ModelQualityTier.ECONOMY  # undeclared base: escalate from the bottom
        )
    except ValueError:
        current = ModelQualityTier.ECONOMY
    rank = min(current.rank + steps * _ESCALATION_STEP, ModelQualityTier.FRONTIER.rank)
    if rank <= current.rank:
        return policy  # base already at (or above) the escalation ceiling
    for tier in ModelQualityTier:
        if tier.rank == rank:
            return _dc_replace(policy, min_quality_tier=tier.value)
    return policy


# --------------------------------------------------------------------------- #
# Message / result builders
# --------------------------------------------------------------------------- #


def _deny_result(suspended) -> CapabilityResultBlock:
    from athena.kernel.policy_context import deny_result

    return deny_result(suspended)


def _block_of(suspended) -> CapabilityCallBlock:
    req = getattr(suspended, "request", None)
    if req is None:
        return CapabilityCallBlock(call_id="", capability_id="", arguments={})
    return CapabilityCallBlock(
        call_id=getattr(req, "call_id", ""),
        capability_id=req.capability_id,
        arguments=dict(req.arguments or {}),
    )


def _observation_from_result(task, result: CapabilityResultBlock):
    """Build a typed InterpreterObservation from a failed capability result.

    Keeps the payload small (audit P0.2: producers artifactize anything
    large); None when there is nothing interpretive to offer. Whether the
    observation actually warrants a subturn is the triggering policy's
    decision (P1-13), made at the offer site.
    """
    from athena.interpreter.protocol import (
        BodyObservationKind,
        InterpreterObservation,
    )

    error_text = (result.error or "")[:2000]
    output_text = (result.output or "")[:4000]
    if not error_text and not output_text:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.CAPABILITY_FAILED,
        payload={
            "call_id": result.call_id,
            "capability_id": result.capability_id,
            "error": error_text,
            "output": output_text,
            "generated_failure": dict((result.metadata or {}).get("generated_failure") or {}),
        },
        task_id=task.id,
        session_id=task.session_id,
    )


def _repeated_failure_observation(task, result: CapabilityResultBlock, attempts: int):
    """Build a REPEATED_FAILURE observation when a capability keeps failing.

    The primary loop's normal tool-correction path repairs input-shape
    errors; when the same capability keeps failing past that, the loop is
    circling and the failure pattern is worth one interpretive look.
    Returns None below the policy threshold (the count is tracked
    regardless, so the threshold is evaluated against true attempts).
    """
    from athena.interpreter.protocol import (
        BodyObservationKind,
        InterpreterObservation,
    )
    from athena.interpreter.triggering import REPEATED_FAILURE_THRESHOLD

    if attempts < REPEATED_FAILURE_THRESHOLD:
        return None
    return InterpreterObservation(
        kind=BodyObservationKind.REPEATED_FAILURE,
        payload={
            "capability_id": result.capability_id,
            "attempts": attempts,
            "last_error": (result.error or "")[:500],
        },
        task_id=task.id,
        session_id=task.session_id,
    )


def _runtime_completed_observation(task, result: CapabilityResultBlock):
    """Build a RUNTIME_COMPLETED observation from an execute-style result.

    Covers the successful-but-voluminous case the failure-only trigger
    misses: a run that exited 0 but produced more output than the primary
    transcript should absorb (the tails and artifact ref go in the payload;
    triggering policy decides whether the size or the exit status warrants
    a subturn). None for results that are not execution-shaped.
    """
    from athena.interpreter.protocol import (
        BodyObservationKind,
        InterpreterObservation,
    )

    metadata = result.metadata or {}
    if "exit_code" not in metadata and "resolved_effects" not in metadata:
        return None
    is_execute = result.capability_id in {"execute", "shell", "process"} or (
        "execute" in set(metadata.get("resolved_effects") or ())
    )
    if not is_execute:
        return None
    output_text = (result.output or "")[:4000]
    return InterpreterObservation(
        kind=BodyObservationKind.RUNTIME_COMPLETED,
        payload={
            "call_id": result.call_id,
            "capability_id": result.capability_id,
            "exit_code": metadata.get("exit_code"),
            "timed_out": (result.error or "") == "execution timed out",
            "interrupted": (result.error or "") == "execution interrupted",
            "output_chars": len(result.output or ""),
            "stdout_tail": output_text,
            "artifact_uri": getattr(result, "ref_uri", None),
        },
        task_id=task.id,
        session_id=task.session_id,
        artifact_uri=getattr(result, "ref_uri", None),
    )


class AgentKernel:
    """The single authoritative reasoning loop (INV-001).

    Dependencies are injected; the kernel owns none of them (§16 MUST NOT).
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        events: EventStore,
        task_manager: Any,
        messages: MessageStore,
        registry: ProviderRegistry,
        context_compiler: ContextCompiler,
        termination: TerminationEvaluator,
        model_sink=None,
        token_sink=None,
        dispatch_factory=None,
        budgets=None,
        cancellations=None,
        provider_usage_store=None,
        model_response_store=None,
        continuation_store=None,
        workflow_run_store=None,
        input_request_store=None,
        steering_store=None,
        parked_slot_wait_s: float = 300.0,
        router: "ModelRouter",
        interpreter=None,
        reality_coordinator: Any = None,
        secret_manager=None,
        workflow_store=None,
        workflow_fabric=None,
        workflow_runner: WorkflowRunner | None = None,
    ) -> None:
        self._task_store = task_store
        self._events = events
        self._messages = messages
        self._registry = registry
        # ONE routing authority: the service-owned router carries role
        # policies; the kernel never builds its own (P1-23, audit P0.1).
        # The router is REQUIRED — no implicit fallback construction. A
        # second construction site would fork the routing authority.
        if router is None:
            raise ValueError(
                "AgentKernel requires an injected ModelRouter "
                "(exactly one routing authority; construct it in the service)"
            )
        self._router = router
        self._compiler = context_compiler
        self._termination = termination
        self._model_sink = model_sink
        self._token_sink = token_sink
        self._dispatch_factory = dispatch_factory
        self._provider_usage_store = provider_usage_store
        self._model_response_store = model_response_store
        self._continuation_store = continuation_store
        self._workflow_run_store = workflow_run_store
        # Operator-clarification continuation: a model-issued request_input
        # call parks the SAME task in WAITING_INPUT with the question durable;
        # the operator's answer resumes the identical task.
        self._input_request_store = input_request_store
        self._steering_store = steering_store
        # Worker slot release (P1-17): how long a parked wait (WAITING_INPUT,
        # WAITING_APPROVAL) may hold its worker coroutine. Past this, the run
        # returns with the task left in its paused status and the worker slot
        # is free; the durable continuation (open question / pending approval)
        # survives, and the resumer (provide_input / approve / startup
        # recovery) relaunches the task on a fresh worker.
        self._parked_slot_wait_s = max(float(parked_slot_wait_s), 0.0)
        self._parked_resource_releaser = None
        self._parked_resource_resumption_handler = None
        # Secret manager for runtime secrets supplied via request_input.
        self._secret_manager = secret_manager
        # Reality completion authority: intercepts terminal decisions to bind
        # acceptance evidence to an active candidate branch and promote only
        # proven reality.
        self._reality_coordinator = reality_coordinator
        self._workflow_runner = workflow_runner
        # Kernel-owned interpreter fusion hook (audit P0.2). The extension
        # itself carries no authority — it receives observations and returns
        # proposals; every subturn and every dispatch routes through the
        # kernel's own inference/dispatch paths. Opt-in: producers only
        # offer observations when the service wires the extension in.
        self._interpreter = interpreter
        self._budgets = budgets
        self._utility_model_semaphore = asyncio.Semaphore(4)
        self._lifecycle = TaskLifecycle(manager=task_manager)
        if budgets is not None:
            self._lifecycle.set_budget_tracker(budgets)
        if cancellations is not None:
            self._lifecycle.set_cancellation_manager(cancellations)

        self._runs: dict[str, RunState] = {}
        self._input_answers: dict[str, str] = {}
        # Durable terminal state is written by the lifecycle before the
        # kernel's final budget checkpoint runs.  Keep a process-local barrier
        # so callers that need a stable snapshot (forks, reviews) can wait for
        # the complete run boundary as well.
        self._completion_events: dict[str, asyncio.Event] = {}
        self._resume: dict[str, asyncio.Event] = {}
        self._resume_decision: dict[str, str] = {}
        # A resume event is only a wakeup hint.  ``_resume_armed`` tracks the
        # durable wait boundary so a notification racing with slot release can
        # tell the service whether a live waiter will consume it or whether a
        # fresh worker must be launched.  The per-task lock closes the final
        # timeout/notification handoff window.
        self._resume_armed: set[str] = set()
        self._resume_locks = ReferenceCountedKeyedLocks()
        # Ephemeral duplicate-append fast path only; durable message receipts
        # remain the correctness boundary across restarts.
        self._response_append_cache: set[str] = set()
        self._prefix_trackers: dict[tuple[str, str, str], Any] = {}

    def set_budget_tracker(self, budgets) -> None:
        # Late-bind the budget authority (construction-order tolerant, §19).
        self._budgets = budgets
        self._lifecycle.set_budget_tracker(budgets)

    def set_cancellation_manager(self, cancellations) -> None:
        # Late-bind the cancellation authority (construction-order tolerant, §20).
        self._lifecycle.set_cancellation_manager(cancellations)

    @property
    def lifecycle(self) -> TaskLifecycle:
        return self._lifecycle

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def run_task(self, task_id: str) -> TaskResult:
        completion = self._completion_events.setdefault(task_id, asyncio.Event())
        completion.clear()
        task = await self._lifecycle.acquire(task_id)
        state = self._runs.get(task_id) or RunState(task)
        self._runs[task_id] = state
        begin_compute = getattr(self._budgets, "begin_compute", None)
        end_compute = getattr(self._budgets, "end_compute", None)
        compute_started = False
        try:
            if begin_compute is not None:
                await begin_compute(task.id)
                compute_started = True
            await self._bootstrap(task, state)
            invocation = (task.metadata or {}).get("_pack_hook_invocation")
            if invocation is not None:
                return await self._run_pack_hook_workflow(task, state, invocation)
            return await self._loop(task, state)
        except BudgetStateUnavailable as exc:
            return await self._finalize(
                task,
                state,
                TaskStatus.RECOVERY_REQUIRED,
                f"budget state unavailable; recovery required: {exc}",
            )
        finally:
            if end_compute is not None and compute_started:
                await end_compute(task.id)
            self._runs.pop(task_id, None)
            self._resume_armed.discard(task_id)
            completion.set()

    async def _run_pack_hook_workflow(
        self,
        task: TaskSpec,
        state: RunState,
        invocation: Mapping[str, Any],
    ) -> TaskResult:
        """Execute a pack hook's declared workflow without model mediation."""
        workflow_id = str(invocation.get("workflow_id") or "")
        pack_id = str(invocation.get("pack_id") or "")
        if not workflow_id or not pack_id:
            return await self._finalize(
                task,
                state,
                TaskStatus.FAILED,
                "pack hook workflow invocation is incomplete",
            )
        workspace = task.workspace
        if workspace is None:
            return await self._finalize(
                task, state, TaskStatus.FAILED, "pack hook workflow requires a workspace"
            )
        if self._workflow_runner is None:
            return await self._finalize(
                task, state, TaskStatus.FAILED, "workflow runner is unavailable"
            )
        try:
            outcome = await self._workflow_runner.run_declared(task, dict(invocation))
            if outcome.suspended is not None:
                await self._transition(task, TaskStatus.WAITING_APPROVAL)
                return await self._paused_result(
                    task,
                    state,
                    TaskStatus.WAITING_APPROVAL,
                    "pack hook workflow is awaiting approval",
                )
            if outcome.status == "completed":
                return await self._finalize(
                    task,
                    state,
                    TaskStatus.COMPLETE,
                    f"pack hook workflow {outcome.workflow_id} completed",
                )
            reason = "; ".join(outcome.failures) or f"workflow status: {outcome.status}"
            return await self._finalize(task, state, TaskStatus.FAILED, reason)
        except Exception as exc:  # workflow failures become truthful task results  # rationale: boundary converts subordinate failure into observable recovery/fallback
            return await self._finalize(
                task,
                state,
                TaskStatus.FAILED,
                f"pack hook workflow failed: {exc}",
            )

    async def wait_for_completion(self, task_id: str, *, timeout: float | None = None) -> None:
        """Wait until the kernel has finished post-result cleanup for a run."""
        event = self._completion_events.get(task_id)
        if event is None:
            return
        if timeout is None:
            await event.wait()
        else:
            await asyncio.wait_for(event.wait(), timeout=max(float(timeout), 0.0))

    async def _bootstrap(self, task: TaskSpec, state: RunState) -> None:
        """Crash-recovery entry: an INTERRUPTED task may be resumed (BUILDSPEC
        §87-89); a task left QUEUED is promoted to RUNNING once we own it."""
        row = await self._task_store.get(task.id)
        status = TaskStatus(row["status"]) if row else None
        if status == TaskStatus.INTERRUPTED:
            await self._transition(task, TaskStatus.RUNNING)
        elif status == TaskStatus.QUEUED:
            await self._transition(task, TaskStatus.RUNNING)

    def cancel_task(self, task_id: str) -> None:
        """Hierarchical, idempotent cancellation (§20). Safe to call twice.

        Signals the per-run cancel token so the reasoning loop wakes immediately,
        marks the task cancelled on the CancellationManager (idempotent), and
        best-effort interrupts the active provider stream. The kernel remains the
        single finalizer: the terminal transition is done by the loop, so no
        duplicate status transition can race the final persist (§18, §86).
        """
        state = self._runs.get(task_id)
        if state is None or state.cancel.is_set():
            return
        state.cancel.set()
        cancellations = self._lifecycle.manager.cancellations
        if cancellations is not None:
            try:
                cancellations.set_token(task_id, "cancelled by kernel")
            except Exception as exc:  # rationale: boundary converts subordinate failure into observable recovery/fallback
                # P1-11: a cancellation bookkeeping failure can leave a token
                # un-set; operators must see why a task kept running.
                _bookkeeping_failure("cancellation token set", task_id, exc)
        if state.request_id and state.provider:
            try:
                provider = self._registry.provider_for(state.provider)
                asyncio.create_task(provider.cancel(state.request_id))
            except Exception as exc:  # rationale: boundary converts subordinate failure into observable recovery/fallback
                # P1-11: best-effort stream interrupt, but the miss is visible.
                _bookkeeping_failure("provider stream interrupt", task_id, exc)

    async def notify_approval_resolved(self, task_id: str, decision: str) -> bool:
        cancel_release = getattr(self, "_cancel_parked_resource_release", None)
        if callable(cancel_release):
            await cancel_release(task_id)
        self._resume_decision[task_id] = decision
        event = self._resume.setdefault(task_id, asyncio.Event())
        async with self._resume_locks.lock(task_id):
            armed = task_id in self._resume_armed
            event.set()
        return armed

    async def notify_input_provided(self, task_id: str, answer: str) -> bool:
        """Resume a WAITING_INPUT task with the operator's answer.

        The answer is already durably stored by ``InputRequestStore.resolve``
        (ANSWERED_PENDING_RESUME) before this wakeup fires. This method is
        therefore a non-authoritative fast path: the kernel reads the durable
        answer from the store on resume, so a crash between DB-write and
        wakeup cannot strand the task.
        """
        # The answer has already been committed by InputRequestStore.  Keep
        # plaintext out of process-local side channels; the durable row is the
        # authority and the event only reduces resume latency.
        cancel_release = getattr(self, "_cancel_parked_resource_release", None)
        if callable(cancel_release):
            await cancel_release(task_id)
        event = self._resume.setdefault(task_id, asyncio.Event())
        async with self._resume_locks.lock(task_id):
            armed = task_id in self._resume_armed
            event.set()
        return armed

    def _arm_resume_wait(self, task_id: str) -> None:
        """Arm one durable continuation boundary before publishing it.

        Clearing is legal only here, before the request/approval becomes
        externally actionable.  ``_park_wait`` must never clear the event:
        doing so after publication can erase a valid operator wakeup.
        """
        self._resume_armed.add(task_id)
        self._resume.setdefault(task_id, asyncio.Event()).clear()
        self._resume_decision.pop(task_id, None)

    def _resume_waiter_armed(self, task_id: str) -> bool:
        return task_id in self._resume_armed

    @staticmethod
    def _replay_policy_context(task) -> dict:
        from athena.kernel.policy_context import replay_policy_context

        return replay_policy_context(task)

    def _park_wait(self, task, state):
        return ContinuationCoordinator(self)._park_wait(task, state)

    def set_parked_resource_releaser(self, releaser) -> None:
        """Bind the service-owned parked-resource retention authority."""
        self._parked_resource_releaser = releaser

    def set_parked_resource_resumption_handler(self, handler) -> None:
        """Bind cancellation of a delayed parked-resource release."""
        self._parked_resource_resumption_handler = handler

    async def _cancel_parked_resource_release(self, task_id: str) -> None:
        handler = self._parked_resource_resumption_handler
        if not callable(handler):
            return
        try:
            result = handler(task_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # parked cleanup is evidence, not a resume blocker  # rationale: boundary converts subordinate failure into observable recovery/fallback
            _logger.warning("parked resource release cancellation failed for %s: %s", task_id, exc)

    async def _release_parked_resources(self, task) -> None:
        releaser = self._parked_resource_releaser
        if not callable(releaser):
            return
        try:
            outcome = releaser(task.id)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # parked cleanup is evidence, not a crash path  # rationale: boundary converts subordinate failure into observable recovery/fallback
            _logger.warning("parked resource release failed for %s: %s", task.id, exc)

    async def _paused_result(self, task, state, status: TaskStatus, reason: str) -> TaskResult:
        return await RunFinalizer(self)._paused_result(task, state, status, reason)

    def _consume_pending_input(self, task, state, request_id: str, args: dict | None = None):
        return ContinuationCoordinator(self)._consume_pending_input(task, state, request_id, args)

    def _input_request_path(self, task, state, response, input_calls):
        return ContinuationCoordinator(self)._input_request_path(task, state, response, input_calls)

    # ------------------------------------------------------------------ #
    # The loop — THE one reasoning loop (INV-001)
    # ------------------------------------------------------------------ #
    def _resume_paused_entry(self, task, state):
        return ContinuationCoordinator(self)._resume_paused_entry(task, state)

    async def _loop(self, task: TaskSpec, state: RunState) -> TaskResult:
        from athena.kernel.reasoning_loop import ReasoningLoop

        return await ReasoningLoop(self).run(task, state)

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    async def _compile(
        self, task: TaskSpec, *, context_window: int | None = None
    ) -> CompiledContext:
        recent: list[Message] = []
        if task.session_id:
            try:
                loader = getattr(self._messages, "list_causal_messages", None)
                if loader is not None:
                    recent = await loader(task.session_id, task.id)
                else:
                    loader = getattr(self._messages, "list_task_messages", None)
                    if loader is not None:
                        recent = await loader(task.session_id, task.id)
                    else:
                        loader = getattr(self._messages, "list_recent_session_messages", None)
                        if loader is None:
                            loader = self._messages.list_session_messages
                        recent = await loader(task.session_id)
            except Exception as exc:  # rationale: boundary converts subordinate failure into observable recovery/fallback
                raise ContextIntegrityError(
                    f"canonical transcript unavailable for session {task.session_id}",
                    cause=exc,
                    session_id=task.session_id,
                ) from exc
        compiled = await self._compiler.compile(
            task,
            recent_messages=_textable_messages(recent),
            workspace=task.workspace.root if task.workspace else None,
            context_window=context_window,
        )
        strategy = compiled.strategy
        await self._emit("StrategySelected", strategy.to_dict(), task)
        if compiled.degradations:
            # P1-6: optional-context failures are visible, not silent. Empty
            # memory/skills/transcript must be distinguishable from a store
            # that raised; this is the diagnostic that says which it was.
            await self._emit(
                "DiagnosticsProduced",
                {
                    "kind": "context_degradation",
                    "degradations": [
                        {"source": d.source, "scope": d.scope, "detail": d.detail}
                        for d in compiled.degradations
                    ],
                    "count": len(compiled.degradations),
                },
                task,
            )
        if strategy.missing_affordance:
            await self._emit(
                "AffordanceGapDetected",
                {
                    "missing_affordance": strategy.missing_affordance,
                    "route": strategy.route,
                },
                task,
            )
        return compiled

    async def _apply_pending_steering(self, task: TaskSpec) -> None:
        """Materialize queued steering as durable user content at a safe boundary."""
        store = self._steering_store
        if store is None or not task.session_id:
            return
        for item in await store.list_pending(task.id):
            message_id = f"msg_steer_{item['id']}"
            source = str(item.get("source") or "").strip().lower()
            if not source:
                source = "parent_task" if item.get("source_task_id") else "operator"
            if source == "parent_task":
                source_type = SourceType.TASK
                trust = TrustClass.AGENT_CURATED
                prefix = "[Parent-task steering for the current task; consider it at this reasoning boundary]\n"
            elif source == "system":
                source_type = SourceType.SYSTEM
                trust = TrustClass.AUTHORITY
                prefix = "[System steering for the current task]\n"
            else:
                source_type = SourceType.USER
                trust = TrustClass.USER_CONTENT
                prefix = "[Operator steering for the current task; consider it at this reasoning boundary]\n"
            message = Message(
                id=message_id,
                role=Role.USER,
                blocks=(
                    TextBlock(
                        text=(prefix + str(item["text"])),
                        provenance=Provenance(
                            source_type=source_type,
                            source_id=str(item["id"]),
                            trust=trust,
                            scope=f"task:{task.id}",
                            created_at=utcnow(),
                        ),
                    ),
                ),
                created_at=utcnow(),
                provenance=Provenance(
                    source_type=source_type,
                    source_id=str(item["id"]),
                    trust=trust,
                    scope=f"task:{task.id}",
                    created_at=utcnow(),
                ),
                metadata={
                    "session_id": task.session_id,
                    "task_id": task.id,
                    "steering_id": item["id"],
                    "source_task_id": item.get("source_task_id"),
                    "source": source,
                },
            )
            await self._messages.append_user_turn(task.session_id, message)
            await store.mark_consumed(item["id"])
            await self._emit(
                "TaskSteered",
                {
                    "steering_id": item["id"],
                    "principal_id": item["principal_id"],
                    "source_task_id": item.get("source_task_id"),
                },
                task,
            )

    async def _select_model(
        self,
        task: TaskSpec,
        compiled: CompiledContext,
        *,
        state: RunState | None = None,
        exclude: frozenset[str | tuple[str, str]] = frozenset(),
        relax_context: bool = False,
    ) -> ModelSelection:
        return await InferenceBroker(self)._select_model(
            task, compiled, state=state, exclude=exclude, relax_context=relax_context
        )

    async def _invoke(
        self,
        task: TaskSpec,
        state: RunState,
        selection: ModelSelection,
        compiled: CompiledContext,
        *,
        inference_kind: str | None = None,
    ) -> ModelResponse:
        return await InferenceBroker(self)._invoke(
            task, state, selection, compiled, inference_kind=inference_kind
        )

    async def interpreter_subturn(
        self,
        *,
        context: "InterpreterContext",
        system_prompt: str,
        user_prompt: str,
    ):
        """Broker ONE interpreter subturn through the single inference path.

        The InterpreterExtension calls this; the kernel remains the only
        component that selects a model, opens a provider request, or meters
        usage. The subturn:

        * reuses the SAME RunState (model_calls / tokens / cost / cancel),
        * routes through the SAME ModelRouter with role "interpreter",
        * emits exactly ONE ModelRequestStarted/Completed pair (P1-8) via
          ``_invoke``'s single lifecycle path, tagged role="interpreter" and
          inference_kind="interpreter" so `athena inspect` shows it as its
          own row without double-counting inference boundaries,
        * compiles AUXILIARY context (P1-14): system instruction + task
          objective + the bounded observation. No skills, memory, research,
          project blocks, transcript, or capability tool schema — an
          interpreter subturn interprets the given observation; it does not
          mine the context corpus or act through a tool surface,
        * does NOT append to the durable assistant history — an interpreter
          subturn is a side read, not a conversational turn (its proposal,
          if any, is dispatched and its results land in the transcript the
          normal way).
        """
        from dataclasses import replace as _dc_replace

        if context.cancel_requested():
            raise RequestCancelled("interpreter subturn cancelled")
        task = context.run_state.task
        state = context.run_state
        role_policy = _dc_replace(task.model_policy, role="interpreter")
        subturn_task = _dc_replace(task, model_policy=role_policy)
        compiled = await self._compiler.compile_auxiliary(
            subturn_task, system=system_prompt, observation=user_prompt
        )
        selection = await self._select_model(subturn_task, compiled)
        return await self._invoke(
            subturn_task, state, selection, compiled, inference_kind="interpreter"
        )

    async def judge_subturn(
        self,
        *,
        task: TaskSpec,
        system_prompt: str,
        user_prompt: str,
    ) -> ModelResponse:
        """Broker one task-scoped acceptance-judge inference.

        Acceptance verification is auxiliary work, but it is still inference
        on behalf of a task. It therefore uses the task's active RunState when
        available, the normal router with the ``judge`` role, the normal
        provider usage store, and the normal cancellation/accounting path.
        The judge response is deliberately not appended as an assistant turn.
        """
        state = self._runs.get(task.id)
        if state is None:
            state = RunState(task)
        if state.cancel.is_set():
            raise RequestCancelled("judge subturn cancelled")
        role_policy = replace(task.model_policy, role="judge", require_tools=False)
        judge_task = replace(task, model_policy=role_policy)
        compiled = await self._compile_for_prompts(
            judge_task, system=system_prompt, user_prompt=user_prompt
        )
        selection = await self._select_model(judge_task, compiled)
        return await self._invoke(judge_task, state, selection, compiled)

    async def dispatch_interpreter_proposal(
        self,
        proposal: "InterpreterProposal",
        context: "InterpreterContext",
    ) -> "DispatchResult | None":
        """Route an interpreter proposal through the CANONICAL dispatch path.

        A proposal carries no authority: it becomes a CapabilityCallBlock and
        rides the same dispatch shim the primary loop uses — repair →
        policy → approval → execution → durable evidence. No special-cased
        interpreter execution lane exists.
        """
        if self._dispatch_factory is None:
            return None
        task = context.run_state.task
        call = CapabilityCallBlock(
            call_id=new_id("call"),
            capability_id=proposal.capability_id,
            arguments=dict(proposal.arguments or {}),
        )
        shim = self._dispatch_factory(task)
        await self._emit(
            "InterpreterProposalDispatched",
            {
                "capability_id": proposal.capability_id,
                "call_id": call.call_id,
                "rationale": proposal.rationale,
                "role": "interpreter",
            },
            task,
        )
        # Bind the producing subturn's identity for repair receipts, exactly
        # as the primary dispatch path does (kernel._dispatch). RunState.provider
        # is set by _invoke for the most recent inference — which, at this
        # point, is the interpreter subturn that produced the proposal.
        # Provenance is invocation state, not dispatcher state, so concurrent
        # tasks cannot overwrite each other's repair identity (P0).
        dispatch_kwargs: dict[str, float | None | DispatchProvenance] = {}
        if "runtime_remaining_s" in inspect.signature(shim.dispatch).parameters:
            dispatch_kwargs["runtime_remaining_s"] = self._remaining_runtime_seconds(
                task, context.run_state
            )
        if "provenance" in inspect.signature(shim.dispatch).parameters:
            dispatch_kwargs["provenance"] = DispatchProvenance(
                provider_profile_id=context.run_state.provider,
                model_id=None,
                repair_mode=None,
            )
        return await shim.dispatch(task, [call], **dispatch_kwargs)

    async def _offer_observation(self, task, state, observation) -> None:
        """Offer one observation to the interpreter extension and dispatch
        any proposal it returns through the canonical path.

        One observation → at most one subturn → at most one proposal →
        canonical dispatch. Failures are logged, never fatal to the primary
        loop (the result that triggered the observation is already durable).
        """
        from athena.interpreter.context import InterpreterContext

        context = InterpreterContext(
            task_id=task.id,
            session_id=task.session_id,
            run_state=state,
        )
        proposal = await self._interpreter.interpret(observation, context)
        if proposal is None:
            return
        if not proposal.is_executable():
            return
        await self.dispatch_interpreter_proposal(proposal, context)

    async def offer_body_observation(self, observation) -> bool:
        """External producers' entry into the interpreter path (P1-15).

        Terminal sessions, runtimes, and process trees announce ambient body
        state (large screen renders, debugger stops) as events; the service
        bridges those events here. The same rules as loop-side offers apply:
        the triggering policy decides whether the observation warrants a
        subturn, the offer is skipped when the task has no live run, the
        budget is exhausted, or no extension is wired. Returns whether an
        offer was actually made.
        """
        if self._interpreter is None:
            return False
        if not observation_warrants_subturn(observation):
            return False
        task_id = observation.task_id
        state = self._runs.get(task_id) if task_id else None
        if state is None or state.cancel.is_set():
            return False
        task = state.task
        budget = getattr(task, "resource_budget", None)
        if budget is not None and _budget_exhausted(state, budget):
            return False
        try:
            await self._offer_observation(task, state, observation)
        except Exception:  # noqa: BLE001 — fusion must not kill the producer
            _logger.warning(
                "interpreter fusion failed for %s observation",
                observation.kind,
                exc_info=True,
            )
            return False
        return True

    async def utility_inference(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        role: str = "summarizer",
        task_id: str | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        budget_task_id: str | None = None,
    ) -> str | None:
        return await InferenceBroker(self).utility_inference(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            role=role,
            task_id=task_id,
            session_id=session_id,
            metadata=metadata,
            budget_task_id=budget_task_id,
        )

    async def task_utility_inference(
        self,
        *,
        task: TaskSpec,
        system_prompt: str,
        user_prompt: str,
        role: str = "summarizer",
    ) -> str | None:
        """Run auxiliary inference through the active task's budget path."""
        state = self._runs.get(task.id)
        if state is None:
            return None
        role_policy = replace(task.model_policy, role=role, require_tools=False)
        scoped_task = replace(task, model_policy=role_policy)
        compiled = await self._compile_for_prompts(
            scoped_task, system=system_prompt, user_prompt=user_prompt
        )
        selection = await self._select_model(scoped_task, compiled)
        response = await self._invoke(scoped_task, state, selection, compiled)

        return (
            " ".join(
                block.text
                for block in response.blocks
                if isinstance(block, TextBlock) and block.text
            ).strip()
            or None
        )

    async def _compile_for_prompts(
        self, task: TaskSpec, *, system: str, user_prompt: str
    ) -> CompiledContext:
        """Compile a one-off prompt pair without touching durable history."""
        from athena.protocol.messages import Role

        user_message = Message(
            id=new_id("msg"),
            role=Role.USER,
            blocks=(TextBlock(text=user_prompt),),
            created_at=utcnow(),
            provenance=Provenance(
                source_type=SourceType.SYSTEM, trust=TrustClass.CONFIGURED_INSTRUCTION
            ),
        )
        return await self._compiler.compile(task, system=system, recent_messages=[user_message])

    def _inference_metadata(self, selection: ModelSelection) -> dict[str, Any]:
        return InferenceBroker(self)._inference_metadata(selection)

    async def _attempt_metadata(
        self, task: TaskSpec, compiled: CompiledContext, selection: ModelSelection
    ) -> dict[str, Any]:
        return await InferenceBroker(self)._attempt_metadata(task, compiled, selection)

    async def _observe_prefix(
        self, task: TaskSpec, compiled: CompiledContext, selection: ModelSelection
    ) -> dict[str, Any]:
        return await InferenceBroker(self)._observe_prefix(task, compiled, selection)

    def _trusted_cache_namespace(self, task: TaskSpec | None = None) -> str:
        """Return the service-owned cache partition, never caller metadata.

        ``cache_namespace`` is an ownership boundary. Public request metadata
        rejects that name; only the internal underscored form can carry a
        trusted override, and the normal path uses the compiler principal.
        """
        internal = None
        if task is not None:
            internal = (task.metadata or {}).get("_athena_cache_namespace")
        value = internal or getattr(self._compiler, "principal_id", DEFAULT_PRINCIPAL_ID)
        return str(value).strip() or DEFAULT_PRINCIPAL_ID

    def _replay_metadata(
        self,
        compiled: CompiledContext,
        selection: ModelSelection,
    ) -> dict[str, Any]:
        return InferenceBroker(self)._replay_metadata(compiled, selection)

    async def _consume(
        self,
        task: TaskSpec,
        state: RunState,
        provider,
        request: ModelRequest,
        *,
        estimator: ModelTokenEstimator | None = None,
        request_fingerprint: str | None = None,
        attempt_id: str | None = None,
    ) -> ModelResponse:
        return await InferenceBroker(self)._consume(
            task,
            state,
            provider,
            request,
            estimator=estimator,
            request_fingerprint=request_fingerprint,
            attempt_id=attempt_id,
        )

    async def _relay_delta(self, task: TaskSpec, delta: ModelDelta) -> None:
        return await InferenceBroker(self)._relay_delta(task, delta)

    async def _dispatch(self, task, state, response, calls):
        # A model-issued clarification request is kernel-owned, not a capability
        # execution: intercept it before the dispatcher so the task can park in
        # WAITING_INPUT even when the turn compiled no other tools.
        input_calls = [c for c in calls if c.capability_id == "request_input"]
        if input_calls:
            # Every model-issued tool call must receive exactly one result.
            # When request_input co-occurs with other calls, the clarification
            # wins the turn: the other calls are not executed and each gets a
            # deterministic "suspended for operator clarification" result.
            # This prevents silently dropping call IDs.
            other_calls = [c for c in calls if c not in input_calls]
            if other_calls:
                suspended = [
                    CapabilityResultBlock(
                        call_id=c.call_id,
                        capability_id=c.capability_id,
                        ok=False,
                        error="not executed: turn suspended for operator clarification",
                    )
                    for c in other_calls
                ]
                await self._append_results(task, suspended, calls=other_calls)
            return await self._input_request_path(task, state, response, input_calls)
        # Natural-language framing is never a semantic authorization boundary:
        # the kernel refuses a call because policy forbids it, the task
        # disabled tools, the capability is unavailable, the request is
        # malformed, or authority is absent — never because the objective's
        # surface grammar looked conversational. A turn compiled with zero
        # capability definitions should not normally produce a valid call; if
        # one arrives anyway, the dispatcher's policy path owns the refusal.
        if self._dispatch_factory is None:
            not_executed = [
                CapabilityResultBlock(
                    call_id=c.call_id,
                    capability_id=c.capability_id,
                    ok=False,
                    error="capability path disabled",
                )
                for c in calls
            ]
            await self._append_results(task, not_executed, calls=calls)
            return None

        # The dispatcher may create and publish an approval request before it
        # returns a SuspendedCall.  Arm the task's resume boundary before that
        # call so an operator decision cannot arrive into an unarmed window.
        self._arm_resume_wait(task.id)
        shim = self._dispatch_factory(task)
        # Bind the producing inference turn to repair receipts before any
        # capability request is translated or dispatched. Provenance travels
        # with this dispatch invocation, never as shared dispatcher state, so
        # concurrent tasks cannot contaminate each other's records (P0).
        dispatch_kwargs: dict[str, float | None | DispatchProvenance] = {}
        if "runtime_remaining_s" in inspect.signature(shim.dispatch).parameters:
            dispatch_kwargs["runtime_remaining_s"] = self._remaining_runtime_seconds(task, state)
        if "provenance" in inspect.signature(shim.dispatch).parameters:
            dispatch_kwargs["provenance"] = DispatchProvenance(
                provider_profile_id=response.metadata.get("provider_profile_id", response.provider),
                model_id=response.metadata.get("model_id", response.model),
                repair_mode=response.metadata.get("tool_repair_mode"),
            )
        outcome = await shim.dispatch(task, calls, **dispatch_kwargs)

        if outcome.suspended:
            return await self._approval_path(task, state, outcome)

        await self._append_results(task, outcome.results, calls=calls)
        # Loop-side observation producer (audit P0.2 completion): a FAILED
        # capability result is an execution-grounded observation. Offer at
        # most ONE per dispatch (cost-amplification bound: a turn with N
        # failed calls must not trigger N unbounded model subturns, and the
        # budget is re-checked immediately so a task at its cost/token cap
        # cannot overshoot inside this loop) to the interpreter extension
        # (when the service wired one in). The extension's subturn and its
        # proposal's dispatch both meter through this kernel.
        if self._interpreter is not None:
            budget = getattr(task, "resource_budget", None)
            for result in outcome.results:
                if not isinstance(result, CapabilityResultBlock):
                    continue
                # Track consecutive failures per capability AFTER the primary
                # loop's own tool-correction path has run: enough repetitions
                # turn one more failed result into a REPEATED_FAILURE
                # observation (triggering policy decides the threshold).
                candidates = []
                if result.ok:
                    # RuntimeCompleted: successful runs with abnormal status
                    # or voluminous output are interpreter material too.
                    candidates.append(_runtime_completed_observation(task, result))
                else:
                    failures = state.interpreter_failure_counts
                    failures[result.capability_id] = failures.get(result.capability_id, 0) + 1
                    candidates = [
                        _observation_from_result(task, result),
                        _repeated_failure_observation(task, result, failures[result.capability_id]),
                    ]
                offered = False
                for observation in candidates:
                    if observation is None:
                        continue
                    # Triggering policy (P1-13): concise failures return
                    # directly to the primary loop; only observations that
                    # genuinely compress body state spend a subturn.
                    if not observation_warrants_subturn(observation):
                        continue
                    if budget is not None and _budget_exhausted(state, budget):
                        break
                    try:
                        await self._offer_observation(task, state, observation)
                        offered = True
                    except Exception:  # noqa: BLE001 — fusion must not kill the loop
                        _logger.warning(
                            "interpreter fusion failed for %s observation",
                            observation.kind,
                            exc_info=True,
                        )
                    break  # one subturn per dispatch, however many failures
                if offered:
                    break
        exhausted: list[str] = []
        max_cycles = int(response.metadata.get("max_tool_correction_cycles", 2))
        for result in outcome.results:
            if not isinstance(result, CapabilityResultBlock):
                continue
            if not (result.error or "").startswith("tool_input_invalid"):
                continue
            count = state.tool_correction_counts.get(result.capability_id, 0) + 1
            state.tool_correction_counts[result.capability_id] = count
            if count > max_cycles:
                exhausted.append(result.capability_id)
        if exhausted:
            await self._emit(
                "ToolInputCorrectionExhausted",
                {
                    "capabilities": sorted(set(exhausted)),
                    "max_cycles": max_cycles,
                },
                task,
            )
            return await self._finalize(
                task,
                state,
                TaskStatus.FAILED,
                "tool_input_invalid: correction budget exhausted",
            )
        return None

    def _approval_path(self, task, state, outcome: DispatchResult):
        return ContinuationCoordinator(self)._approval_path(task, state, outcome)

    # Durable-continuation mechanism (P1-10): bodies live in
    # :mod:`athena.kernel.continuations_coordinator`. Delegates bind the
    # coordinator per call against THIS instance — including bare test
    # namespaces — so every attribute access, patched or real, resolves
    # through the kernel exactly as when these bodies lived here.

    def _continuation_mechanism(self):
        return ContinuationCoordinator(self)

    async def _resume_workflow_parent(
        self,
        task,
        suspended=None,
        *,
        record: Any = None,
        shim: Any = None,
    ):
        return await ContinuationCoordinator(self)._resume_workflow_parent(
            task,
            suspended,
            record=record,
            shim=shim,
        )

    async def _reconcile_workflow_suspended(
        self, suspended, result, *, workspace_root: Any = None, workspace: Any = None
    ):
        return await ContinuationCoordinator(self)._reconcile_workflow_suspended(
            suspended, result, workspace_root=workspace_root, workspace=workspace
        )

    async def _mark_continuations_consumed(self, suspended):
        return await ContinuationCoordinator(self)._mark_continuations_consumed(suspended)

    async def _resume_durable_continuation(self, task):
        return await ContinuationCoordinator(self)._resume_durable_continuation(task)

    async def _reconcile_workflow_continuation(
        self, record, result, *, workspace_root: Any = None, workspace: Any = None
    ):
        return await ContinuationCoordinator(self)._reconcile_workflow_continuation(
            record, result, workspace_root=workspace_root, workspace=workspace
        )

    async def _consume_durable_call(self, call_id):
        return await ContinuationCoordinator(self)._consume_durable_call(call_id)

    async def _release_durable_call(self, call_id):
        return await ContinuationCoordinator(self)._release_durable_call(call_id)

    async def _scrub_input_answer(self, request_id, answer_ref):
        return await ContinuationCoordinator(self)._scrub_input_answer(request_id, answer_ref)

    async def _scrub_input_answer_impl(self, request_id, answer_ref):
        return await ContinuationCoordinator(self)._scrub_input_answer_impl(request_id, answer_ref)

    async def _finalize(self, task, state, status: TaskStatus, reason: str) -> TaskResult:
        return await RunFinalizer(self)._finalize(task, state, status, reason)

    async def _finalize_decision(self, task, state, decision: TerminationDecision) -> TaskResult:
        return await RunFinalizer(self)._finalize_decision(task, state, decision)

    async def _transition(self, task: TaskSpec, status: TaskStatus) -> None:
        # Delegated to TaskLifecycle/TaskManager (§16 MUST NOT: the kernel does
        # not own lifecycle/SQL; the manager validates, transitions, and emits).
        await self._lifecycle.transition(task.id, status)

    async def _emit(self, type_: str, payload: dict, task: TaskSpec) -> None:
        if self._events is None:
            return
        await self._events.append_event(
            type_,
            dict(payload),
            task_id=task.id,
            session_id=task.session_id,
        )

    def _deadline_passed(self, task: TaskSpec) -> bool:
        deadline = task.deadline
        return deadline is not None and utcnow() >= deadline

    @staticmethod
    def _remaining_runtime_seconds(task: TaskSpec, state: RunState) -> float | None:
        from athena.kernel.policy_context import remaining_runtime_seconds

        return remaining_runtime_seconds(task, state)

    async def _refresh_runtime_budget(self, task: TaskSpec, state: RunState) -> None:
        """Refresh the root-aware active-compute ceiling before each turn."""
        if self._budgets is None:
            return
        remaining = await self._budgets.remaining(task.id)
        wall_remaining = remaining.get("wall_time_s")
        state.budget_wall_time_remaining_s = (
            float(wall_remaining) if wall_remaining is not None else None
        )
        state.budget_wall_time_checkpoint_s = state.elapsed_ms / 1000

    async def _append_response(self, task: TaskSpec, response: ModelResponse) -> None:
        if response.request_id and response.request_id in self._response_append_cache:
            return
        message = _assistant_message(task, response)
        appended = await self._append_assistant_message(message)
        if not appended:
            if response.request_id:
                self._response_append_cache.add(response.request_id)
            return
        await self._emit(
            "TaskMessage",
            {
                "message_id": message.id,
                "role": message.role.value,
                "text": message.conversation_text(),
            },
            task,
        )
        if response.request_id:
            self._response_append_cache.add(response.request_id)

    async def _append_assistant_message(self, message: Message) -> bool:
        """Persist an assistant response through the durable replay boundary."""
        append_idempotent = getattr(self._messages, "append_idempotent", None)
        if append_idempotent is not None:
            result = bool(await append_idempotent(message))
            receipt = (message.metadata or {}).get("inference_receipt") or {}
            provider_metadata = receipt.get("provider_metadata") or {}
            attempt_id = provider_metadata.get("inference_attempt_id")
            hook = getattr(self, "_inference_fault_injector", None)
            if result and hook is not None:
                value = hook("assistant-message-append")
                if inspect.isawaitable(value):
                    await value
            if attempt_id and self._model_response_store is not None:
                await self._model_response_store.mark_assistant_appended(str(attempt_id))
            return result
        # Narrow compatibility path for test doubles and legacy adapters.
        await self._messages.append(message)
        return True

    async def _append_final_response(self, task: TaskSpec, response: ModelResponse) -> None:
        return await RunFinalizer(self)._append_final_response(task, response)

    async def _append_results(self, task: TaskSpec, blocks, *, calls=()) -> None:
        return await RunFinalizer(self)._append_results(task, blocks, calls=calls)

    async def _maybe_await(self, value) -> None:
        if inspect.isawaitable(value):
            await value


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #
def _input_tokens_of(
    response: ModelResponse,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> int:
    """Return reported input tokens or a conservative fallback ledger value."""
    try:
        usage = response.usage
    except AttributeError:
        usage = None
    count = int(getattr(usage, "input_tokens", None) or 0)
    if count > 0:
        return count
    estimate = _estimate_input_tokens(request, estimator=estimator)
    return estimate if estimate is not None else _display_input_estimate(request)


def _compiled_work_evidence(compiled: CompiledContext, task_id: str) -> list[WorkEvidence]:
    evidence: list[WorkEvidence] = []
    for message in compiled.messages:
        metadata = getattr(message, "metadata", {}) or {}
        recorded_task = metadata.get("task_id")
        if recorded_task is not None and str(recorded_task) != str(task_id):
            continue
        for block in getattr(message, "blocks", ()):
            if not isinstance(block, CapabilityResultBlock):
                continue
            item = result_qualifies_as_work_evidence(block)
            if item is not None and item.call_id not in {entry.call_id for entry in evidence}:
                evidence.append(item)
    return evidence


# Compatibility for integrations that imported the old private helper.
def _compiled_has_observed_work(compiled: CompiledContext, task_id: str) -> bool:
    return bool(_compiled_work_evidence(compiled, task_id))


def _estimate_input_tokens(
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> int | None:
    """Return the hard-admission bound for the complete request envelope."""
    return (estimator or ModelTokenEstimator()).upper_bound(request)


def _display_input_estimate(request: ModelRequest) -> int:
    """Cheap fallback for usage display when a profile declares no bound."""
    text = (
        (request.system or "")
        + "\n"
        + "\n".join(
            (
                message.conversation_text()
                if callable(getattr(message, "conversation_text", None))
                else message.text()
            )
            or ""
            for message in request.messages
        )
    )
    return max(1, (len(text) + 3) // 4)


def _output_tokens_of(response: ModelResponse) -> int:
    """Return real output-token count when reported; else a chars/4 estimate.

    Never approximates token count by block count (§19).
    """
    try:
        usage = response.usage
    except AttributeError:
        usage = None
    count = int(getattr(usage, "output_tokens", None) or 0)
    if count > 0:
        return count
    return sum(len(getattr(b, "text", None) or "") for b in response.blocks) // 4


def _actual_model_cost(
    info,
    response: ModelResponse,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> Decimal | None:
    """Prefer provider cost, then declared model pricing, else unknown."""
    usage = getattr(response, "usage", None)
    reported = getattr(usage, "cost_usd", None)
    if reported is not None:
        try:
            return Decimal(str(reported))
        except (TypeError, ValueError):
            pass
    pricing = getattr(info, "cost", None)
    if pricing is None:
        return None
    # A zero-token response legitimately costs zero when pricing is known.
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) or _estimate_input_tokens(
        request, estimator=estimator
    )
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) or _output_tokens_of(response)
    if input_tokens is None:
        return None
    cache_read = int(getattr(usage, "cache_read_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_write_tokens", 0) or 0)
    uncached = getattr(usage, "uncached_input_tokens", None)
    if uncached is None:
        uncached = max(input_tokens - cache_read - cache_write, 0)
    else:
        uncached = max(int(uncached), 0)
    if pricing.per_1m_input is None and uncached:
        return None
    if cache_read and pricing.per_1m_cache_read_input is None:
        return None
    if cache_write and pricing.per_1m_cache_write_input is None:
        return None
    if pricing.per_1m_output is None and output_tokens:
        return None
    return (
        Decimal(str(pricing.per_1m_input or 0)) * uncached
        + Decimal(str(pricing.per_1m_cache_read_input or 0)) * cache_read
        + Decimal(str(pricing.per_1m_cache_write_input or 0)) * cache_write
        + Decimal(str(pricing.per_1m_output or 0)) * output_tokens
    ) / Decimal(1_000_000)


def _worst_case_cost(
    info,
    request: ModelRequest,
    *,
    estimator: ModelTokenEstimator | None = None,
) -> Decimal | None:
    """Estimate the bounded maximum cost from the actual compiled request."""
    input_tokens = _estimate_input_tokens(request, estimator=estimator)
    output_tokens = request.max_tokens or getattr(info, "max_output_tokens", None) or 4096
    pricing = getattr(info, "cost", None)
    if pricing is None or input_tokens is None:
        return None
    if pricing.per_1m_input is None or pricing.per_1m_output is None:
        return None
    cache_mode = str((request.metadata or {}).get("cache_mode") or "none").strip().lower()
    input_rates = [Decimal(str(pricing.per_1m_input))]
    if cache_mode not in {"", "none", "off"}:
        if pricing.per_1m_cache_read_input is None or pricing.per_1m_cache_write_input is None:
            return None
        input_rates.extend(
            [
                Decimal(str(pricing.per_1m_cache_read_input)),
                Decimal(str(pricing.per_1m_cache_write_input)),
            ]
        )
    input_rate = max(input_rates)
    output_rate = Decimal(str(pricing.per_1m_output))
    return (input_rate * input_tokens + output_rate * output_tokens) / Decimal(1_000_000)


def _budget_exhausted(state: RunState, budget: ResourceBudget) -> bool:
    if budget.max_agent_iterations and state.iterations >= budget.max_agent_iterations:
        return True
    if budget.max_input_tokens is not None and state.input_tokens >= budget.max_input_tokens:
        return True
    if budget.max_output_tokens is not None and state.output_tokens >= budget.max_output_tokens:
        return True
    if budget.max_cost_usd is not None and state.cost >= budget.max_cost_usd:
        return True
    wall_remaining = getattr(state, "budget_wall_time_remaining_s", None)
    if wall_remaining is not None:
        if wall_remaining <= 0:
            return True
    elif budget.max_wall_time is not None:
        if state.elapsed_ms >= int(budget.max_wall_time.total_seconds() * 1000):
            return True
    return False


def _is_retryable(exc: ProviderError) -> bool:
    return bool(getattr(exc, "retryable", False))


def _denied_result_withcall(call_id, capability_id) -> CapabilityResultBlock:
    return CapabilityResultBlock(
        call_id=call_id,
        capability_id=capability_id,
        ok=False,
        error="denied: approval not granted",
    )
