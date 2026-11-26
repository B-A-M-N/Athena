from athena.evaluation.competitive import (
    classify_comparison,
    competitive_cases,
    outcome_from_competitive_record,
)


def test_competitive_cases_have_independent_oracles_and_read_only_boundaries():
    cases = competitive_cases()
    assert len(cases) == 4
    assert len({item.case.id for item in cases}) == len(cases)
    assert all(item.oracle in item.case.required_evidence for item in cases)
    assert sum(item.case.metadata["read_only"] for item in cases) == 3
    assert sum(not item.case.metadata["read_only"] for item in cases) == 1


def test_comparison_classification_preserves_partial_outcomes():
    both = classify_comparison(
        {
            "athena": {"eligible_as_complete": True},
            "hermes": {"eligible_as_complete": True},
        }
    )
    partial = classify_comparison(
        {
            "athena": {"eligible_as_complete": True},
            "hermes": {"eligible_as_complete": False},
        }
    )
    neither = classify_comparison(
        {
            "athena": {"eligible_as_complete": False},
            "hermes": {"eligible_as_complete": False},
        }
    )
    assert both == "both_complete"
    assert partial == "one_complete_one_incomplete"
    assert neither == "neither_complete"


def test_unavailable_provider_record_cannot_satisfy_oracle():
    case = competitive_cases()[0]
    outcome = outcome_from_competitive_record(
        case,
        {
            "status": "blocked",
            "metadata": {"reason": "provider unavailable"},
        },
    )
    assert outcome.status == "blocked"
    assert outcome.evidence == ()
    assert outcome.metadata["reason"] == "provider unavailable"
