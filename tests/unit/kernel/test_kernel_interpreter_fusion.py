"""Loop-side interpreter fusion wiring (audit P0.2 completion).

Proves the production trigger at the kernel boundary: a FAILED capability
result becomes an observation, the extension gets ONE metered subturn, and
any proposal it returns dispatches through the canonical path — while a
kernel without the extension wired in behaves exactly as before (opt-in).
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.context.compiler import ContextCompiler
from athena.kernel.kernel import AgentKernel, RunState
from athena.kernel.termination import TerminationEvaluator
from athena.models.providers.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.interpreter import InterpreterExtension
from athena.protocol.ids import new_id
from athena.protocol.messages import CapabilityResultBlock
from athena.protocol.tasks import TaskSpec
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@dataclass
class Stack:
    db: Database
    tasks: TaskStore
    events: EventStore
    messages: MessageStore
    provider: FakeModelProvider
    kernel: AgentKernel


INTERPRETER_TEXT = (
    '{"capability_id": "runtime.evaluate", '
    '"arguments": {"code": "repr(obj)"}, '
    '"rationale": "inspect the failing object"}'
)

# The primary-loop model answers the task; the interpreter subturn model
# answers with the proposal. Match on the observation prompt marker.
PRIMARY_TEXT = "done"


def _scripts(*texts: str) -> list[dict]:
    return [{"match": {"user_contains": "Observation kind"}, "respond": {"text": t}}
            for t in texts]


async def _make_stack(*, interpreter) -> Stack:
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    provider = FakeModelProvider(
        scripts=_scripts(INTERPRETER_TEXT), model="fake-1", provider="fake",
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
        dispatch_factory=None,
        interpreter=interpreter,
    )
    return Stack(
        db=db, tasks=tasks, events=events, messages=messages,
        provider=provider, kernel=kernel,
    )


def _extension_for(kernel: AgentKernel) -> InterpreterExtension:
    async def broker(*, context, system_prompt, user_prompt):
        return await kernel.interpreter_subturn(
            context=context, system_prompt=system_prompt, user_prompt=user_prompt
        )
    return InterpreterExtension(inference_broker=broker)


async def _persisted_task(stack: Stack) -> TaskSpec:
    task = TaskSpec(
        id=new_id("task"), objective="fusion wiring", session_id="s-fusion",
    )
    await stack.db.execute(
        "INSERT OR IGNORE INTO sessions(id, parent_id, created_at, updated_at, metadata) "
        "VALUES (?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}')",
        (task.session_id,),
    )
    await stack.tasks.insert_task(task.id, task.session_id, None, task.objective)
    return task


class _RecordingShim:
    """Dispatch stand-in: first call fails a capability, later calls succeed."""

    def __init__(self, sink: list, fail_first: bool = True):
        self._sink = sink
        self._fail_first = fail_first
        self._calls = 0

    async def dispatch(self, task, calls):
        from athena.kernel.dispatch import DispatchResult

        self._calls += 1
        self._sink.extend(calls)
        results = []
        for c in calls:
            ok = True
            if self._fail_first and self._calls == 1:
                ok = False
            results.append(CapabilityResultBlock(
                call_id=c.call_id, capability_id=c.capability_id,
                ok=ok, output="ok" if ok else "",
                error=None if ok else "TypeError: boom",
            ))
        return DispatchResult(results=tuple(results))


async def _run_one_turn(stack: Stack, task: TaskSpec, shim) -> None:
    """Drive one primary-loop dispatch cycle manually (no full run_task)."""
    state = stack.kernel._runs.get(task.id) or RunState(task=task)
    stack.kernel._runs[task.id] = state
    response = _FakeResponse()
    calls = [_Call()]
    stack.kernel._dispatch_factory = lambda t: shim
    await stack.kernel._dispatch(task, state, response, calls)


@dataclass
class _FakeResponse:
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class _Call:
    call_id: str = "c-1"
    capability_id: str = "runtime.evaluate"
    arguments: dict = None

    def __post_init__(self):
        if self.arguments is None:
            self.arguments = {}


async def test_failed_result_triggers_interpreter_subturn_and_dispatch():
    stack = await _make_stack(interpreter="wired-marker")
    task = await _persisted_task(stack)
    # Wire the REAL extension through the kernel's broker.
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    dispatched: list = []
    shim = _RecordingShim(dispatched)
    await _run_one_turn(stack, task, shim)
    # The interpreter subturn ran exactly once (model_calls == 1) and its
    # proposal was dispatched through the same shim (second dispatch).
    state = stack.kernel._runs[task.id]
    assert state.model_calls == 1
    assert len(dispatched) == 2
    assert dispatched[1].capability_id == "runtime.evaluate"
    assert dispatched[1].arguments == {"code": "repr(obj)"}
    rows = await stack.events.list_for_task(task.id)
    roles = [e.payload.get("role") for e in rows
             if e.type == "ModelRequestStarted"]
    assert "interpreter" in roles
    proposal_events = [e for e in rows if e.type == "InterpreterProposalDispatched"]
    assert proposal_events, "canonical dispatch must be inspectable"
    await stack.db.close()


async def test_successful_results_do_not_trigger_interpreter():
    stack = await _make_stack(interpreter=None)
    task = await _persisted_task(stack)
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    dispatched: list = []
    shim = _RecordingShim(dispatched, fail_first=False)
    await _run_one_turn(stack, task, shim)
    assert len(dispatched) == 1  # primary dispatch only, no interpreter turn
    state = stack.kernel._runs[task.id]
    assert state.model_calls == 0  # no unmetered reasoning
    await stack.db.close()


async def test_no_extension_wired_is_noop():
    stack = await _make_stack(interpreter=None)
    task = await _persisted_task(stack)
    dispatched: list = []
    shim = _RecordingShim(dispatched)
    await _run_one_turn(stack, task, shim)
    assert len(dispatched) == 1
    state = stack.kernel._runs[task.id]
    assert state.model_calls == 0
    await stack.db.close()


async def test_interpreter_failure_never_kills_primary_loop():
    """A broken extension is logged and skipped, not fatal."""
    stack = await _make_stack(interpreter="marker")
    task = await _persisted_task(stack)

    class _BrokenExt:
        async def interpret(self, observation, context):
            raise RuntimeError("interpreter exploded")

    stack.kernel._interpreter = _BrokenExt()
    dispatched: list = []
    shim = _RecordingShim(dispatched)
    await _run_one_turn(stack, task, shim)
    # Primary dispatch result still appended; loop survived.
    assert len(dispatched) == 1
    state = stack.kernel._runs[task.id]
    assert state.model_calls == 0
    await stack.db.close()


async def test_cancellation_skips_fusion():
    stack = await _make_stack(interpreter="marker")
    task = await _persisted_task(stack)
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    state = stack.kernel._runs.get(task.id) or RunState(task=task)
    stack.kernel._runs[task.id] = state
    state.cancel.set()
    dispatched: list = []
    shim = _RecordingShim(dispatched)
    await _run_one_turn(stack, task, shim)
    # interpret() honours cancel_requested() and returns None — no subturn,
    # no proposal dispatch, primary loop unharmed.
    assert state.model_calls == 0
    assert len(dispatched) == 1
    await stack.db.close()
