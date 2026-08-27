"""Interpreter fusion tests (audit P0.2/P0.4 — FUSE family).

Proves the architectural contract at its public boundaries:

1. the extension proposes capabilities via the kernel broker — it never
   touches providers, subprocesses, or stores itself;
2. interpreter subturns are metered into the SAME RunState counters and
   observe the SAME cancellation token (no unmetered reasoning);
3. subturns emit role-tagged inference events visible to `athena inspect`;
4. oversized observations are refused, not truncated;
5. malformed model answers yield no proposal rather than a guess;
6. proposals carry no execution authority — dispatch goes through the
   canonical CapabilityRequest path.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from athena.context.compiler import ContextCompiler
from athena.kernel.kernel import AgentKernel, RunState
from athena.kernel.termination import TerminationEvaluator
from athena.models.providers.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.interpreter import (
    InterpreterContext,
    InterpreterExtension,
    InterpreterObservation,
    InterpreterProposal,
)
from athena.protocol.errors import RequestCancelled
from athena.protocol.ids import new_id
from athena.protocol.tasks import TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
@dataclass
class Stack:
    db: Database
    tasks: TaskStore
    events: EventStore
    messages: MessageStore
    registry: ProviderRegistry
    provider: FakeModelProvider
    kernel: AgentKernel
    manager: TaskManager


PROPOSAL_TEXT = (
    '{"capability_id": "runtime.evaluate", '
    '"arguments": {"code": "repr(obj)"}, '
    '"rationale": "inspect the failing object"}'
)


def _scripts(*texts: str) -> list[dict]:
    return [{"match": {"user_contains": "Observation kind"}, "respond": {"text": t}}
            for t in texts]


@pytest.fixture
async def stack():
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    provider = FakeModelProvider(
        scripts=_scripts(PROPOSAL_TEXT), model="fake-1", provider="fake",
        tool_calling=True,
    )
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
        db=db, tasks=tasks, events=events, messages=messages,
        registry=registry, provider=provider, kernel=kernel, manager=manager,
    )
    await db.close()


def _task(objective: str = "interpret runtime observations") -> TaskSpec:
    return TaskSpec(id=new_id("task"), objective=objective, session_id="s-test")


async def _persisted_task(stack) -> TaskSpec:
    """Create the session + task rows — events enforce FK on task_id."""
    task = _task()
    await stack.db.execute(
        "INSERT OR IGNORE INTO sessions(id, parent_id, created_at, updated_at, metadata) "
        "VALUES (?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}')",
        (task.session_id,),
    )
    await stack.tasks.insert_task(
        task.id, task.session_id, None, task.objective
    )
    return task


def _observation(kind: str = "runtime.evaluate", **payload) -> InterpreterObservation:
    return InterpreterObservation(
        kind=kind,
        payload=payload or {"output": "TypeError: 'NoneType' is not subscriptable"},
        task_id="t-1",
        session_id="s-1",
    )


def _context(kernel, task) -> InterpreterContext:
    state = kernel._runs.get(task.id)
    if state is None:
        state = RunState(task=task)
        kernel._runs[task.id] = state
    return InterpreterContext(task_id=task.id, session_id=task.session_id, run_state=state)


def _extension(stack) -> InterpreterExtension:
    async def broker(*, context, system_prompt, user_prompt):
        return await stack.kernel.interpreter_subturn(
            context=context, system_prompt=system_prompt, user_prompt=user_prompt
        )
    return InterpreterExtension(inference_broker=broker)


# --------------------------------------------------------------------- #
# contract tests
# --------------------------------------------------------------------- #
async def test_extension_parses_proposal_from_broker_response(stack):
    ext = _extension(stack)
    proposal = await ext.interpret(_observation(), _context(stack.kernel, await _persisted_task(stack)))
    assert proposal is not None
    assert proposal.capability_id == "runtime.evaluate"
    assert proposal.arguments == {"code": "repr(obj)"}


async def test_extension_returns_none_on_empty_proposal(stack):
    stack.provider._scripts = _scripts("{}")
    ext = _extension(stack)
    assert await ext.interpret(_observation(), _context(stack.kernel, await _persisted_task(stack))) is None


async def test_extension_refuses_oversized_observation_without_broker_call(stack):
    big = InterpreterObservation(kind="runtime.evaluate", payload={"output": "x" * 30_000})
    ext = _extension(stack)
    assert await ext.interpret(big, _context(stack.kernel, await _persisted_task(stack))) is None


async def test_extension_refuses_malformed_json_without_guessing(stack):
    stack.provider._scripts = _scripts("<not json at all>")
    ext = _extension(stack)
    assert await ext.interpret(_observation(), _context(stack.kernel, await _persisted_task(stack))) is None


async def test_extension_honours_cancellation(stack):
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    ctx.run_state.cancel.set()
    ext = _extension(stack)
    assert await ext.interpret(_observation(), ctx) is None


# --------------------------------------------------------------------- #
# metering + inspectability (P0.4 / P0.3)
# --------------------------------------------------------------------- #
async def test_interpreter_subturn_shares_budget_counters(stack):
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    ext = _extension(stack)
    await ext.interpret(_observation(), ctx)
    assert ctx.run_state.model_calls == 1


async def test_interpreter_subturn_emits_role_tagged_events(stack):
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    stack.provider._scripts = _scripts("{}")
    ext = _extension(stack)
    await ext.interpret(_observation(), ctx)
    rows = await stack.events.list_for_task(task.id)
    inference = [e for e in rows if e.type in ("ModelRequestStarted", "ModelResponseCompleted")]
    assert inference, "interpreter subturn must emit inference events"
    started = [e for e in inference if e.type == "ModelRequestStarted"]
    assert started and started[0].payload.get("role") == "interpreter"
    assert started[0].payload.get("subturn") is True


async def test_interpreter_subturn_cancel_raises(stack):
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    ctx.run_state.cancel.set()
    with pytest.raises(RequestCancelled):
        await stack.kernel.interpreter_subturn(
            context=ctx, system_prompt="s", user_prompt="u"
        )


async def test_interpreter_subturn_does_not_touch_durable_history(stack):
    """A subturn is a side read: no assistant message lands in the store."""
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    before = await stack.messages.list_session_messages(task.session_id)
    stack.provider._scripts = _scripts("{}")
    ext = _extension(stack)
    await ext.interpret(_observation(), ctx)
    after = await stack.messages.list_session_messages(task.session_id)
    assert len(after) == len(before)


# --------------------------------------------------------------------- #
# canonical dispatch of interpreter proposals (P0.2 completion)
# --------------------------------------------------------------------- #
class _RecordingShim:
    """Stand-in for CapabilityDispatchShim recording dispatched calls."""

    def __init__(self, sink):
        self._sink = sink

    async def dispatch(self, task, calls):
        from athena.kernel.dispatch import DispatchResult
        from athena.protocol.messages import CapabilityResultBlock
        calls = list(calls or ())
        self._sink.extend(calls)
        results = tuple(
            CapabilityResultBlock(
                call_id=c.call_id, capability_id=c.capability_id,
                ok=True, output="ok",
            )
            for c in calls
        )
        return DispatchResult(results=results)


async def test_proposal_dispatches_through_canonical_path(stack):
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    proposal = InterpreterProposal(
        capability_id="runtime.evaluate",
        arguments={"code": "repr(obj)"},
        rationale="inspect the failing object",
    )
    dispatched: list = []
    stack.kernel._dispatch_factory = lambda t: _RecordingShim(dispatched)
    outcome = await stack.kernel.dispatch_interpreter_proposal(proposal, ctx)
    assert outcome is not None and outcome.results
    assert len(dispatched) == 1
    assert dispatched[0].capability_id == "runtime.evaluate"
    assert dispatched[0].arguments == {"code": "repr(obj)"}
    rows = await stack.events.list_for_task(task.id)
    proposal_events = [e for e in rows if e.type == "InterpreterProposalDispatched"]
    assert proposal_events, "canonical dispatch must be inspectable"
    assert proposal_events[0].payload.get("role") == "interpreter"


async def test_proposal_dispatch_without_factory_is_none(stack):
    task = await _persisted_task(stack)
    ctx = _context(stack.kernel, task)
    stack.kernel._dispatch_factory = None
    proposal = InterpreterProposal(capability_id="runtime.evaluate", arguments={})
    assert await stack.kernel.dispatch_interpreter_proposal(proposal, ctx) is None
