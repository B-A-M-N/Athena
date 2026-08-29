from athena.protocol.capabilities import Availability, CapabilityDescriptor
from athena.strategy import StrategyAffordance, select_strategy


def test_strategy_does_not_report_a_gap_for_an_empty_inventory():
    guidance = select_strategy("research the release", ())

    assert guidance.route == "direct"
    assert guidance.missing_affordance is None


def test_research_strategy_exposes_only_available_candidates():
    guidance = select_strategy("compare the evidence", ("research", "artifacts"))

    assert guidance.route == "evidence_acquisition"
    assert guidance.candidates == ("research", "artifacts")


def test_missing_fusion_is_an_observable_affordance_gap():
    guidance = select_strategy("run a shadow experiment", ("fs", "execute"))

    assert guidance.route == "affordance_gap"
    assert guidance.missing_affordance == "fusion"


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


def test_structured_tags_and_effects_can_select_a_route_without_keywords():
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

    assert guidance.route == "fusion"
    assert guidance.route_kind == "fusion_shadow"
