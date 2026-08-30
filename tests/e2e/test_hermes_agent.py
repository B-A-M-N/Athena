"""Optional live transport acceptance against an operator-owned Hermes Agent."""

from __future__ import annotations

import os

import pytest

from athena.hermes import HermesAgentEvaluator, HermesDecision, HermesReferee, ReviewPacket


def _packet() -> ReviewPacket:
    return ReviewPacket(
        kind="candidate",
        risk={"level": "low"},
        verification_results=({"id": "hermes-live-e2e", "passed": True},),
        release_results={"review_eligible": True},
    )


@pytest.mark.asyncio
@pytest.mark.athena_evidence("e2e")
async def test_live_hermes_referee_transport() -> None:
    """Exercise the actual Hermes HTTP endpoint when the operator opts in."""
    endpoint = os.environ.get("ATHENA_HERMES_E2E_ENDPOINT")
    if not endpoint:
        pytest.skip("set ATHENA_HERMES_E2E_ENDPOINT to run the live Hermes check")

    adapter = HermesAgentEvaluator(
        endpoint=endpoint,
        profile=os.environ.get("ATHENA_HERMES_E2E_PROFILE", "athena-referee"),
        timeout_seconds=float(os.environ.get("ATHENA_HERMES_E2E_TIMEOUT", "90")),
        api_key=os.environ.get("ATHENA_HERMES_E2E_API_KEY", ""),
    )
    try:
        await adapter.health()
        raw = await adapter(_packet())
        assert str(raw.get("decision")) in {decision.value for decision in HermesDecision}
        verdict = await HermesReferee(adapter).review(_packet())
        assert verdict.decision in {
            HermesDecision.PASS,
            HermesDecision.HOLD,
            HermesDecision.REJECT,
            HermesDecision.CHALLENGE,
            HermesDecision.READY_FOR_HUMAN_REVIEW,
        }
    finally:
        await adapter.aclose()
