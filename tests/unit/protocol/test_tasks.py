from decimal import Decimal
from datetime import timedelta


from athena.protocol.tasks import (
    FINAL_STATUSES,
    LEGAL_TRANSITIONS,
    PAUSED_STATUSES,
    ResourceBudget,
    TaskStatus,
)


def test_legal_and_illegal_transitions():
    assert TaskStatus.QUEUED in LEGAL_TRANSITIONS[TaskStatus.CREATED]
    assert TaskStatus.COMPLETE in LEGAL_TRANSITIONS[TaskStatus.RUNNING]
    assert TaskStatus.RUNNING not in LEGAL_TRANSITIONS.get(TaskStatus.COMPLETE, set())
    assert TaskStatus.COMPLETE not in LEGAL_TRANSITIONS[TaskStatus.CREATED]
    assert TaskStatus.RECOVERY_REQUIRED not in LEGAL_TRANSITIONS.get(TaskStatus.FAILED, set())


def test_resource_budget_merged_with_takes_min():
    budget = ResourceBudget(
        max_agent_iterations=10,
        max_input_tokens=1000,
        max_cost_usd=Decimal("50"),
        max_wall_time=timedelta(minutes=10),
    )
    tighter = ResourceBudget(
        max_agent_iterations=5,
        max_input_tokens=None,
        max_cost_usd=Decimal("20"),
        max_wall_time=None,
    )
    merged = budget.merged_with(tighter)
    assert merged.max_agent_iterations == 5
    assert merged.max_input_tokens == 1000
    assert merged.max_cost_usd == Decimal("20")
    assert merged.max_wall_time == timedelta(minutes=10)


def test_resource_budget_merged_with_non_min_taken():
    budget = ResourceBudget(max_agent_iterations=3)
    merged = budget.merged_with(ResourceBudget(max_agent_iterations=100))
    assert merged.max_agent_iterations == 3


def test_task_spec_constructs():
    from athena.protocol.tasks import TaskSpec

    spec = TaskSpec(id="task_1", objective="do something")
    assert spec.id == "task_1"
    assert spec.objective == "do something"
    assert spec.resource_budget == ResourceBudget()


def test_paused_and_final_are_disjoint():
    assert FINAL_STATUSES.isdisjoint(PAUSED_STATUSES)


def test_interrupted_is_paused_not_final():
    assert TaskStatus.INTERRUPTED in PAUSED_STATUSES
    assert TaskStatus.INTERRUPTED not in FINAL_STATUSES


def test_final_exactly_the_terminal_four():
    assert FINAL_STATUSES == frozenset({
        TaskStatus.COMPLETE, TaskStatus.PARTIAL,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    })


def test_paused_includes_resumable_states():
    assert TaskStatus.WAITING_APPROVAL in PAUSED_STATUSES
    assert TaskStatus.WAITING_INPUT in PAUSED_STATUSES
    assert TaskStatus.RECOVERY_REQUIRED in PAUSED_STATUSES
    assert TaskStatus.BLOCKED in PAUSED_STATUSES


def test_every_paused_status_can_resume_to_running():
    for status in PAUSED_STATUSES:
        assert TaskStatus.RUNNING in status.legal_transitions(), (
            f"{status.value} must be resumable to RUNNING"
        )


def test_no_final_status_transitions_anywhere():
    for status in FINAL_STATUSES:
        assert status.legal_transitions() == frozenset(), (
            f"{status.value} is terminal and must not transition"
        )
        assert status not in LEGAL_TRANSITIONS


def test_terminal_statuses_aliases_final():
    from athena.protocol.tasks import TERMINAL_STATUSES

    assert TERMINAL_STATUSES is FINAL_STATUSES