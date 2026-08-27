from athena.strategy import select_strategy


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
