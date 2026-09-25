from athena.evaluation.benchmark import run_fixed_corpus_benchmark
from athena.evaluation.corpus import default_benchmark_cases
from athena.evaluation.neutral import EvaluationOutcome


async def test_fixed_corpus_benchmark_preserves_usage_and_outcome_evidence():
    cases = default_benchmark_cases()

    async def adapter(case):
        return EvaluationOutcome(
            case_id=case.id,
            status="complete",
            evidence=case.required_evidence,
            model_calls=2,
            tool_calls=1,
            prompt_tokens=100,
            output_tokens=20,
        )

    report = await run_fixed_corpus_benchmark(
        cases,
        {"local-fixture": adapter},
        repetitions=2,
    )
    assert report["schema"] == "athena.evaluation.benchmark.v1"
    assert report["case_count"] == 4
    assert report["repetitions"] == 2
    assert report["observation_count"] == 8
    assert report["completion_rate"] == 1.0
    first = report["observations"][0]["systems"]["local-fixture"]
    assert first["evidence"] == ["workspace-observed"]
    assert first["model_calls"] == 2
    assert first["tool_calls"] == 1
    assert report["environment"]["cpu_count"] >= 1
    assert "not a matched Hermes" in report["limitations"]
