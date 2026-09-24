"""Controlled held-out capability reliability scenarios.

These scenarios exercise the real kernel/provider/dispatch seams with a
deterministic provider. They are intentionally separate from Fusion unit
integrity tests: the question here is whether bounded recovery changes task
outcomes, and what it costs when it is not useful.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from athena.context.compiler import ContextCompiler
from athena.kernel.dispatch import CapabilityResultBlock, DispatchResult
from athena.kernel.kernel import AgentKernel
from athena.kernel.termination import TerminationEvaluator
from athena.models.providers.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.protocol.ids import new_id
from athena.protocol.tasks import TaskSpec, TaskStatus
from athena.state.database import Database
from athena.state.events import EventStore
from athena.state.messages import MessageStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.manager import TaskManager


@dataclass(frozen=True)
class ReliabilityMetrics:
    scenario: str
    completed: bool
    verification_results: tuple[bool, ...]
    wasted_attempts: int
    model_calls: int
    dispatch_calls: int
    latency_ms: int


class _ControlledFusionDispatch:
    def __init__(self, *, scenario: str):
        self.scenario = scenario
        self.calls = 0
        self.verification_results: list[bool] = []

    async def dispatch(self, task, calls):
        self.calls += 1
        call = calls[0]
        if self.calls == 1:
            self.verification_results.append(False)
            return DispatchResult(
                results=(
                    CapabilityResultBlock(
                        call_id=call.call_id,
                        capability_id="fusion",
                        ok=True,
                        output=(
                            '{"status":"FAILED","kind":"speculative_verification_failure",'
                            f'"scenario":"{self.scenario}"}}'
                        ),
                        metadata={
                            "operation": "run",
                            "failure_record": {
                                "kind": "speculative_failure",
                                "failed_operation": [
                                    {
                                        "capability_id": "fs",
                                        "arguments": {"path": "wrong.py"},
                                    }
                                ],
                                "verification_results": [
                                    {"name": "held-out verification", "passed": False}
                                ],
                                "violated_invariants": ["held-out verification"],
                                "workspace_changes": ["wrong.py"],
                                "remaining_execution_budget": {"iterations": 48},
                            },
                        },
                    ),
                )
            )

        self.verification_results.append(True)
        return DispatchResult(
            results=(
                CapabilityResultBlock(
                    call_id=call.call_id,
                    capability_id="fusion",
                    ok=True,
                    output=(
                        '{"status":"COMPLETED","comparison_id":"held-out-comparison",'
                        '"verified_count":1,"selection":"kernel_decision_required"}'
                    ),
                    metadata={"operation": "compare", "verified_count": 1},
                ),
            )
        )


async def _run_case(
    *,
    scenario: str,
    recovery_attempts: int,
    no_benefit: bool = False,
) -> ReliabilityMetrics:
    db = Database(":memory:")
    await db._ensure_ready()
    sessions = SessionRepository(db)
    tasks = TaskStore(db)
    events = EventStore(db)
    messages = MessageStore(db)
    manager = TaskManager(task_store=tasks, events=events, sessions=sessions)
    provider = FakeModelProvider(
        scripts=(
            [{"respond": {"text": "no speculation needed", "done": True}}]
            if no_benefit
            else [
                {
                    "match": {"last_capability_result_contains": "kernel_decision_required"},
                    "respond": {"text": "verified candidate selected", "done": True},
                },
                {
                    "match": {
                        "last_capability_result_contains": "speculative_verification_failure"
                    },
                    "respond": {
                        "capability_call": {
                            "capability_id": "fusion",
                            "arguments": {
                                "operation": "compare",
                                "proposals": [
                                    [
                                        {
                                            "capability_id": "fs",
                                            "arguments": {"path": "wrong.py"},
                                        }
                                    ],
                                    [
                                        {
                                            "capability_id": "fs",
                                            "arguments": {"path": "fixed.py"},
                                        }
                                    ],
                                ],
                                "changes_from_previous": (
                                    "Use the corrected path and compare it with the failed candidate."
                                ),
                            },
                        },
                        "done": False,
                    },
                },
                {
                    "match": {"user_contains": "held-out"},
                    "respond": {
                        "capability_call": {
                            "capability_id": "fusion",
                            "arguments": {
                                "operation": "run",
                                "proposal": [
                                    {
                                        "capability_id": "fs",
                                        "arguments": {"path": "wrong.py"},
                                    }
                                ],
                            },
                        },
                        "done": False,
                    },
                },
            ]
        ),
        model="fake-1",
        provider="fake",
        tool_calling=True,
    )
    registry = ProviderRegistry()
    registry.register("fake", provider)
    kernel = AgentKernel(
        task_store=tasks,
        events=events,
        task_manager=manager,
        messages=messages,
        registry=registry,
        router=ModelRouter(registry),
        context_compiler=ContextCompiler(message_store=messages),
        termination=TerminationEvaluator(),
    )
    dispatch = _ControlledFusionDispatch(scenario=scenario)
    kernel._dispatch_factory = lambda task: dispatch
    session_id = new_id("session")
    await sessions.create(session_id)
    task = TaskSpec(
        id=new_id("task"),
        objective=(
            "no-benefit control task"
            if no_benefit
            else f"held-out speculative coding scenario: {scenario}"
        ),
        session_id=session_id,
        metadata={"speculation_recovery_attempts": recovery_attempts},
    )
    await manager.create(task)
    await manager.enqueue(task.id)
    result = await kernel.run_task(task.id)
    metrics = ReliabilityMetrics(
        scenario=scenario,
        completed=result.status is TaskStatus.COMPLETE,
        verification_results=tuple(dispatch.verification_results),
        wasted_attempts=max(dispatch.calls - len(dispatch.verification_results), 0),
        model_calls=result.usage.model_calls,
        dispatch_calls=dispatch.calls,
        latency_ms=result.usage.duration_ms,
    )
    await db.close()
    return metrics


@pytest.mark.asyncio
async def test_held_out_speculation_is_bounded_and_measurably_useful():
    scenarios = (
        "ambiguous requirements",
        "incomplete tests",
        "stale dependencies",
        "conflicting candidate changes",
    )
    enabled = [await _run_case(scenario=scenario, recovery_attempts=1) for scenario in scenarios]
    disabled = await _run_case(scenario=scenarios[0], recovery_attempts=0)
    no_benefit = await _run_case(
        scenario="no speculation benefit", recovery_attempts=1, no_benefit=True
    )

    assert all(item.completed for item in enabled)
    assert all(item.verification_results == (False, True) for item in enabled)
    assert all(item.dispatch_calls == 2 for item in enabled)
    assert all(item.wasted_attempts == 0 for item in enabled)

    assert disabled.completed is False
    assert disabled.verification_results == (False,)
    assert disabled.dispatch_calls == 1
    assert no_benefit.completed is True
    assert no_benefit.dispatch_calls == 0
    assert no_benefit.model_calls == 1
    assert no_benefit.wasted_attempts == 0

    # The metrics are part of the acceptance evidence, not just assertions:
    # every run has a bounded model/dispatch cost and an observed latency.
    assert all(item.model_calls <= 3 and item.latency_ms >= 0 for item in enabled)
