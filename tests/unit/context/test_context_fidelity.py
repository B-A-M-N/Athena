from athena.evaluation.context_fidelity import (
    compact_context,
    large_context_fidelity_corpus,
    score_context_fidelity,
)


def test_large_context_fidelity_corpus_has_required_sizes_and_scoring():
    cases = large_context_fidelity_corpus()
    assert [case.target_tokens for case in cases] == [50_000, 100_000, 250_000]
    for case in cases:
        score = score_context_fidelity(case, case.source())
        assert score["passed"] is True
        assert score["coverage"] == 1.0


def test_context_fidelity_scoring_reports_anchor_loss():
    case = large_context_fidelity_corpus()[0]
    score = score_context_fidelity(case, case.anchors[0])
    assert score["passed"] is False
    assert score["anchors_preserved"] == 1
    assert score["missing_anchors"] == list(case.anchors[1:])


def test_compaction_preserves_distributed_anchors_and_reports_fidelity_metrics():
    case = large_context_fidelity_corpus()[0]
    result = compact_context(case, rounds=3)
    assert result["passed"] is True
    assert result["compaction_rounds"] == 3
    assert result["summarizer_calls"] == 3
    assert result["compiled_tokens"] < case.target_tokens
    assert result["causal_order_fidelity"] is True
    assert result["stable_prefix_digest"]
