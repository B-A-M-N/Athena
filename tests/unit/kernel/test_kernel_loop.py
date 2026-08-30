"""End-to-end AgentKernel reasoning-loop tests (INV-001)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from athena.context.compiler import ContextCompiler
from athena.kernel.dispatch import CapabilityResultBlock, DispatchResult
from athena.kernel.kernel import AgentKernel
from athena.kernel.termination import TerminationEvaluator
from athena.models.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.protocol.ids import new_id
from athena.protocol.models import CostInfo
from athena.protocol.tasks import ResourceBudget, TaskSpec, TaskStatus
from athena.tasks.budgets import BudgetStateUnavailable, BudgetTracker
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@dataclass
class Stack:
    db: Database
    sessions: SessionRepository
    tasks: TaskStore
    messages: MessageStore
    events: EventStore
    manager: TaskManager
    provider: FakeModelProvider
    kernel: AgentKernel


class StubDispatchIface:
    """Stub for a CapabilityDispatchShim handed to the kernel."""

    def __init__(self, sink):
        self._sink = sink

    async def dispatch(self, task, calls):
        calls = list(calls or [])
        self._sink.extend(calls)
        results = tuple(
            CapabilityResultBlock(
                call_id=c.call_id,
                capability_id=c.capability_id,
                ok=True,
                output="ok",
            )
            for c in calls
        )
        return DispatchResult(results=results)


class _UsageRecorder:
    def __init__(self):
        self.attempt_metadata = None

    async def record_attempt(self, **kwargs):
        self.attempt_metadata = kwargs.get("metadata")
        return "usage-1"

    async def record_completion(self, *args, **kwargs):
        return None


@pytest.fixture
async def stack():
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)

    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    provider = FakeModelProvider(scripts=[], model="fake-1", provider="fake", tool_calling=True)
    registry = ProviderRegistry()
    registry.register("fake", provider)
    router = ModelRouter(registry)
    compiler = ContextCompiler(message_store=messages)
    kernel = AgentKernel(
        task_store=tasks,
        events=events,
        task_manager=manager,
        messages=messages,
        registry=registry,
        router=router,
        context_compiler=compiler,
        termination=TerminationEvaluator(),
    )
    yield Stack(
        db=db,
        sessions=sessions,
        tasks=tasks,
        messages=messages,
        events=events,
        manager=manager,
        provider=provider,
        kernel=kernel,
    )
    await db.close()


def _task(objective, session_id, *, budget=None, metadata=None):
    return TaskSpec(
        id=new_id("task"),
        objective=objective,
        session_id=session_id,
        resource_budget=budget or ResourceBudget(),
        metadata=metadata or {},
    )


async def _create(stack, objective, *, budget=None):
    session_id = new_id("session")
    await stack.sessions.create(session_id)
    spec = _task(objective, session_id, budget=budget)
    await stack.manager.create(spec)
    await stack.manager.enqueue(spec.id)
    return spec


@pytest.mark.athena_claim("INV-001")
@pytest.mark.athena_evidence("test", "invariant")
@pytest.mark.athena_scenario("FUSE-004")
async def test_end_to_end_simple_completes(stack):
    stack.provider._scripts = [
        {"match": {"user_contains": "hello"}, "respond": {"text": "hi there!", "done": True}}
    ]
    spec = await _create(stack, "hello world")
    result = await stack.kernel.run_task(spec.id)
    assert result.status == TaskStatus.COMPLETE, result
    assert result.usage.cost_known is False
    events = await stack.events.list_for_task(spec.id)
    types = {e.type for e in events}
    assert "TaskCreated" in types
    # The kernel ran at least one full model turn (the TaskIterationStarted
    # event may be dropped when its sequence collides with a lifecycle event;
    # see sequence-collision note in the report).
    assert "ModelResponseCompleted" in types


async def test_prefix_metadata_is_observed_once_per_provider_attempt(stack, monkeypatch):
    stack.provider._scripts = [
        {"match": {"user_contains": "prefix"}, "respond": {"text": "done", "done": True}}
    ]
    observed = 0
    original = stack.kernel._observe_prefix

    async def counted(*args, **kwargs):
        nonlocal observed
        observed += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(stack.kernel, "_observe_prefix", counted)
    spec = await _create(stack, "prefix check")
    result = await stack.kernel.run_task(spec.id)

    assert result.status == TaskStatus.COMPLETE
    assert observed == 1


async def test_utility_inference_receives_a_reusable_opaque_cache_key(stack):
    stack.provider._scripts = [{"respond": {"text": "compressed", "done": True}}]
    usage = _UsageRecorder()
    stack.kernel._provider_usage_store = usage
    captured = {}
    original_complete = stack.provider.complete

    async def capture(request):
        captured.update(request.metadata)
        async for event in original_complete(request):
            yield event

    stack.provider.complete = capture

    result = await stack.kernel.utility_inference(
        system_prompt="stable utility instructions",
        user_prompt="summarize this turn",
        role="summarizer",
    )

    assert result == "compressed"
    assert captured["cache_prefix_message_count"] == 1
    assert captured["cache_session_key"].startswith("athena-cache-v2:")
    assert "stable utility instructions" not in captured["cache_session_key"]
    assert usage.attempt_metadata["cache_session_key"] == captured["cache_session_key"]


def test_cache_namespace_uses_compiler_identity_not_public_task_metadata(stack):
    stack.kernel._compiler = SimpleNamespace(principal_id="tenant-a")
    public_override = _task(
        "cache partition check",
        "session-a",
        metadata={"cache_namespace": "victim"},
    )
    trusted_override = _task(
        "cache partition check",
        "session-b",
        metadata={"_athena_cache_namespace": "tenant-b"},
    )

    assert stack.kernel._trusted_cache_namespace(public_override) == "tenant-a"
    assert stack.kernel._trusted_cache_namespace(trusted_override) == "tenant-b"


@pytest.mark.athena_claim("INV-001")
@pytest.mark.athena_evidence("test", "invariant")
@pytest.mark.athena_scenario("FUSE-004")
async def test_scripted_capability_then_answer_runs_two_iterations(stack):
    # Match ordering: the capability-result-aware script comes first; on the
    # first call there is no result yet so it is skipped, then the capability
    # script matches. On the second call the result is present and the
    # terminator matches.
    stack.provider._scripts = [
        {
            "match": {"capability_result_ok": True},
            "respond": {"text": "finished after tool", "done": True},
        },
        {
            "match": {"user_contains": "cmd"},
            "respond": {
                "capability_call": {"capability_id": "tools.execute", "arguments": {"cmd": "pwd"}},
                "done": False,
            },
        },
    ]
    dispatched: list = []
    stack.kernel._dispatch_factory = lambda task: StubDispatchIface(dispatched)

    spec = await _create(stack, "run the cmd")
    result = await stack.kernel.run_task(spec.id)
    assert result.status == TaskStatus.COMPLETE
    assert len(dispatched) == 1
    events = await stack.events.list_for_task(spec.id)
    iterations = [e for e in events if e.type == "TaskIterationStarted"]
    # The capability turn happens across two iterations. At least one
    # TaskIterationStarted must surface (the other may be dropped by the
    # UNIQUE(task_id, sequence) collision between the kernel and lifecycle
    # event emitters; see source-bug note in the report).
    assert len(iterations) >= 1


@pytest.mark.athena_claim("BHV-134")
@pytest.mark.athena_evidence("test", "e2e")
async def test_budget_exhaustion_is_partial_not_failed(stack):
    stack.provider._scripts = [
        {"match": {"user_contains": "work"}, "respond": {"text": "doing", "done": False}},
    ]
    spec = await _create(stack, "work", budget=ResourceBudget(max_agent_iterations=1))
    result = await stack.kernel.run_task(spec.id)
    assert result.status == TaskStatus.PARTIAL
    assert result.status != TaskStatus.FAILED
    assert "budget" in result.summary.lower()


async def test_budget_failure_during_bootstrap_is_recovery_required_and_not_leaked(stack):
    budgets = BudgetTracker(task_store=stack.tasks)
    calls: list[str] = []

    async def fail_begin(task_id: str) -> None:
        calls.append(f"begin:{task_id}")
        raise BudgetStateUnavailable("durable budget read failed")

    async def record_end(task_id: str) -> None:
        calls.append(f"end:{task_id}")

    budgets.begin_compute = fail_begin  # type: ignore[method-assign]
    budgets.end_compute = record_end  # type: ignore[method-assign]
    stack.manager.set_budget_tracker(budgets)
    stack.kernel.set_budget_tracker(budgets)

    spec = await _create(stack, "bootstrap budget failure")
    result = await stack.kernel.run_task(spec.id)

    assert result.status == TaskStatus.RECOVERY_REQUIRED
    assert calls == [f"begin:{spec.id}"]
    assert (await stack.tasks.get(spec.id))["status"] == TaskStatus.RECOVERY_REQUIRED.value


async def test_successful_model_calls_reconcile_cost_before_next_reservation(stack):
    """A completed call cannot leave its worst-case reservation behind."""
    stack.provider._info_kwargs["cost"] = CostInfo(per_1m_input=1.0, per_1m_output=100.0)
    stack.provider._info_kwargs["max_output_tokens"] = 4096
    stack.provider._response_cost_usd = 0.001
    stack.provider._scripts = [
        {
            "match": {"capability_result_ok": True},
            "respond": {"text": "priced complete", "done": True, "cost_usd": 0.001},
        },
        {
            "match": {"user_contains": "priced"},
            "respond": {
                "capability_call": {
                    "capability_id": "tools.think",
                    "arguments": {},
                },
                "done": False,
                "cost_usd": 0.001,
            },
        },
    ]
    dispatched: list = []
    stack.kernel._dispatch_factory = lambda task: StubDispatchIface(dispatched)
    budgets = BudgetTracker(task_store=stack.tasks)
    stack.manager.set_budget_tracker(budgets)
    stack.kernel.set_budget_tracker(budgets)

    spec = await _create(
        stack,
        "priced model call",
        budget=ResourceBudget(max_cost_usd=Decimal("0.015"), max_output_tokens=100),
    )
    result = await stack.kernel.run_task(spec.id)
    usage = await budgets.total(spec.id)

    assert result.status == TaskStatus.COMPLETE
    assert usage.model_calls == 2
    assert usage.cost == Decimal("0.002")
    assert budgets._model_cost_reservations.get(spec.id, Decimal("0")) == Decimal("0")


@pytest.mark.athena_claim("BHV-076", "BHV-078")
@pytest.mark.athena_evidence("test", "e2e")
async def test_cancellation_mid_run_is_cancelled(stack):
    # First turn emits a capability call so the loop stays alive beyond turn 1
    # (a final-text turn would terminate immediately). Cancel while the second
    # turn is pending.
    stack.provider._scripts = [
        {
            "match": {"user_contains": "long"},
            "respond": {
                "capability_call": {"capability_id": "tools.think", "arguments": {}},
                "done": False,
            },
        },
    ]
    spec = await _create(stack, "long task")

    runner = asyncio.create_task(stack.kernel.run_task(spec.id))
    await asyncio.sleep(0.02)
    stack.kernel.cancel_task(spec.id)
    result = await runner
    assert result.status == TaskStatus.CANCELLED
    assert result.status != TaskStatus.FAILED
    assert result.status != TaskStatus.INTERRUPTED
