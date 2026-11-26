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
    return [{"match": {"user_contains": "Observation kind"}, "respond": {"text": t}} for t in texts]


async def _make_stack(*, interpreter) -> Stack:
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    provider = FakeModelProvider(
        scripts=_scripts(INTERPRETER_TEXT),
        model="fake-1",
        provider="fake",
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
        db=db,
        tasks=tasks,
        events=events,
        messages=messages,
        provider=provider,
        kernel=kernel,
    )


def _extension_for(kernel: AgentKernel) -> InterpreterExtension:
    async def broker(*, context, system_prompt, user_prompt):
        return await kernel.interpreter_subturn(
            context=context, system_prompt=system_prompt, user_prompt=user_prompt
        )

    return InterpreterExtension(inference_broker=broker)


async def _persisted_task(stack: Stack) -> TaskSpec:
    task = TaskSpec(
        id=new_id("task"),
        objective="fusion wiring",
        session_id="s-fusion",
    )
    await stack.db.execute(
        "INSERT OR IGNORE INTO sessions(id, parent_id, created_at, updated_at, metadata) "
        "VALUES (?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}')",
        (task.session_id,),
    )
    await stack.tasks.insert_task(task.id, task.session_id, None, task.objective)
    return task


class _RecordingShim:
    """Dispatch stand-in: first call fails a capability, later calls succeed.

    Failures are observation-shaped (a sprawling traceback with a large
    output dump) because that is the class of failure the interpreter
    exists to condense. Concise failures do not trigger subturns — pinned
    separately in test_concise_failure_returns_directly.
    """

    def __init__(self, sink: list, fail_first: bool = True, fail_count: int = 1):
        self._sink = sink
        self._fail_first = fail_first
        self._fail_count = fail_count
        self._calls = 0

    async def dispatch(self, task, calls):
        from athena.kernel.dispatch import DispatchResult

        self._calls += 1
        self._sink.extend(calls)
        results = []
        for c in calls:
            ok = True
            if self._fail_first and self._calls <= self._fail_count:
                ok = False
            results.append(
                CapabilityResultBlock(
                    call_id=c.call_id,
                    capability_id=c.capability_id,
                    ok=ok,
                    output="ok" if ok else "x" * 3000,
                    error=None
                    if ok
                    else "Traceback (most recent call last):\n"
                    + "\n".join(
                        f'  File "mod_{i}.py", line {i}, in fn_{i}\n    raise RuntimeError("boom")'
                        for i in range(40)
                    ),
                )
            )
        return DispatchResult(results=tuple(results))


async def _run_one_turn(stack: Stack, task: TaskSpec, shim, calls=None) -> None:
    """Drive one primary-loop dispatch cycle manually (no full run_task)."""
    state = stack.kernel._runs.get(task.id) or RunState(task=task)
    stack.kernel._runs[task.id] = state
    response = _FakeResponse()
    calls = calls if calls is not None else [_Call()]
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
    roles = [e.payload.get("role") for e in rows if e.type == "ModelRequestStarted"]
    assert "interpreter" in roles
    proposal_events = [e for e in rows if e.type == "InterpreterProposalDispatched"]
    assert proposal_events, "canonical dispatch must be inspectable"
    await stack.db.close()


async def test_concise_failure_returns_directly():
    """P1-13: a short, legible failure does NOT spend an interpreter subturn.

    The primary transcript already carries the error verbatim; a subturn
    would only re-derive context the loop already has.
    """
    stack = await _make_stack(interpreter="wired-marker")
    task = await _persisted_task(stack)
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    dispatched: list = []

    class _ConciseShim:
        async def dispatch(self, task, calls):
            from athena.kernel.dispatch import DispatchResult

            dispatched.extend(calls)
            return DispatchResult(
                results=tuple(
                    CapabilityResultBlock(
                        call_id=c.call_id,
                        capability_id=c.capability_id,
                        ok=False,
                        error="TypeError: boom",
                    )
                    for c in calls
                )
            )

    await _run_one_turn(stack, task, _ConciseShim())
    assert len(dispatched) == 1  # primary dispatch only
    state = stack.kernel._runs[task.id]
    assert state.model_calls == 0
    await stack.db.close()


async def test_repeated_concise_failures_trigger_subturn():
    """P1-13: after enough consecutive failures of the same capability,
    even a concise failure becomes a REPEATED_FAILURE observation."""
    stack = await _make_stack(interpreter="wired-marker")
    task = await _persisted_task(stack)
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    dispatched: list = []

    class _ConciseShim:
        def __init__(self):
            self.calls = 0

        async def dispatch(self, task, calls):
            from athena.kernel.dispatch import DispatchResult

            self.calls += 1
            dispatched.extend(calls)
            return DispatchResult(
                results=tuple(
                    CapabilityResultBlock(
                        call_id=c.call_id,
                        capability_id=c.capability_id,
                        ok=False,
                        error="TypeError: boom",
                    )
                    for c in calls
                )
            )

    shim = _ConciseShim()
    for _ in range(3):  # threshold is 3 consecutive failures
        await _run_one_turn(stack, task, shim)
    state = stack.kernel._runs[task.id]
    # First two failures: no subturn. Third: REPEATED_FAILURE fires one.
    assert state.interpreter_failure_counts["runtime.evaluate"] == 3
    assert state.model_calls == 1
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


async def test_many_failed_results_still_one_subturn():
    """Cost-amplification bound: N failed calls in one dispatch → ONE subturn.

    Without the bound, a turn whose model emitted many failing tool calls
    would trigger one metered model subturn per failure — budget checks are
    only applied at the next loop iteration, so a task at its cost cap could
    overshoot inside a single dispatch.
    """
    stack = await _make_stack(interpreter="wired-marker")
    task = await _persisted_task(stack)
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    dispatched: list = []
    shim = _RecordingShim(dispatched)
    calls = [_Call(call_id=f"c-{i}") for i in range(5)]
    await _run_one_turn(stack, task, shim, calls=calls)
    state = stack.kernel._runs[task.id]
    assert state.model_calls == 1  # exactly one subturn for five failures
    # primary dispatch (5 calls) + one proposal dispatch
    assert len(dispatched) == 6
    await stack.db.close()


async def test_budget_exhausted_skips_fusion():
    """A task already at its budget cap must not fund another subturn."""
    from athena.protocol.tasks import ResourceBudget

    stack = await _make_stack(interpreter="wired-marker")
    task = TaskSpec(
        id=new_id("task"),
        objective="budget cap",
        session_id="s-fusion",
        resource_budget=ResourceBudget(max_input_tokens=100),
    )
    await stack.db.execute(
        "INSERT OR IGNORE INTO sessions(id, parent_id, created_at, updated_at, metadata) "
        "VALUES (?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}')",
        (task.session_id,),
    )
    await stack.tasks.insert_task(task.id, task.session_id, None, task.objective)
    ext = _extension_for(stack.kernel)
    stack.kernel._interpreter = ext
    state = stack.kernel._runs.get(task.id) or RunState(task=task)
    stack.kernel._runs[task.id] = state
    state.input_tokens = 100  # at the cap already
    dispatched: list = []
    shim = _RecordingShim(dispatched)
    await _run_one_turn(stack, task, shim)
    # _budget_exhausted sees input_tokens >= max → no observation offered
    assert len(dispatched) == 1  # primary dispatch only
    assert state.model_calls == 0
    await stack.db.close()
    await stack.db.close()


async def test_generated_failure_arms_one_canonical_repair_turn():
    stack = await _make_stack(interpreter=None)
    task = await _persisted_task(stack)
    dispatched: list = []

    class _GeneratedShim:
        def __init__(self):
            self.calls = 0

        async def dispatch(self, task, calls):
            from athena.kernel.dispatch import DispatchResult

            self.calls += 1
            dispatched.extend(calls)
            if self.calls == 1:
                return DispatchResult(
                    results=(
                        CapabilityResultBlock(
                            call_id="generated-call",
                            capability_id="synth_buggy",
                            ok=False,
                            error="implementation failed",
                            metadata={
                                "generated_failure": {
                                    "capability_id": "synth_buggy",
                                    "failure_class": "implementation_failure",
                                    "repairable": True,
                                    "recovery_action": "source_repair",
                                    "evidence": {"stderr": "unexpected value"},
                                }
                            },
                        ),
                    )
                )
            return DispatchResult(
                results=(
                    CapabilityResultBlock(
                        call_id=calls[0].call_id,
                        capability_id=calls[0].capability_id,
                        ok=True,
                        output="repaired",
                    ),
                )
            )

    shim = _GeneratedShim()
    await _run_one_turn(stack, task, shim)
    state = stack.kernel._runs[task.id]
    assert state.generated_recovery_pending is True
    assert state.generated_recovery_attempts == 0
    assert len(dispatched) == 1

    repair = _Call(
        call_id="repair-call",
        capability_id="synthesis",
        arguments={
            "operation": "repair",
            "capability_id": "synth_buggy",
            "code": "def run(args):\n    return {'ok': True}\n",
        },
    )
    await _run_one_turn(stack, task, shim, calls=[repair])
    assert state.generated_recovery_pending is False
    assert state.generated_recovery_attempts == 1
    assert dispatched[-1].capability_id == "synthesis"
    events = await stack.events.list_for_task(task.id)
    assert any(event.type == "GeneratedFailureObserved" for event in events)
    await stack.db.close()


async def test_successful_repair_retries_original_generated_call_once():
    stack = await _make_stack(interpreter=None)
    task = await _persisted_task(stack)
    state = stack.kernel._runs.get(task.id) or RunState(task=task)
    stack.kernel._runs[task.id] = state
    state.generated_recovery_original = {
        "call_id": "original-call",
        "capability_id": "synth_buggy",
        "arguments": {"value": 7},
    }
    state.generated_recovery_retried = False
    dispatched: list = []

    class _Shim:
        async def dispatch(self, task, calls):
            dispatched.extend(calls)
            return __import__("athena.kernel.dispatch", fromlist=["DispatchResult"]).DispatchResult(
                results=()
            )

    result = await stack.kernel._retry_repaired_generated_operation(
        task,
        state,
        (
            CapabilityResultBlock(
                call_id="repair",
                capability_id="synthesis",
                ok=True,
                output="{}",
                metadata={"capability_id": "synth_fixed"},
            ),
        ),
        _Shim(),
    )
    assert result is not None
    assert dispatched[0].capability_id == "synth_fixed"
    assert dispatched[0].arguments == {"value": 7}
    assert state.generated_recovery_retried is True
    second = await stack.kernel._retry_repaired_generated_operation(
        task,
        state,
        (
            CapabilityResultBlock(
                call_id="repair-2",
                capability_id="synthesis",
                ok=True,
                metadata={"capability_id": "synth_fixed_2"},
            ),
        ),
        _Shim(),
    )
    assert second is None
    await stack.db.close()


async def test_generated_dependency_failure_does_not_arm_source_repair():
    stack = await _make_stack(interpreter=None)
    task = await _persisted_task(stack)
    state = stack.kernel._runs.get(task.id) or RunState(task=task)
    stack.kernel._runs[task.id] = state
    result = CapabilityResultBlock(
        call_id="generated-call",
        capability_id="synth_dependency",
        ok=False,
        error="dependency missing",
        metadata={
            "generated_failure": {
                "capability_id": "synth_dependency",
                "failure_class": "environment_changed",
                "repairable": False,
                "recovery_action": "dependency_refresh_and_revalidation",
            }
        },
    )
    assert await stack.kernel._record_generated_failure(task, state, result) is False
    assert state.generated_recovery_pending is False
    await stack.db.close()


async def test_composed_generated_repair_revalidates_and_retries_original_call(tmp_path):
    import json as _json

    from athena.affordances import CapabilityFabric
    from athena.capabilities.registry import CapabilityRegistry
    from athena.capabilities.synthesis import SynthesisCapability
    from athena.kernel.dispatch import DispatchResult
    from athena.protocol.capabilities import (
        CapabilityRequest,
        CapabilityRequestOrigin,
        InvocationContext,
    )
    from athena.protocol.tasks import WorkspaceSpec
    from athena.synthesis.engine import SynthesisEngine

    stack = await _make_stack(interpreter=None)
    task = await _persisted_task(stack)
    fabric = CapabilityFabric(CapabilityRegistry())
    engine = SynthesisEngine()
    synthesis = SynthesisCapability(engine, fabric)
    created = await synthesis.invoke(
        CapabilityRequest(
            capability_id="synthesis",
            task_id=task.id,
            call_id="generated-create",
            origin=CapabilityRequestOrigin.MODEL,
            arguments={
                "operation": "create",
                "name": "branch_transform",
                "description": "Transforms values",
                "code": "def run(args):\n    return {'value': args['value'].upper()}\n",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "effects": ["READ_LOCAL"],
                "validation_cases": [
                    {"args": {"value": "ready"}, "expect_output": {"value": "READY"}},
                ],
            },
        )
    )
    assert created.status.value == "ok", (created.error, created.output)
    original_id = _json.loads(created.output)["capability_id"]
    original = engine.synthetic_for(original_id)
    assert original is not None
    workspace = WorkspaceSpec(id="repo", root=str(tmp_path))
    retry_outputs: list[str] = []

    class _ComposedShim:
        def __init__(self):
            self.calls = 0

        async def dispatch(self, task, calls):
            self.calls += 1
            call = calls[0]
            if self.calls == 1:
                return DispatchResult(
                    results=(
                        CapabilityResultBlock(
                            call_id=call.call_id,
                            capability_id=original_id,
                            ok=False,
                            error="unseen input raised ValueError",
                            metadata={
                                "generated_failure": {
                                    "capability_id": original_id,
                                    "code_hash": "original-hash",
                                    "failure_class": "implementation_failure",
                                    "repairable": True,
                                    "recovery_action": "source_repair",
                                    "evidence": {"stderr": "unseen input"},
                                }
                            },
                        ),
                    )
                )
            if call.capability_id == "synthesis":
                repaired = await synthesis.invoke(
                    CapabilityRequest(
                        capability_id="synthesis",
                        task_id=task.id,
                        call_id=call.call_id,
                        origin=CapabilityRequestOrigin.MODEL,
                        arguments={
                            "operation": "repair",
                            "capability_id": original_id,
                            "name": "branch_transform_v2",
                            "description": "Transforms values and records a suffix",
                            "code": (
                                "def run(args):\n"
                                "    return {'value': args['value'].upper() + ('' if args['value'] == 'ready' else '-fixed')}\n"
                            ),
                            "input_schema": dict(original.input_schema),
                            "effects": ["READ_LOCAL"],
                            "validation_cases": [
                                {
                                    "args": {"value": "unseen"},
                                    "expect_output": {"value": "UNSEEN-fixed"},
                                },
                            ],
                        },
                    )
                )
                assert repaired.status.value == "ok", (repaired.error, repaired.output)
                payload = _json.loads(repaired.output) if repaired.output else {}
                return DispatchResult(
                    results=(
                        CapabilityResultBlock(
                            call_id=call.call_id,
                            capability_id=call.capability_id,
                            ok=True,
                            output=repaired.output,
                            metadata={**dict(repaired.metadata), **payload},
                        ),
                    )
                )
            request = CapabilityRequest(
                capability_id=call.capability_id,
                arguments=dict(call.arguments or {}),
                task_id=task.id,
                call_id=call.call_id,
            )
            result = await fabric.executor_for(
                call.capability_id,
                task_id=task.id,
            ).invoke(request, context=InvocationContext(workspace=workspace, task_id=task.id))
            retry_outputs.append(result.output)
            return DispatchResult(
                results=(
                    CapabilityResultBlock(
                        call_id=result.call_id,
                        capability_id=result.capability_id,
                        ok=result.status.value == "ok",
                        output=result.output,
                        error=result.error,
                        metadata=result.metadata,
                    ),
                )
            )

    shim = _ComposedShim()
    initial = _Call(
        call_id="generated-original", capability_id=original_id, arguments={"value": "unseen"}
    )
    await _run_one_turn(stack, task, shim, calls=[initial])
    state = stack.kernel._runs[task.id]
    assert state.generated_recovery_pending is True
    assert (await stack.tasks.get(task.id))["metadata"]["generated_recovery"]["failing_input"] == {
        "value": "unseen"
    }
    repair = _Call(
        call_id="generated-repair",
        capability_id="synthesis",
        arguments={
            "operation": "repair",
            "capability_id": original_id,
            "code": "def run(args):\n    return {'value': args['value'].upper() + '-fixed'}\n",
        },
    )
    await _run_one_turn(stack, task, shim, calls=[repair])
    assert state.generated_recovery_attempts == 1
    assert state.generated_recovery_retried is True
    assert state.generated_recovery_retry_status == "consumed"
    assert _json.loads(retry_outputs[0])["value"] == "UNSEEN-fixed"
    await stack.db.close()
