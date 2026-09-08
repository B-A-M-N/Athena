"""Optional live transport acceptance against an operator-owned Hermes Agent."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athena.hermes import HermesAgentEvaluator, HermesDecision, HermesReferee, ReviewPacket


def _packet() -> ReviewPacket:
    return ReviewPacket(
        kind="candidate",
        risk={"level": "low"},
        verification_results=({"id": "hermes-live-e2e", "passed": True},),
        release_results={"review_eligible": True},
    )


def _write_certification_evidence(
    path: str | Path,
    *,
    source_sha: str,
    release_run_id: str = "local",
    profile: str,
    preflight: Any,
    packet: ReviewPacket,
    verdict: Any,
) -> None:
    """Persist only public, structured facts from a successful live check."""
    referee = preflight.capabilities.get("referee")
    build = preflight.capabilities.get("build")
    referee_record = referee if isinstance(referee, Mapping) else {}
    build_record = build if isinstance(build, Mapping) else {}
    evidence = {
        "source_sha": source_sha,
        "release_run_id": release_run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "certification_scope": "live_transport_and_read_only_contract",
        "policy_version": referee_record.get("policy_version"),
        "referee_contract": build_record.get("referee_contract"),
        "safety_verified": preflight.safety_verified,
        "build": {"referee_contract": build_record.get("referee_contract")},
        "referee": {
            "enabled": referee_record.get("enabled"),
            "policy_version": referee_record.get("policy_version"),
            "effective_tools": referee_record.get("effective_tools"),
        },
        "packet_hash": packet.digest(),
        "resulting_verdict_class": verdict.decision.value,
        "certification_status": "certified",
    }
    evidence_path = Path(path)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")


def test_certification_evidence_is_structured_and_credential_free(tmp_path):
    packet = _packet()
    verdict = SimpleNamespace(decision=HermesDecision.PASS)
    preflight = SimpleNamespace(
        safety_verified=True,
        capabilities={
            "referee": {"enabled": True, "policy_version": 1, "effective_tools": []},
            "build": {"referee_contract": 1},
        },
    )
    path = tmp_path / "evidence.json"

    _write_certification_evidence(
        path,
        source_sha="abc123",
        profile="athena-referee",
        preflight=preflight,
        packet=packet,
        verdict=verdict,
    )

    evidence = json.loads(path.read_text(encoding="utf-8"))
    assert evidence["source_sha"] == "abc123"
    assert evidence["certification_scope"] == "live_transport_and_read_only_contract"
    assert evidence["safety_verified"] is True
    assert evidence["referee_contract"] == 1
    assert evidence["packet_hash"] == packet.digest()
    assert evidence["certification_status"] == "certified"
    assert "endpoint" not in evidence
    assert "api_key" not in json.dumps(evidence)


@pytest.mark.asyncio
@pytest.mark.athena_evidence("e2e")
async def test_live_hermes_referee_transport() -> None:
    """Certify live transport and the read-only Hermes contract when opted in."""
    endpoint = os.environ.get("ATHENA_HERMES_E2E_ENDPOINT")
    if not endpoint:
        if os.environ.get("ATHENA_HERMES_INTEGRATION_GATE") == "1":
            pytest.fail(
                "Hermes integration gate requires ATHENA_HERMES_E2E_ENDPOINT for live evidence"
            )
        pytest.skip("set ATHENA_HERMES_E2E_ENDPOINT to run the live Hermes check")

    profile = os.environ.get("ATHENA_HERMES_E2E_PROFILE", "athena-referee")
    packet = _packet()
    adapter = HermesAgentEvaluator(
        endpoint=endpoint,
        profile=profile,
        timeout_seconds=float(os.environ.get("ATHENA_HERMES_E2E_TIMEOUT", "90")),
        api_key=os.environ.get("ATHENA_HERMES_E2E_API_KEY", ""),
    )
    try:
        preflight = await adapter.preflight()
        assert preflight.safety_verified is True
        raw = await adapter(packet)
        assert str(raw.get("decision")) in {decision.value for decision in HermesDecision}
        verdict = await HermesReferee(adapter).review(packet)
        assert verdict.decision in {
            HermesDecision.PASS,
            HermesDecision.HOLD,
            HermesDecision.REJECT,
            HermesDecision.CHALLENGE,
            HermesDecision.READY_FOR_HUMAN_REVIEW,
        }
        assert verdict.packet_hash == packet.digest()
    finally:
        await adapter.aclose()

    evidence_path = os.environ.get("ATHENA_HERMES_E2E_EVIDENCE_PATH")
    if evidence_path:
        _write_certification_evidence(
            evidence_path,
            source_sha=os.environ.get("ATHENA_RELEASE_SHA")
            or os.environ.get("GITHUB_SHA")
            or "unknown",
            release_run_id=os.environ.get("ATHENA_RELEASE_RUN_ID")
            or os.environ.get("GITHUB_RUN_ID")
            or "local",
            profile=profile,
            preflight=preflight,
            packet=packet,
            verdict=verdict,
        )
