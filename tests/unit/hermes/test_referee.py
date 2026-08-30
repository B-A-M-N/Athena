from __future__ import annotations

import pytest

from athena.hermes import HermesDecision, HermesReferee, ReviewPacket


def _candidate_packet(**overrides):
    values = {
        "kind": "candidate",
        "risk": {"level": "low", "paths": ["src/athena/cli/app.py"]},
        "verification_results": ({"id": "pytest", "passed": True},),
        "release_results": {"review_eligible": True},
    }
    values.update(overrides)
    return ReviewPacket(**values)


@pytest.mark.asyncio
async def test_referee_intersects_external_pass_with_deterministic_proof():
    seen = []

    async def evaluator(packet):
        seen.append(packet)
        return {"decision": "PASS", "rationale": "safe"}

    packet = _candidate_packet()
    verdict = await HermesReferee(evaluator).review(packet)

    assert verdict.decision is HermesDecision.PASS
    assert verdict.packet_hash == packet.digest()
    assert seen == [packet]
    assert not hasattr(HermesReferee, "apply")
    assert not hasattr(HermesReferee, "promote")


@pytest.mark.asyncio
async def test_deterministic_proof_failure_rejects_external_pass():
    called = False

    async def evaluator(_packet):
        nonlocal called
        called = True
        return {"decision": "PASS"}

    verdict = await HermesReferee(evaluator).review(
        _candidate_packet(verification_results=({"id": "pytest", "passed": False},))
    )

    assert verdict.decision is HermesDecision.REJECT
    assert called is False


@pytest.mark.asyncio
async def test_critical_pass_becomes_human_review():
    async def evaluator(_packet):
        return {"decision": "PASS", "rationale": "looks safe"}

    verdict = await HermesReferee(evaluator).review(
        _candidate_packet(risk={"level": "high", "paths": ["src/athena/reality/gate.py"]})
    )

    assert verdict.decision is HermesDecision.READY_FOR_HUMAN_REVIEW
    assert verdict.requires_human_review is True


@pytest.mark.asyncio
async def test_external_human_review_request_can_only_reduce_authority():
    async def evaluator(_packet):
        return {
            "decision": "PASS",
            "requires_human_review": True,
            "rationale": "operator must inspect this",
        }

    verdict = await HermesReferee(evaluator).review(_candidate_packet())

    assert verdict.decision is HermesDecision.READY_FOR_HUMAN_REVIEW
    assert verdict.requires_human_review is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "src/athena/kernel/kernel.py",
        "src/athena/release/gates.py",
        "tests/security/test_workspace_escape.py",
        ".github/workflows/ci.yml",
        "scripts/release-check",
        "SECURITY.md",
    ],
)
async def test_authority_surfaces_cannot_receive_autonomous_pass(path):
    async def evaluator(_packet):
        return {"decision": "PASS", "rationale": "looks safe"}

    verdict = await HermesReferee(evaluator).review(
        _candidate_packet(risk={"level": "low", "paths": [path]})
    )

    assert verdict.decision is HermesDecision.READY_FOR_HUMAN_REVIEW
    assert verdict.requires_human_review is True


@pytest.mark.asyncio
async def test_referee_preserves_challenges_and_holds_without_evaluator():
    async def evaluator(_packet):
        return {
            "decision": "CHALLENGE",
            "rationale": "prove restart behavior",
            "challenges": [{"type": "counterexample", "request": "cold restart"}],
        }

    challenged = await HermesReferee(evaluator).review(_candidate_packet())
    unconfigured = await HermesReferee().review(_candidate_packet())

    assert challenged.decision is HermesDecision.CHALLENGE
    assert challenged.challenges[0]["type"] == "counterexample"
    assert unconfigured.decision is HermesDecision.HOLD


@pytest.mark.asyncio
async def test_mission_pass_is_a_mission_support_verdict():
    async def evaluator(_packet):
        return {"decision": "PASS", "rationale": "history covers objective"}

    verdict = await HermesReferee(evaluator).review(
        ReviewPacket(
            kind="mission",
            release_results={"task_status": "complete", "review_eligible": True},
        )
    )

    assert verdict.decision is HermesDecision.MISSION_COMPLETE_SUPPORTED
