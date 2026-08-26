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
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from athena.context.compiler import CompiledContext, ContextCompiler
from athena.models.registry import ProviderRegistry
from athena.models.router import (
    CAP_AUDIO_INPUT,
    CAP_REASONING,
    CAP_TOOLS,
    CAP_VISION,
    ModelRouter,
    ModelSelection,
    _candidate_key,
    _OFFLINE_PRIVACY,
)
from athena.protocol.errors import (
    ModelUnavailable,
    ProviderError,
    RequestCancelled,
    TaskBudgetExceeded,
    TaskDeadlineExceeded,
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
from athena.protocol.models import ModelDelta, ModelRequest, ModelResponse, PrivacyClass
from athena.protocol.tasks import (
    ResourceBudget,
    TaskResult,
    TaskSpec,
    TaskStatus,
    UsageSummary,
)
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.tasks import TaskStore

from athena.kernel.dispatch import DispatchResult, SuspendedCall
from athena.kernel.lifecycle import TaskLifecycle
from athena.kernel.termination import TerminationDecision, TerminationEvaluator

__all__ = ["AgentKernel"]

_logger = logging.getLogger("athena.kernel")

_FALLBACK_ATTEMPTS = 2


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
                blocks.append(_ResultTextBlock(
                    call_id=b.call_id, capability_id=b.capability_id, ok=b.ok,
                    output=b.output, error=b.error, metadata=b.metadata,
                    ref_uri=b.ref_uri,
                ))
                changed = True
            elif isinstance(b, CapabilityCallBlock) and not isinstance(b, _CallTextBlock):
                blocks.append(_CallTextBlock(
                    call_id=b.call_id, capability_id=b.capability_id,
                    arguments=dict(b.arguments or {}),
                ))
                changed = True
            else:
                blocks.append(b)
        if changed:
            out.append(Message(
                id=msg.id, role=msg.role, blocks=tuple(blocks),
                created_at=msg.created_at, provenance=msg.provenance,
                metadata=dict(msg.metadata or {}),
            ))
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
    request_id: str | None = None
    provider: str | None = None

    @property
    def elapsed_ms(self) -> int:
        return int((utcnow() - self.start).total_seconds() * 1000)


# --------------------------------------------------------------------------- #
# Message / result builders
# --------------------------------------------------------------------------- #
def _block_identity(block) -> tuple:
    """Stable identity for dedup of streamed vs DONE-response blocks."""
    kind = type(block).__name__
    if kind == "CapabilityCallBlock":
        return (kind, getattr(block, "call_id", ""))
    if hasattr(block, "text"):
        return (kind, hash(getattr(block, "text", "")))
    return (kind, id(block))


def _assistant_message(task: TaskSpec, response: ModelResponse) -> Message:
    """Build the durable assistant message preserving ALL blocks.

    A mixed assistant response — text + capability calls, or reasoning +
    text + tool calls — must keep every block: providers (Anthropic
    tool_use, OpenAI tool_calls) require the assistant turn to contain the
    call before the matching result arrives. Stripping non-text blocks
    breaks provider replay (BHV provider-history invariant).
    """
    blocks = tuple(response.blocks or ())
    return Message(
        id=new_id("msg"),
        role=Role.ASSISTANT,
        blocks=blocks,
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.GENERATED, trust=TrustClass.AGENT_CURATED),
        metadata={"session_id": task.session_id} if task.session_id else {},
    )


def _results_message(task: TaskSpec, blocks) -> Message:
    return Message(
        id=new_id("msg"),
        role=Role.CAPABILITY,
        blocks=tuple(blocks),
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.CAPABILITY),
        metadata={"session_id": task.session_id} if task.session_id else {},
    )


def _deny_result(suspended) -> CapabilityResultBlock:
    call_id = getattr(suspended, "call_id", "")
    req = getattr(suspended, "request", None)
    capability_id = getattr(req, "capability_id", "") if req is not None else ""
    return CapabilityResultBlock(
        call_id=call_id,
        capability_id=capability_id,
        ok=False,
        error="denied: approval not granted",
    )


def _to_result_block(result) -> CapabilityResultBlock:
    from athena.protocol.capabilities import CapabilityResultStatus
    if isinstance(result, CapabilityResultBlock):
        return result
    return CapabilityResultBlock(
        call_id=getattr(result, "call_id", ""),
        capability_id=getattr(result, "capability_id", ""),
        ok=(getattr(result, "status", None) is CapabilityResultStatus.OK),
        output=getattr(result, "output", "") or "",
        error=getattr(result, "error", None),
        metadata=getattr(result, "metadata", None) or {},
        ref_uri=getattr(result, "ref_uri", None),
    )


def _block_of(suspended) -> CapabilityCallBlock:
    req = getattr(suspended, "request", None)
    if req is None:
        return CapabilityCallBlock(call_id="", capability_id="", arguments={})
    return CapabilityCallBlock(
        call_id=getattr(req, "call_id", ""),
        capability_id=req.capability_id,
        arguments=dict(req.arguments or {}),
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
        router: "ModelRouter | None" = None,
    ) -> None:
        self._task_store = task_store
        self._events = events
        self._messages = messages
        self._registry = registry
        # ONE routing authority: the service-owned router carries role
        # policies; the kernel never builds its own (P1-23).
        self._router = router or ModelRouter(registry)
        self._compiler = context_compiler
        self._termination = termination
        self._model_sink = model_sink
        self._token_sink = token_sink
        self._dispatch_factory = dispatch_factory
        self._provider_usage_store = provider_usage_store
        self._lifecycle = TaskLifecycle(manager=task_manager)
        if budgets is not None:
            self._lifecycle.set_budget_tracker(budgets)
        if cancellations is not None:
            self._lifecycle.set_cancellation_manager(cancellations)

        self._runs: dict[str, RunState] = {}
        self._resume: dict[str, asyncio.Event] = {}
        self._resume_decision: dict[str, str] = {}
        self._stored_responses: set[str] = set()

    def set_budget_tracker(self, budgets) -> None:
        # Late-bind the budget authority (construction-order tolerant, §19).
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
        task = await self._lifecycle.acquire(task_id)
        state = self._runs.get(task_id) or RunState(task)
        self._runs[task_id] = state
        await self._bootstrap(task, state)
        try:
            return await self._loop(task, state)
        finally:
            self._runs.pop(task_id, None)

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
            except Exception:
                pass
        if state.request_id and state.provider:
            try:
                provider = self._registry.provider_for(state.provider)
                asyncio.create_task(provider.cancel(state.request_id))
            except Exception:
                pass

    async def notify_approval_resolved(self, task_id: str, decision: str) -> None:
        self._resume_decision[task_id] = decision
        self._resume.setdefault(task_id, asyncio.Event()).set()

    # ------------------------------------------------------------------ #
    # The loop — THE one reasoning loop (INV-001)
    # ------------------------------------------------------------------ #
    async def _loop(self, task: TaskSpec, state: RunState) -> TaskResult:
        budget = task.resource_budget or ResourceBudget()

        while True:
            try:
                await self._lifecycle.assert_runnable(task)
            except RequestCancelled:
                return await self._finalize(task, state, TaskStatus.CANCELLED,
                                            "task cancelled")
            except TaskBudgetExceeded:
                return await self._finalize(task, state, TaskStatus.PARTIAL,
                                            "resource budget exhausted")
            state.iterations += 1
            await self._emit("TaskIterationStarted", {"iteration": state.iterations}, task)

            if self._deadline_passed(task):
                return await self._finalize(task, state, TaskStatus.PARTIAL, "deadline exceeded")
            if _budget_exhausted(state, budget):
                return await self._finalize(task, state, TaskStatus.PARTIAL,
                                            "resource budget exhausted")

            compiled = await self._compile(task)
            selection = await self._select_model(task, compiled)

            try:
                response = await self._invoke(task, state, selection, compiled)
            except RequestCancelled:
                return await self._finalize(task, state, TaskStatus.CANCELLED, "task cancelled")
            except TaskDeadlineExceeded:
                return await self._finalize(task, state, TaskStatus.PARTIAL, "deadline exceeded")
            except ProviderError:
                return await self._finalize(task, state, TaskStatus.FAILED, "model unavailable")
            except Exception as exc:  # kernel never crashes; truthful terminal.
                return await self._finalize(task, state, TaskStatus.FAILED,
                                            f"kernel failure: {exc}")

            calls = [b for b in response.blocks if isinstance(b, CapabilityCallBlock)]

            if calls:
                # Persist the assistant turn (text/reasoning + calls) BEFORE
                # dispatch: provider history requires the tool_use/tool_call
                # to precede its result. The dispatch path appends results
                # after; ordering is the replay invariant.
                await self._append_response(task, response)
                outcome = await self._dispatch(task, state, response, calls)
                if outcome is not None:
                    return outcome
                continue

            decision = await self._termination.evaluate(
                task, response,
                iterations=state.iterations,
                max_iterations=budget.max_agent_iterations,
                budget_exhausted=_budget_exhausted(state, budget),
                cancelled=state.cancel.is_set(),
            )
            if decision.terminal:
                await self._append_final_response(task, response)
                return await self._finalize_decision(task, state, decision)

            await self._append_response(task, response)

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    async def _compile(self, task: TaskSpec) -> CompiledContext:
        recent = []
        if task.session_id:
            try:
                recent = await self._messages.list_session_messages(task.session_id)
            except Exception as exc:
                _logger.warning(
                    "context compilation fallback: could not load session messages "
                    "for %s: %s",
                    task.session_id, exc,
                )
                recent = []
        return await self._compiler.compile(
            task,
            recent_messages=_textable_messages(recent),
            workspace=task.workspace.root if task.workspace else None,
        )

    async def _select_model(
        self, task: TaskSpec, compiled: CompiledContext, *, exclude: frozenset[str] = frozenset()
    ) -> ModelSelection:
        from athena.models.router import ModelRequirements

        caps: set[str] = set()
        if getattr(compiled.requirements, "needs_tools", False):
            caps.add(CAP_TOOLS)
        if getattr(compiled.requirements, "vision", False):
            caps.add(CAP_VISION)
        if getattr(compiled.requirements, "audio", False):
            caps.add(CAP_AUDIO_INPUT)
        if getattr(compiled.requirements, "reasoning", False):
            caps.add(CAP_REASONING)

        requirements = ModelRequirements(
            required_capabilities=frozenset(caps),
            minimum_context_tokens=getattr(compiled.requirements, "min_context_window", None),
            max_output_tokens=getattr(compiled.requirements, "reserved_output", None),
        )
        selection = await self._router.select(
            policy=task.model_policy,
            requirements=requirements,
        )
        if selection.provider not in exclude:
            return selection

        # The router does not accept exclusions; if it re-chose a failed
        # provider, fall back to a registry scan honouring the same policy gate.
        policy = task.model_policy
        allowed = tuple(policy.allowed or ())
        offline = policy.privacy in _OFFLINE_PRIVACY
        models = list(await self._registry.list_models())
        field = {
            CAP_TOOLS: "tool_calling",
            CAP_VISION: "vision",
            CAP_AUDIO_INPUT: "audio_input",
            CAP_REASONING: "reasoning",
        }
        candidates: list = []
        for info in models:
            if info.provider in exclude:
                continue
            if allowed and info.id not in allowed and f"{info.provider}/{info.id}" not in allowed:
                continue
            for cap in caps:
                attr = field.get(cap)
                if attr is not None and not getattr(info, attr, False):
                    break
            else:
                if info.context_limit is not None and requirements.minimum_context_tokens is not None \
                        and info.context_limit < requirements.minimum_context_tokens:
                    continue
                if info.max_output_tokens is not None and requirements.max_output_tokens is not None \
                        and info.max_output_tokens < requirements.max_output_tokens:
                    continue
                if policy.require_tools and not info.tool_calling:
                    continue
                if offline and info.privacy_class is not PrivacyClass.LOCAL:
                    continue
                candidates.append(info)

        if not candidates:
            raise ModelUnavailable(
                f"no candidate model excludes failed providers {sorted(exclude)}")
        best = min(candidates, key=_candidate_key)
        return ModelSelection(provider=best.provider, model=best.id, info=best)

    async def _invoke(
        self, task: TaskSpec, state: RunState, selection: ModelSelection, compiled: CompiledContext
    ) -> ModelResponse:
        request = compiled.to_request(
            provider=selection.provider,
            model=selection.model,
            request_id=new_id("call"),
            metadata={"task_id": task.id, "session_id": task.session_id},
        )
        state.request_id = request.request_id
        state.provider = selection.provider
        await self._emit("ModelRequestStarted", {
            "provider": selection.provider, "model": selection.model,
        }, task)

        # Record provider attempt
        usage_id = None
        if self._provider_usage_store is not None:
            try:
                usage_id = await self._provider_usage_store.record_attempt(
                    provider=selection.provider,
                    model=selection.model,
                    task_id=task.id,
                    session_id=task.session_id,
                )
            except Exception:
                pass

        last_err: ProviderError | None = None
        attempted: set[str] = set()
        selection_for_attempt = selection
        for attempt in range(_FALLBACK_ATTEMPTS):
            if state.cancel.is_set():
                raise RequestCancelled("task cancelled")
            if selection_for_attempt.provider in attempted:
                raise last_err or ModelUnavailable(
                    f"no candidate model excludes failed providers {sorted(attempted)}")
            provider = self._registry.provider_for(selection_for_attempt.provider)
            request = compiled.to_request(
                provider=selection_for_attempt.provider,
                model=selection_for_attempt.model,
                request_id=new_id("call"),
                metadata={"task_id": task.id, "session_id": task.session_id},
            )
            state.request_id = request.request_id
            state.provider = selection_for_attempt.provider
            try:
                response = await self._consume(task, state, provider, request)
                await self._emit("ModelResponseCompleted", {
                    "provider": selection_for_attempt.provider,
                    "model": selection_for_attempt.model,
                }, task)
                state.model_calls += 1
                # Record final usage
                if self._provider_usage_store is not None and usage_id is not None:
                    try:
                        usage = response.usage if response else None
                        await self._provider_usage_store.record_completion(
                            usage_id,
                            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                        )
                    except Exception:
                        pass
                return response
            except ProviderError as exc:
                last_err = exc
                state.request_id = None
                if not _is_retryable(exc):
                    raise
                if attempt >= _FALLBACK_ATTEMPTS - 1:
                    break
                attempted.add(selection_for_attempt.provider)
                selection_for_attempt = await self._select_model(
                    task, compiled, exclude=frozenset(attempted))
        raise last_err or ModelUnavailable("no model available")

    async def _consume(
        self, task: TaskSpec, state: RunState, provider, request: ModelRequest
    ) -> ModelResponse:
        blocks: list[ContentBlock] = []
        final: ModelResponse | None = None

        async for event in provider.complete(request):
            if state.cancel.is_set():
                raise RequestCancelled("task cancelled")
            if event.type.value == "delta" and event.delta is not None:
                await self._relay_delta(task, event.delta)
                if event.delta.block is not None:
                    blocks.append(event.delta.block)
            elif event.type.value == "reasoning" and event.delta is not None:
                await self._emit("ModelReasoningDelta", {}, task)
                if self._model_sink is not None and event.delta.reasoning:
                    await self._maybe_await(self._model_sink(event.delta.reasoning))
            elif event.type.value == "done" and event.response is not None:
                final = event.response
            elif event.type.value == "failed":
                raise ProviderError(event.error or "provider failed", code=event.code)

        # ONE canonical assembly owner: the kernel merges streamed delta
        # blocks with the provider's DONE response. Providers may emit
        # tool-call blocks as deltas AND a DONE response carrying only
        # text; the merged view (deltas first, then any DONE-only blocks
        # not already present) is authoritative. Adapters never decide
        # alone which streamed blocks survive.
        if final is None:
            final = ModelResponse(
                request_id=request.request_id,
                model=request.model,
                provider=request.provider,
                blocks=tuple(blocks),
            )
        elif blocks:
            have = {_block_identity(b) for b in final.blocks}
            missing = [b for b in blocks if _block_identity(b) not in have]
            if missing:
                final = ModelResponse(
                    request_id=final.request_id, model=final.model,
                    provider=final.provider,
                    blocks=tuple(list(final.blocks) + missing),
                    finish_reason=final.finish_reason,
                )
        state.input_tokens += _input_tokens_of(final, request)
        state.output_tokens += _output_tokens_of(final)
        state.cost += _cost_of(final)
        return final

    async def _relay_delta(self, task: TaskSpec, delta: ModelDelta) -> None:
        if delta.reasoning:
            await self._emit("ModelReasoningDelta", {}, task)
        if self._token_sink is not None and delta.text:
            await self._maybe_await(self._token_sink(delta.text))
        if delta.text:
            await self._emit("ModelDelta", {"text": delta.text}, task)

    # ------------------------------------------------------------------ #
    # Capability dispatch path (INV-004)
    # ------------------------------------------------------------------ #
    async def _dispatch(self, task, state, response, calls):
        if self._dispatch_factory is None:
            not_executed = [
                CapabilityResultBlock(call_id=c.call_id, capability_id=c.capability_id,
                                      ok=False, error="capability path disabled")
                for c in calls
            ]
            await self._append_results(task, not_executed)
            return None

        shim = self._dispatch_factory(task)
        outcome = await shim.dispatch(task, calls)

        if outcome.suspended:
            return await self._approval_path(task, state, outcome)

        await self._append_results(task, outcome.results)
        return None

    async def _approval_path(self, task, state, outcome: DispatchResult) -> TaskResult | None:
        await self._transition(task, TaskStatus.WAITING_APPROVAL)
        ev = self._resume.setdefault(task.id, asyncio.Event())
        ev.clear()
        await self._emit("ApprovalRequested", {"calls": len(outcome.suspended)}, task)

        # Park until granted/denied (BHV-017) or cancelled (§20, BHV-017). No
        # spin: race the resume event against the cancellation token so an
        # external cancel wakes the task instead of leaving it hung forever.
        resume_task = asyncio.create_task(ev.wait())
        cancel_task = asyncio.create_task(state.cancel.wait())
        try:
            await asyncio.wait(
                {resume_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (resume_task, cancel_task):
                if not pending.done():
                    pending.cancel()
        if not resume_task.done() or state.cancel.is_set():
            return await self._finalize(task, state, TaskStatus.CANCELLED,
                                        "task cancelled during approval")
        decision = self._resume_decision.get(task.id, "denied")

        try:
            await self._transition(task, TaskStatus.RUNNING)
        except Exception:
            return await self._finalize_decision(
                task, state, TerminationDecision(True, "approval wait could not resume",
                                                 TaskStatus.BLOCKED))

        if decision in ("denied", "cancelled"):
            denied = [_deny_result(s) for s in outcome.suspended]
            await self._append_results(task, denied)
            return None

        if self._dispatch_factory is None:
            return None

        shim = self._dispatch_factory(task)
        dispatcher = getattr(shim, "_dispatcher", None)
        if dispatcher is None:
            blocks = [_block_of(s) for s in outcome.suspended]
            retried = await shim.dispatch(task, blocks)
            await self._append_results(task, retried.results)
            return None

        suspended = list(outcome.suspended)
        while suspended:
            requests = [s.request for s in suspended]
            items = await dispatcher.dispatch_many(
                requests,
                workspace=shim._workspace,
                profile=shim._profile,
                task_policy=task.capability_policy,
            )
            results = []
            re_ask: list = []
            for it in items:
                if isinstance(it, SuspendedCall):
                    re_ask.append(it)
                else:
                    results.append(_to_result_block(it))
            if re_ask:
                await self._append_results(task, results)
                return await self._approval_path(
                    task, state,
                    DispatchResult(results=(), suspended=tuple(re_ask)),
                )
            await self._append_results(task, results)
            return None
        return None

    # ------------------------------------------------------------------ #
    # Finalization
    # ------------------------------------------------------------------ #
    async def _finalize(self, task, state, status: TaskStatus, reason: str) -> TaskResult:
        return await self._lifecycle.finalize(
            task,
            status=status,
            reason=reason,
            usage=UsageSummary(
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                model_calls=state.model_calls,
                cost_usd=state.cost,
                duration_ms=state.elapsed_ms,
            ),
        )

    async def _finalize_decision(self, task, state, decision: TerminationDecision) -> TaskResult:
        return await self._lifecycle.finalize(
            task,
            decision=decision,
            usage=UsageSummary(
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                model_calls=state.model_calls,
                cost_usd=state.cost,
                duration_ms=state.elapsed_ms,
            ),
        )

    # ------------------------------------------------------------------ #
    # Persistence / event / misc helpers
    # ------------------------------------------------------------------ #
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

    async def _append_response(self, task: TaskSpec, response: ModelResponse) -> None:
        if response.request_id and response.request_id in self._stored_responses:
            return
        await self._messages.append(_assistant_message(task, response))
        if response.request_id:
            self._stored_responses.add(response.request_id)

    async def _append_final_response(self, task: TaskSpec, response: ModelResponse) -> None:
        """Persist a terminal text-only assistant answer to the session store.

        The non-terminal path appends assistant responses so resumed sessions
        see the animated transcript. A final answer (no capability calls) was
        previously never stored, so a resumed session missed it. Persist it here,
        guarding against double-append via ``_stored_responses``.
        """
        if response.request_id in self._stored_responses:
            return
        message = _assistant_message(task, response)
        if not any((getattr(b, "text", "") or "") for b in message.blocks):
            return
        await self._messages.append(message)
        self._stored_responses.add(response.request_id)

    async def _append_results(self, task: TaskSpec, blocks) -> None:
        if not blocks:
            return
        await self._messages.append(_results_message(task, blocks))

    async def _maybe_await(self, value) -> None:
        if inspect.isawaitable(value):
            await value


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #
def _input_tokens_of(response: ModelResponse, request: ModelRequest) -> int:
    """Return real input-token count when reported; else a chars/4 estimate."""
    try:
        usage = response.usage
    except AttributeError:
        usage = None
    count = int(getattr(usage, "input_tokens", None) or 0)
    if count > 0:
        return count
    return sum(len(m.text() or "") for m in request.messages) // 4


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


def _cost_of(response: ModelResponse) -> Decimal:
    try:
        usage = response.usage
    except AttributeError:
        return Decimal("0")
    if usage is None or not hasattr(usage, "cost_usd"):
        return Decimal("0")
    try:
        return Decimal(str(usage.cost_usd or "0"))
    except Exception:
        return Decimal("0")


def _budget_exhausted(state: RunState, budget: ResourceBudget) -> bool:
    if budget.max_agent_iterations and state.iterations >= budget.max_agent_iterations:
        return True
    if budget.max_input_tokens is not None and state.input_tokens >= budget.max_input_tokens:
        return True
    if budget.max_output_tokens is not None and state.output_tokens >= budget.max_output_tokens:
        return True
    if budget.max_cost_usd is not None and state.cost >= budget.max_cost_usd:
        return True
    if budget.max_wall_time is not None:
        if state.elapsed_ms >= int(budget.max_wall_time.total_seconds() * 1000):
            return True
    return False


def _is_retryable(exc: ProviderError) -> bool:
    return bool(getattr(exc, "retryable", False))


def _denied_result_withcall(call_id, capability_id) -> CapabilityResultBlock:
    return CapabilityResultBlock(
        call_id=call_id, capability_id=capability_id, ok=False,
        error="denied: approval not granted",
    )