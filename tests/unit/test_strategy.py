from athena.protocol.capabilities import Availability, CapabilityDescriptor
from athena.strategy import StrategyAffordance, select_strategy


def test_strategy_marks_action_discovery_miss_as_discovery_work():
    guidance = select_strategy("research the release", ())

    assert guidance.route == "discover"
    assert guidance.decision == "discover"
    assert guidance.completion_mode == "observable_work_required"
    assert guidance.missing_affordance is None


def test_strategy_keeps_conversation_response_only_without_action_signal():
    guidance = select_strategy("tell me a short joke", ())

    assert guidance.route == "respond"
    assert guidance.decision == "respond"
    assert guidance.completion_mode == "response_only"


def test_conversation_stays_response_only_even_if_inventory_is_present():
    guidance = select_strategy("tell me a short joke", ("fs", "execute"))

    assert guidance.decision == "respond"
    assert guidance.completion_mode == "response_only"


def test_research_strategy_exposes_only_available_candidates():
    guidance = select_strategy("compare the evidence", ("research", "artifacts"))

    assert guidance.route == "evidence_acquisition"
    assert guidance.candidates == ("research", "artifacts")
    assert guidance.completion_mode == "observable_work_required"


def test_visible_primitives_are_used_when_specialized_route_is_absent():
    guidance = select_strategy("run a shadow experiment", ("fs", "execute"))

    assert guidance.route == "direct"
    assert guidance.decision == "act"
    assert set(guidance.candidates) == {"fs", "execute"}


def test_strategy_consumes_descriptor_evidence_and_remains_advisory():
    execute = CapabilityDescriptor(
        id="execute",
        description="run a bounded command",
        input_schema={},
        availability=Availability.AVAILABLE,
    )
    guidance = select_strategy("run the existing command", (execute,))

    assert guidance.route == "direct"
    assert guidance.route_kind == "existing_primitive"
    assert guidance.candidates == ("execute",)
    assert guidance.affordances[0].id == "execute"
    assert guidance.affordances[0].available is True
    assert guidance.to_dict()["affordances"][0]["id"] == "execute"


def test_strategy_reports_dependency_and_environment_gaps_separately():
    guidance = select_strategy(
        "research the release",
        (
            {
                "id": "research",
                "availability": "available",
                "optimizer": {
                    "dependency_available": False,
                    "environment_compatible": True,
                },
            },
        ),
    )

    assert guidance.route == "affordance_gap"
    assert guidance.missing_affordance == "research"
    assert guidance.gap_kind == "dependency_unready"


def test_strategy_accepts_typed_affordance_records():
    guidance = select_strategy(
        "build a tool",
        (
            StrategyAffordance(
                id="synthesis",
                description="construct a capability",
                scope="task",
                proof={"all_passed": True},
            ),
        ),
    )

    assert guidance.route == "synthesize"
    assert guidance.route_kind == "generated_capability"
    assert guidance.affordances[0].proof == {"all_passed": True}


def test_coding_mutation_defaults_to_fusion_when_speculative_surface_is_visible():
    guidance = select_strategy(
        "fix the parser bug in this repository",
        ("fs", "execute", "fusion"),
    )

    assert guidance.route == "fusion"
    assert guidance.route_kind == "fusion_shadow"
    assert guidance.candidates[0] == "fusion"
    assert guidance.decision == "act"
    assert guidance.completion_mode == "observable_work_required"


def test_simple_readme_edit_stays_direct_when_advanced_capabilities_are_visible():
    guidance = select_strategy(
        "fix a typo in README.md",
        ("fs", "execute", "fusion", "synthesis"),
    )

    assert guidance.route == "direct"
    assert guidance.work_requirement == "persistent_mutation"
    assert "fusion" not in guidance.candidates[:1]


def test_unrelated_research_inventory_does_not_change_a_direct_edit():
    guidance = select_strategy(
        "fix a typo in README.md",
        ("fs", "execute", "research", "fusion", "synthesis", "workflow"),
    )

    assert guidance.route == "direct"


def test_coding_mutation_does_not_invent_a_missing_fusion_capability():
    guidance = select_strategy(
        "fix the parser bug in this repository",
        ("fs", "execute"),
    )

    assert guidance.route == "direct"
    assert guidance.decision == "act"
    assert "fusion" not in guidance.candidates


def test_observation_work_does_not_get_the_mutation_fusion_override():
    guidance = select_strategy(
        "review the parser bug in this repository",
        ("fs", "execute", "fusion"),
    )

    # Fusion may win from evidence, but it must not be the deterministic
    # mutation-specific override when the turn intent is not a mutation.
    assert guidance.turn_intent != "mutation"
    assert guidance.completion_mode == "observable_work_required"


def test_structured_tags_and_effects_do_not_override_task_requirements():
    guidance = select_strategy(
        "perform the bounded operation",
        (
            StrategyAffordance(
                id="fusion.shadow",
                description="bounded operation",
                tags=("fusion",),
                effects=("execute", "write_local"),
                proof={"all_passed": True},
            ),
        ),
    )

    assert guidance.route == "direct"
    assert guidance.work_requirement == "direct_execution"


# ---------------------------------------------------------------------- #
# P0-6: two-channel separation — tool availability vs evidence obligation.
# A workspace/current-state question needs BOTH a working tool surface AND
# a completion mode that refuses a prose-only hallucinated answer.
# General knowledge and hypothetical explanation need neither.
# ---------------------------------------------------------------------- #


def test_workspace_state_questions_require_observed_evidence():
    for prompt in (
        "What changed in this repo?",
        "Why is this test failing?",
        "What does README.md say?",
        "Look at the logs and tell me what broke.",
        "what does this function do?",
    ):
        guidance = select_strategy(prompt, ("fs", "git", "execute"))
        assert guidance.completion_mode == "observable_work_required", prompt


def test_general_knowledge_stays_response_only():
    for prompt in (
        "Who wrote Hamlet?",
        "What is the capital of France?",
        "Explain how quicksort works.",
    ):
        guidance = select_strategy(prompt, ("fs", "git", "execute"))
        assert guidance.completion_mode == "response_only", prompt


def test_hypothetical_explanation_stays_response_only():
    for prompt in (
        "What happens if I run pytest with no args?",
        "What would happen if we deleted the cache layer?",
    ):
        guidance = select_strategy(prompt, ("fs", "git", "execute"))
        assert guidance.completion_mode == "response_only", prompt


def test_continuations_inherit_evidence_obligation():
    for prompt in (
        "go ahead",
        "continue",
        "yes, do it",
        "what did we use last time?",
    ):
        guidance = select_strategy(prompt, ("fs", "git", "execute"))
        assert guidance.completion_mode == "observable_work_required", prompt


def test_strategy_selection_record_is_tied_to_baseline_and_acceptance():
    from athena.protocol.tasks import Criterion, TaskSpec, WorkspaceSpec
    from athena.strategy import build_strategy_selection_record

    guidance = select_strategy("run a shadow experiment", ("fusion", "execute"))
    task = TaskSpec(
        id="task-strategy-record",
        objective="run a shadow experiment",
        workspace=WorkspaceSpec(id="repo", root="/tmp/repo", revision="rev-1"),
        acceptance_criteria=(Criterion(id="proof", description="candidate passes"),),
    )
    record = build_strategy_selection_record(guidance, task=task)
    payload = record.to_record()
    assert payload["selected_route"] == guidance.route
    assert payload["selected_by"] == "deterministic_advisory_strategy"
    assert payload["viable_alternatives"]
    assert payload["workspace_baseline"]["workspace_id"] == "repo"
    assert payload["workspace_baseline"]["workspace_revision"] == "rev-1"
    assert payload["acceptance_criteria"] == ["proof:True"]
    assert "work_requirement:" in " ".join(payload["evidence"])
