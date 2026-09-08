"""Request-specific provider readiness admission tests."""

from __future__ import annotations

import pytest

from athena.models.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.protocol.errors import ModelProviderUnconfigured
from athena.protocol.models import PrivacyClass
from athena.protocol.tasks import AgentRequest, ModelPolicy
from athena.service.service import AthenaService


class _ReadinessFake(FakeModelProvider):
    def __init__(self, name: str, state: str, *, provider: str) -> None:
        super().__init__(
            model=name,
            provider=provider,
            tool_calling=True,
            privacy_class=PrivacyClass.LOCAL,
        )
        self.state = state

    def readiness(self) -> dict[str, str]:
        return {"state": self.state}


def _service_with_routes() -> tuple[AthenaService, _ReadinessFake]:
    ready = _ReadinessFake("model-a", "ready", provider="provider-a")
    missing = _ReadinessFake("model-b", "auth_missing", provider="provider-b")
    registry = ProviderRegistry()
    registry.register("provider-a", ready)
    registry.register("provider-b", missing)
    service = AthenaService()
    service._model_registry = registry
    service._router = ModelRouter(
        registry,
        role_policies={"judge": ModelPolicy(role="judge", allowed=("provider-b/model-b",))},
    )
    return service, missing


@pytest.mark.asyncio
async def test_unpinned_request_uses_ready_provider_only():
    service, _ = _service_with_routes()

    await service.require_agent_ready(AgentRequest(prompt="hello"))


@pytest.mark.asyncio
async def test_pinned_unready_provider_fails_before_task_admission():
    service, missing = _service_with_routes()

    with pytest.raises(ModelProviderUnconfigured) as error:
        await service.require_agent_ready(
            AgentRequest(
                prompt="hello",
                model_policy=ModelPolicy(allowed=("provider-b/model-b",)),
            )
        )
    assert error.value.data["provider_state"] == "request_unavailable"
    assert missing.state == "auth_missing"


@pytest.mark.asyncio
async def test_role_pinned_unready_provider_fails_then_admits_when_ready():
    service, missing = _service_with_routes()
    request = AgentRequest(prompt="judge this", model_policy=ModelPolicy(role="judge"))

    with pytest.raises(ModelProviderUnconfigured):
        await service.require_agent_ready(request)

    missing.state = "ready"
    await service.require_agent_ready(request)


@pytest.mark.asyncio
async def test_model_judgment_preflights_unready_judge_role():
    service, _ = _service_with_routes()
    request = AgentRequest(
        prompt="judge this",
        metadata={"acceptance_criteria": ["the result is correct"]},
    )

    with pytest.raises(ModelProviderUnconfigured) as error:
        await service.require_agent_ready(request)
    assert error.value.data["role"] == "judge"


@pytest.mark.asyncio
async def test_mandatory_judge_does_not_hide_unready_primary_role():
    primary = _ReadinessFake("model-a", "auth_missing", provider="provider-a")
    judge = _ReadinessFake("model-b", "ready", provider="provider-b")
    registry = ProviderRegistry()
    registry.register("provider-a", primary)
    registry.register("provider-b", judge)
    service = AthenaService()
    service._model_registry = registry
    service._router = ModelRouter(
        registry,
        role_policies={"judge": ModelPolicy(role="judge", allowed=("provider-b/model-b",))},
    )

    with pytest.raises(ModelProviderUnconfigured) as error:
        await service.require_agent_ready(
            AgentRequest(
                prompt="judge this",
                model_policy=ModelPolicy(allowed=("provider-a/model-a",)),
                metadata={"acceptance_criteria": ["the result is correct"]},
            )
        )
    assert error.value.data["role"] == "primary"
