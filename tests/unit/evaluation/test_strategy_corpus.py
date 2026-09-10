from athena.evaluation.strategy_corpus import strategy_escalation_corpus


def test_strategy_corpus_covers_escalation_branches():
    cases = strategy_escalation_corpus()
    assert len(cases) >= 8
    assert {case.expected_decision for case in cases} == {"respond", "discover", "act"}
    assert {case.required_escalation for case in cases} >= {
        "workspace_observation",
        "operator_approval",
        "network_policy",
        "recovery_receipt",
    }
