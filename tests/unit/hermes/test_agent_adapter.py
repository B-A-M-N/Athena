from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from athena.hermes import HermesAgentEvaluator, HermesReferee, ReviewPacket
from athena.hermes.agent_adapter import HermesRefereeSafetyError
from athena.service.config import AthenaConfig, HermesRefereeConfig
from athena.service.service import AthenaService


def _packet() -> ReviewPacket:
    return ReviewPacket(
        kind="candidate",
        risk={"level": "low"},
        verification_results=({"id": "pytest", "passed": True},),
        release_results={"review_eligible": True},
    )


@pytest.mark.asyncio
async def test_agent_adapter_posts_bounded_packet_to_referee_profile():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"decision":"PASS"}'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAgentEvaluator(
        endpoint="http://127.0.0.1:8642",
        profile="athena-referee",
        client=client,
    )
    try:
        result = await adapter(_packet())
    finally:
        await client.aclose()

    assert result == {"decision": "PASS"}
    assert requests[0].url == "http://127.0.0.1:8642/p/athena-referee/v1/chat/completions"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "hermes-agent"
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 1024
    assert payload["response_format"] == {"type": "json_object"}
    assert json.loads(payload["messages"][1]["content"])["packet_hash"] == _packet().digest()


@pytest.mark.asyncio
async def test_agent_adapter_requires_strict_json_and_referee_holds_on_garbage():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '```json\n{"decision":"PASS"}\n```'}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAgentEvaluator(endpoint="http://hermes.test/v1", client=client)
    try:
        verdict = await HermesReferee(adapter).review(_packet())
    finally:
        await client.aclose()

    assert verdict.decision.value == "HOLD"
    assert "failed" in verdict.rationale.lower()


@pytest.mark.asyncio
async def test_agent_adapter_health_uses_v1_models_probe():
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAgentEvaluator(endpoint="http://hermes.test/v1", client=client)
    try:
        await adapter.health()
    finally:
        await client.aclose()

    assert paths == ["/p/athena-referee/v1/models"]


@pytest.mark.asyncio
async def test_agent_adapter_preflight_requires_referee_contract_and_caches_success():
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "referee-model"}]})
        return httpx.Response(
            200,
            json={
                "runtime": {"mode": "referee", "tool_execution": "disabled"},
                "referee": {
                    "enabled": True,
                    "policy_version": 1,
                    "effective_tools": [],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HermesAgentEvaluator(endpoint="http://127.0.0.1:8642", client=client)
    try:
        first = await adapter.preflight()
        second = await adapter.preflight()
    finally:
        await client.aclose()

    assert first == second
    assert paths == [
        "/p/athena-referee/v1/models",
        "/p/athena-referee/v1/capabilities",
    ]


@pytest.mark.asyncio
async def test_agent_adapter_rejects_remote_without_explicit_opt_in():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    adapter = HermesAgentEvaluator(endpoint="http://hermes.test/v1", client=client)
    try:
        with pytest.raises(HermesRefereeSafetyError, match="allow_remote"):
            await adapter.preflight()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_service_status_marks_unsafe_without_preflight_health_bypass():
    service = AthenaService(
        config=AthenaConfig(
            hermes_referee=HermesRefereeConfig(enabled=True),
        )
    )
    adapter = type("Adapter", (), {})()
    adapter.preflight = AsyncMock(side_effect=HermesRefereeSafetyError("referee contract missing"))
    adapter.health = AsyncMock(side_effect=AssertionError("health must not precede safety"))
    service._hermes_adapter = adapter  # noqa: SLF001 - status boundary proof
    service._hermes_referee = HermesReferee(adapter)  # noqa: SLF001

    status = await service.hermes_referee_status()

    assert status["state"] == "unsafe"
    assert status["safety_verified"] is False
    adapter.health.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_builds_configured_referee_after_secret_boundary():
    service = AthenaService(
        config=AthenaConfig(
            hermes_referee=HermesRefereeConfig(
                enabled=True,
                endpoint="http://hermes.test:8642",
            )
        )
    )
    service._configure_hermes_referee()  # noqa: SLF001 - composition-root wiring proof
    try:
        assert isinstance(service._hermes_referee, HermesReferee)  # noqa: SLF001
        assert service._hermes_adapter is not None  # noqa: SLF001
        assert service.startup_health()["checks"]["hermes_referee"]["state"] == "configured"
    finally:
        await service._hermes_adapter.aclose()  # noqa: SLF001
