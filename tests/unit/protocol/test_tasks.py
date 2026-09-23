from decimal import Decimal
import pytest
from datetime import timedelta


from athena.protocol.tasks import (
    CapabilityPolicy,
    FINAL_STATUSES,
    LEGAL_TRANSITIONS,
    PAUSED_STATUSES,
    ResourceBudget,
    ResourceBudgetCeiling,
    TaskStatus,
    capability_policy_covers,
    effective_capability_policy,
    intersect_resource_budgets,
)


def test_effective_capability_policy_applies_deny_before_reflection_or_delegation():
    effective = effective_capability_policy(
        {"allow": ["files.write"], "ask": ["files.read"], "deny": ["files.write"]}
    )
    assert effective.allow == ()
    assert effective.ask == ("files.read",)
    assert not capability_policy_covers(effective, {"allow": ["files.write"]})


@pytest.mark.athena_claim("BHV-014")
@pytest.mark.athena_evidence("test", "invariant")
def test_legal_and_illegal_transitions():
    assert TaskStatus.QUEUED in LEGAL_TRANSITIONS[TaskStatus.CREATED]
    assert TaskStatus.COMPLETE in LEGAL_TRANSITIONS[TaskStatus.RUNNING]
    assert TaskStatus.RUNNING not in LEGAL_TRANSITIONS.get(TaskStatus.COMPLETE, set())
    assert TaskStatus.COMPLETE not in LEGAL_TRANSITIONS[TaskStatus.CREATED]
    assert TaskStatus.RECOVERY_REQUIRED not in LEGAL_TRANSITIONS.get(TaskStatus.FAILED, set())


@pytest.mark.athena_claim("BHV-083")
@pytest.mark.athena_evidence("test", "invariant")
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
    assert FINAL_STATUSES == frozenset(
        {
            TaskStatus.COMPLETE,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    )


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


def test_capability_policy_coverage_preserves_ask_vs_allow_lattice():
    assert capability_policy_covers(
        CapabilityPolicy(allow=("files.write",)),
        CapabilityPolicy(allow=("files.write",)),
    )
    assert capability_policy_covers(
        CapabilityPolicy(allow=("files.write",)),
        CapabilityPolicy(ask=("files.write",)),
    )
    assert capability_policy_covers(
        CapabilityPolicy(ask=("files.write",)),
        CapabilityPolicy(ask=("files.write",)),
    )
    assert not capability_policy_covers(
        CapabilityPolicy(ask=("files.write",)),
        CapabilityPolicy(allow=("files.write",)),
    )


def test_capability_policy_coverage_handles_unrestricted_and_deny_rules():
    assert capability_policy_covers(CapabilityPolicy(), CapabilityPolicy(allow=("files.write",)))
    assert not capability_policy_covers(
        CapabilityPolicy(allow=("files.write",)), CapabilityPolicy()
    )
    assert not capability_policy_covers(
        CapabilityPolicy(deny=("files.write",)), CapabilityPolicy(ask=("files.write",))
    )
    assert capability_policy_covers(
        CapabilityPolicy(allow=("files.write",)),
        CapabilityPolicy(allow=("files.write",), deny=("files.write",)),
    )


def test_cancelled_positive_rules_are_empty_authority_for_coverage():
    cancelled = CapabilityPolicy(ask=("files.read",), deny=("files.read",))
    denied_universe = CapabilityPolicy(deny=("files.read",))

    assert capability_policy_covers(cancelled, cancelled)
    assert not capability_policy_covers(cancelled, denied_universe)


def test_resource_budget_ceiling_keeps_unbounded_dimensions_explicit():
    ceiling = intersect_resource_budgets({}, {"max_children": 2})
    assert isinstance(ceiling, ResourceBudgetCeiling)
    assert ceiling.max_children == 2
    assert ceiling.max_agent_iterations is None
