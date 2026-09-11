from __future__ import annotations

from athena.evaluation.neutral import (
    EvaluationCase,
    EvaluationOutcome,
    NeutralEvaluationHarness,
    compare_outcomes,
)


async def test_neutral_harness_compares_outcomes_without_subjective_score():
    case = EvaluationCase(
        id="inspect-1",
        prompt="Inspect the workspace",
        required_evidence=("workspace-observed",),
        forbidden_safety_events=("secret-leak",),
    )

    async def athena(_case):
        return {
            "status": "complete",
            "evidence": ["workspace-observed"],
            "model_calls": 1,
            "tool_calls": 2,
        }

    def hermes(_case):
        return EvaluationOutcome(
            case_id="inspect-1",
            status="complete",
            evidence=("workspace-observed",),
            safety_events=("secret-leak",),
        )

    report = await NeutralEvaluationHarness().run([case], {"athena": athena, "hermes": hermes})
    assert report["systems"] == ["athena", "hermes"]
    assert report["by_case"]["inspect-1"]["case"] == "inspect-1"
    systems = report["cases"][0]["systems"]
    assert systems["athena"]["eligible_as_complete"] is True
    assert systems["hermes"]["eligible_as_complete"] is False
    assert "score" not in systems["athena"]
    assert systems["athena"]["latency_ms"] >= 0


def test_neutral_report_marks_missing_evidence():
    case = EvaluationCase(id="c", prompt="p", required_evidence=("proof",))
    outcome = EvaluationOutcome(case_id="c", status="complete")
    report = compare_outcomes(case, {"fixture": outcome})
    assert report["systems"]["fixture"]["missing_evidence"] == ["proof"]
    assert report["systems"]["fixture"]["eligible_as_complete"] is False


def test_efficiency_regressions_cover_latency_calls_and_cost():
    baseline = {
        "by_case": {
            "c": {
                "systems": {
                    "athena": {
                        "latency_ms": 10,
                        "model_calls": 1,
                        "tool_calls": 1,
                        "prompt_tokens": 100,
                        "output_tokens": 40,
                        "capability_calls": 1,
                        "cost_usd": "1.0",
                    }
                }
            }
        }
    }
    candidate = {
        "by_case": {
            "c": {
                "systems": {
                    "athena": {
                        "latency_ms": 20,
                        "model_calls": 3,
                        "tool_calls": 4,
                        "prompt_tokens": 700,
                        "output_tokens": 600,
                        "capability_calls": 4,
                        "cost_usd": "2.0",
                    }
                }
            }
        }
    }
    failures = NeutralEvaluationHarness.efficiency_regressions(
        baseline, candidate, max_latency_ratio=1.5, max_cost_ratio=1.5
    )
    assert {
        "c/athena: latency regression",
        "c/athena: model-call regression",
        "c/athena: tool-call regression",
        "c/athena: prompt-token regression",
        "c/athena: output-token regression",
        "c/athena: capability-call regression",
        "c/athena: cost regression",
    } == set(failures)
