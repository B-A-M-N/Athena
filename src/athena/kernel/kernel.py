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
from typing import Any

from athena.context.compiler import CompiledContext, ContextCompiler
from athena.models.registry import ProviderRegistry
from athena.models.router import (
    CAP_AUDIO_INPUT,
    CAP_REASONING,
    CAP_TOOLS,
    CAP_VISION,
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
from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityRequestOrigin,
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
    TrustClass,
    utcnow,
)
from athena.protocol.models import (
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
    PrivacyClass,
)
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
from athena.interpreter.context import InterpreterContext  # noqa: F401 (annotation)

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
                    arguments=dict(b.arguments or {}), candidate=b.candidate,
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
    tool_correction_counts: dict[str, int] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> int:
        return int((utcnow() - self.start).total_seconds() * 1000)


# --------------------------------------------------------------------------- #
# Message / result builders
# --------------------------------------------------------------------------- #
def _assistant_message(task: TaskSpec, response: ModelResponse) -> Message:
    """Build the durable assistant message preserving ALL blocks.

    A mixed assistant response — text + capability calls, or reasoning +
    text + tool calls — must keep every block: providers (Anthropic
    tool_use, OpenAI tool_calls) require the assistant turn to contain the
    call before the matching result arrives. Stripping non-text blocks
    breaks provider replay (BHV provider-history invariant).
    """
    blocks = tuple(response.blocks or ())
    metadata: dict[str, Any] = (
        {"session_id": task.session_id} if task.session_id else {}
    )
    try:
        from athena.models.compat.caching import InferenceReceipt

        receipt = InferenceReceipt(
            call_id=response.request_id,
            provider_profile_id=str(
                response.metadata.get("provider_profile_id", response.provider)
            ),
            model_id=response.model,
            response_id=response.metadata.get("response_id"),
            tool_ids=tuple(
                b.call_id for b in response.blocks
                if isinstance(b, CapabilityCallBlock) and b.call_id
            ),
            provider_metadata=dict(response.metadata),
            usage=(dict(vars(response.usage)) if response.usage is not None else {}),
        )
        metadata["inference_receipt"] = receipt.to_dict()
    except Exception as exc:
        _logger.warning("could not build inference receipt: %s", exc)
    return Message(
        id=new_id("msg"),
        role=Role.ASSISTANT,
        blocks=blocks,
        created_at=utcnow(),
        provenance=Provenance(source_type=SourceType.GENERATED, trust=TrustClass.AGENT_CURATED),
        metadata=metadata,
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


def _deny_result_for_request(request: CapabilityRequest) -> CapabilityResultBlock:
    return CapabilityResultBlock(
        call_id=request.call_id,
        capability_id=request.capability_id,
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
        continuation_store=None,
        router: "ModelRouter",
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
        self._continuation_store = continuation_store
        self._lifecycle = TaskLifecycle(manager=task_manager)
        if budgets is not None:
            self._lifecycle.set_budget_tracker(budgets)
        if cancellations is not None:
            self._lifecycle.set_cancellation_manager(cancellations)

        self._runs: dict[str, RunState] = {}
        self._resume: dict[str, asyncio.Event] = {}
        self._resume_decision: dict[str, str] = {}
        self._stored_responses: set[str] = set()
        self._prefix_trackers: dict[tuple[str, str], Any] = {}

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

            # If approval was resolved while this process was down, the
            # service requeues the same task. Consume the durable canonical
            # call before asking the model for another turn; otherwise the
            # assistant tool call would be replayed as a new request and could
            # be repaired/executed twice.
            await self._resume_durable_continuation(task)

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
        metadata = self._inference_metadata(selection)
        cache_metadata = await self._observe_prefix(task, compiled, selection)
        if cache_metadata.get("boundary") is not None:
            await self._emit("CacheBoundary", cache_metadata["boundary"], task)
        metadata.update(cache_metadata)
        replay_metadata = self._replay_metadata(compiled, selection)
        if replay_metadata.get("boundary") is not None:
            await self._emit("InferenceReplayBoundary", {
                "boundary": replay_metadata["boundary"],
                "provider": selection.provider,
                "model": selection.model,
            }, task)
        metadata.update(replay_metadata)
        request = compiled.to_request(
            provider=selection.provider,
            model=selection.model,
            request_id=new_id("call"),
            metadata={"task_id": task.id, "session_id": task.session_id, **metadata},
        )
        state.request_id = request.request_id
        state.provider = selection.provider
        # P0.3: every inference subturn must be inspectable — the policy role
        # that requested this model travels on the event and the usage record
        # so `athena inspect` can prove which model served each subturn.
        role = getattr(task.model_policy, "role", None) or "primary"
        await self._emit("ModelRequestStarted", {
            "provider": selection.provider, "model": selection.model,
            "provider_profile_id": metadata.get("provider_profile_id"),
            "prefix_fingerprint": metadata.get("prefix_fingerprint"),
            "role": role,
            "request_id": request.request_id,
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
                    metadata={"inference": dict(self._inference_metadata(selection)),
                              "role": role},
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
                metadata={"task_id": task.id, "session_id": task.session_id,
                          **self._inference_metadata(selection_for_attempt),
                          **(await self._observe_prefix(
                              task, compiled, selection_for_attempt)),
                          **self._replay_metadata(compiled, selection_for_attempt)},
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
                            metadata={
                                "inference": dict(self._inference_metadata(selection_for_attempt)),
                                "usage": dict(vars(usage)) if usage is not None else {},
                            },
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

    # ------------------------------------------------------------------ #
    # Interpreter fusion broker (audit P0.2 / P0.4)
    # ------------------------------------------------------------------ #
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
        * emits its own ModelRequestStarted/Completed events with
          role="interpreter" so `athena inspect` shows it as its own row,
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
        compiled = await self._compile_for_prompts(
            subturn_task, system=system_prompt, user_prompt=user_prompt
        )
        selection = await self._select_model(subturn_task, compiled)
        await self._emit("ModelRequestStarted", {
            "provider": selection.provider,
            "model": selection.model,
            "role": "interpreter",
            "subturn": True,
        }, task)
        response = await self._invoke(subturn_task, state, selection, compiled)
        await self._emit("ModelResponseCompleted", {
            "provider": selection.provider,
            "model": selection.model,
            "role": "interpreter",
            "subturn": True,
        }, task)
        return response

    async def _compile_for_prompts(
        self, task: TaskSpec, *, system: str, user_prompt: str
    ) -> CompiledContext:
        """Compile a one-off prompt pair without touching durable history."""
        from athena.protocol.messages import Role, TextBlock

        user_message = Message(
            id=new_id("msg"),
            role=Role.USER,
            blocks=(TextBlock(text=user_prompt),),
            created_at=utcnow(),
            provenance=Provenance(
                source_type=SourceType.SYSTEM, trust=TrustClass.CONFIGURED_INSTRUCTION
            ),
        )
        return await self._compiler.compile(
            task, system=system, recent_messages=[user_message]
        )

    def _inference_metadata(self, selection: ModelSelection) -> dict[str, Any]:
        profile = self._registry.profile_for(selection.provider)
        if profile is None:
            return {"provider_profile_id": selection.provider}
        fingerprint = getattr(profile, "fingerprint", None)
        profile_fingerprint = fingerprint() if callable(fingerprint) else str(
            getattr(profile, "id", selection.provider)
        )
        profile_id = str(getattr(profile, "id", selection.provider))
        model_profile = self._registry.model_profile_for(
            selection.provider, selection.model)
        from athena.models.compat.profiles import resolve_compatibility_profile

        compatibility = resolve_compatibility_profile(
            str(getattr(profile, "compatibility_profile", "auto"))
        )
        return {
            # ID is the stable configured route identity. The fingerprint is
            # separate because changing a route's wire semantics must still
            # create an explicit cache/replay boundary for the same ID.
            "provider_profile_id": profile_id,
            "provider_profile_fingerprint": profile_fingerprint,
            "profile_id": getattr(profile, "id", selection.provider),
            "cache_mode": getattr(profile, "cache_mode", "none"),
            "cache_session_key": (
                f"{selection.provider}:{selection.model}"
                if getattr(profile, "cache_session_key", False)
                else None
            ),
            "compatibility_profile": getattr(
                profile, "compatibility_profile", "auto"
            ),
            "tool_repair_mode": compatibility.tool_repair,
            "max_tool_correction_cycles": compatibility.max_tool_correction_cycles,
            "protocol": getattr(profile, "protocol", "openai-compat"),
            "model_profile": (
                dict(vars(model_profile)) if model_profile is not None else None
            ),
        }

    async def _observe_prefix(
        self, task: TaskSpec, compiled: CompiledContext, selection: ModelSelection
    ) -> dict[str, Any]:
        """Observe the stable prompt prefix for this session/provider pair."""
        from athena.models.compat.caching import PrefixTracker, PromptEnvelope

        session_key = task.session_id or task.id
        metadata = self._inference_metadata(selection)
        # Keep the tracker stable across profile revisions so it can emit a
        # provider-profile boundary instead of silently starting a new tracker.
        key = (session_key, selection.provider)
        profile_id = str(metadata.get("provider_profile_id", selection.provider))
        tracker = self._prefix_trackers.setdefault(key, PrefixTracker())
        if tracker.last_prefix_fp is None and self._events is not None:
            try:
                for event in reversed(await self._events.list_for_session(session_key)):
                    if event.type != "InferencePrefixObserved":
                        continue
                    payload = dict(event.payload or {})
                    tracker.last_prefix_fp = payload.get("prefix_fingerprint")
                    tracker.last_full_fp = payload.get("full_fingerprint")
                    tracker.components_fp = dict(payload.get("components_fp") or {})
                    break
            except Exception as exc:
                _logger.debug("prefix tracker restore failed for %s: %s", session_key, exc)
        system_blocks = [m.text() for m in compiled.messages if m.role.value == "system"]
        envelope = PromptEnvelope(
            stable_prefix=[system_blocks, [d.input_schema for d in compiled.capability_definitions]],
            append_history=[m.id for m in compiled.messages],
            dynamic_suffix=[task.objective],
        )
        observed = tracker.observe(
            envelope,
            components={
                "system_prompt": system_blocks,
                "tools": [d.id for d in compiled.capability_definitions],
                "tool_schemas": [d.input_schema for d in compiled.capability_definitions],
                "model": selection.model,
                "provider_profile": metadata.get(
                    "provider_profile_fingerprint", profile_id),
            },
        )
        output = {
            "prefix_fingerprint": observed["prefix_fp"],
            "full_fingerprint": tracker.last_full_fp,
            "components_fp": dict(tracker.components_fp),
            "boundary": observed.get("boundary"),
            "cache_boundary": observed.get("boundary"),
            "cache_session_key": f"{session_key}:{profile_id}",
        }
        await self._emit("InferencePrefixObserved", output, task)
        return output

    def _replay_metadata(
        self, compiled: CompiledContext, selection: ModelSelection,
    ) -> dict[str, Any]:
        """Reload durable assistant-turn receipts for a resumed request.

        Canonical messages remain the source of truth for provider replay. The
        receipts are carried as request metadata for adapters/inspection and
        are compared against the selected route so a provider/model switch is
        an explicit replay boundary rather than an accidental continuation.
        """
        receipts: list[dict[str, Any]] = []
        for message in compiled.messages:
            value = (message.metadata or {}).get("inference_receipt")
            if isinstance(value, dict):
                receipts.append(dict(value))
        if not receipts:
            return {"replay_receipts": (), "replay_compatible": True}
        last = receipts[-1]
        current_profile = str(self._inference_metadata(selection).get(
            "provider_profile_id", selection.provider))
        last_profile = str(last.get("provider_profile_id") or "")
        last_model = str(last.get("model_id") or "")
        boundary = None
        if last_profile and last_profile != current_profile:
            boundary = {
                "reason": "provider_profile_changed",
                "from": last_profile,
                "to": current_profile,
            }
        elif last_model and last_model != selection.model:
            boundary = {
                "reason": "model_changed",
                "from": last_model,
                "to": selection.model,
            }
        return {
            "replay_receipts": tuple(receipts[-8:]),
            "replay_compatible": boundary is None,
            "replay_boundary": boundary,
            "boundary": boundary,
        }

    async def _consume(
        self, task: TaskSpec, state: RunState, provider, request: ModelRequest
    ) -> ModelResponse:
        accumulator = ModelResponseAccumulator(request)

        async for event in provider.complete(request):
            if state.cancel.is_set():
                raise RequestCancelled("task cancelled")
            accumulator.ingest(event)
            if event.type.value == "delta" and event.delta is not None:
                await self._relay_delta(task, event.delta)
            elif event.type.value == "reasoning" and event.delta is not None:
                await self._emit("ModelReasoningDelta", {}, task)
                if self._model_sink is not None and event.delta.reasoning:
                    await self._maybe_await(self._model_sink(event.delta.reasoning))
            elif event.type.value == "failed":
                raise ProviderError(event.error or "provider failed", code=event.code)

        # The accumulator is the only owner of final mixed-content assembly.
        final = accumulator.finish()
        # Providers own wire translation, but the request owns the canonical
        # inference identity. Carry it onto the response before the assistant
        # turn is persisted so durable receipts never fall back to a bare
        # provider name.
        response_metadata = dict(final.metadata)
        for key in (
            "task_id", "session_id", "provider_profile_id",
            "provider_profile_fingerprint", "profile_id", "model_id",
            "compatibility_profile", "model_profile", "protocol",
            "tool_repair_mode", "max_tool_correction_cycles",
            "cache_mode", "cache_session_key", "prefix_fingerprint",
            "full_fingerprint", "components_fp",
        ):
            if key in request.metadata and key not in response_metadata:
                response_metadata[key] = request.metadata[key]
        response_metadata["request_id"] = request.request_id
        from athena.models.compat.caching import UsageRecord

        usage_metadata = dict(final.usage.provider_metadata or {})
        raw_usage = usage_metadata.get("raw_usage")
        if isinstance(raw_usage, dict):
            if request.metadata.get("protocol") == "anthropic":
                normalized = UsageRecord.from_anthropic(raw_usage)
            else:
                normalized = UsageRecord.from_openai_compat(raw_usage)
        else:
            normalized = UsageRecord(
                prompt_tokens=final.usage.input_tokens,
                completion_tokens=final.usage.output_tokens,
                cache_read_tokens=final.usage.cache_read_tokens,
                cache_write_tokens=final.usage.cache_write_tokens,
                uncached_prompt_tokens=final.usage.uncached_input_tokens,
            )
        response_metadata["usage_record"] = normalized.to_dict()
        usage = replace(
            final.usage,
            provider_metadata={**usage_metadata, "normalized": normalized.to_dict()},
        )
        final = replace(final, usage=usage, metadata=response_metadata)
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
        # Bind the producing inference turn to repair receipts before any
        # capability request is translated or dispatched.
        dispatcher = getattr(shim, "_dispatcher", None)
        if dispatcher is not None and hasattr(dispatcher, "set_inference_provenance"):
            dispatcher.set_inference_provenance(
                provider_profile_id=response.metadata.get(
                    "provider_profile_id", response.provider),
                model_id=response.metadata.get("model_id", response.model),
                repair_mode=response.metadata.get("tool_repair_mode"),
            )
        outcome = await shim.dispatch(task, calls)

        if outcome.suspended:
            return await self._approval_path(task, state, outcome)

        await self._append_results(task, outcome.results)
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
            await self._emit("ToolInputCorrectionExhausted", {
                "capabilities": sorted(set(exhausted)),
                "max_cycles": max_cycles,
            }, task)
            return await self._finalize(
                task, state, TaskStatus.FAILED,
                "tool_input_invalid: correction budget exhausted",
            )
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
            await self._mark_continuations_consumed(outcome.suspended)
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
            for request in requests:
                # The model boundary already produced and validated these
                # canonical arguments. Approval replay must not run a future
                # repair-policy version over them.
                object.__setattr__(
                    request, "origin", CapabilityRequestOrigin.TRUSTED_ORCHESTRATION
                )
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
            await self._mark_continuations_consumed(suspended)
            return None
        return None

    async def _mark_continuations_consumed(self, suspended) -> None:
        if self._continuation_store is None:
            return
        for item in suspended:
            call_id = getattr(item, "call_id", None)
            if not call_id:
                continue
            try:
                await self._continuation_store.mark_consumed_for_call(call_id)
            except Exception as exc:
                _logger.warning("continuation consume failed for %s: %s", call_id, exc)

    async def _resume_durable_continuation(self, task: TaskSpec) -> None:
        if self._continuation_store is None or self._dispatch_factory is None:
            return
        try:
            record = await self._continuation_store.claim_resolved(task.id)
        except Exception as exc:
            _logger.warning("durable continuation lookup failed for %s: %s", task.id, exc)
            return
        if record is None:
            return

        call_id = str(record.get("call_id") or new_id("call"))
        request = CapabilityRequest(
            capability_id=str(record.get("capability_id") or ""),
            arguments=dict(record.get("canonical_arguments") or {}),
            task_id=task.id,
            session_id=task.session_id,
            call_id=call_id,
            # This is already canonical, durable Athena state. Re-running the
            # model compatibility repair here would make replay policy-version
            # dependent and violate the approval TOCTOU binding.
            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        )
        if record.get("decision") not in (None, "granted"):
            await self._append_results(task, [_deny_result_for_request(request)])
            await self._consume_durable_call(call_id)
            return

        try:
            shim = self._dispatch_factory(task)
            result = await shim._dispatcher.dispatch(
                request,
                workspace=shim._workspace,
                profile=shim._profile,
                task_policy=task.capability_policy,
            )
            if isinstance(result, SuspendedCall):
                await self._append_results(task, [
                    CapabilityResultBlock(
                        call_id=call_id,
                        capability_id=request.capability_id,
                        ok=False,
                        error="approval continuation could not be resumed",
                    )
                ])
                await self._release_durable_call(call_id)
            else:
                await self._append_results(task, [_to_result_block(result)])
                await self._consume_durable_call(call_id)
        except Exception as exc:
            await self._release_durable_call(call_id)
            await self._append_results(task, [
                CapabilityResultBlock(
                    call_id=call_id,
                    capability_id=request.capability_id,
                    ok=False,
                    error=f"approval continuation failed: {exc}",
                )
            ])

    async def _consume_durable_call(self, call_id: str) -> None:
        if self._continuation_store is None:
            return
        try:
            await self._continuation_store.mark_consumed_for_call(call_id)
        except Exception as exc:
            _logger.warning("continuation consume failed for %s: %s", call_id, exc)

    async def _release_durable_call(self, call_id: str) -> None:
        if self._continuation_store is None:
            return
        release = getattr(self._continuation_store, "release_claim", None)
        if release is None:
            return
        try:
            await release(call_id)
        except Exception as exc:
            _logger.warning("continuation claim release failed for %s: %s", call_id, exc)

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
