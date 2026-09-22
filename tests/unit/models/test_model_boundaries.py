"""Contracts for normalized model inventory and admission diagnostics."""

from __future__ import annotations

import pytest

from athena.models.compat.profiles import ModelProfile
from athena.models.fake import FakeModelProvider
from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRequirements, ModelRouter
from athena.context.requirements import build_model_requirements
from athena.protocol.errors import ModelUnavailable
from athena.protocol.models import PrivacyClass
from athena.protocol.tasks import ModelPolicy, TaskSpec


@pytest.mark.asyncio
async def test_registry_normalizes_profile_capacity_before_router_admission():
    provider = FakeModelProvider(
        model="profiled",
        provider="local",
        privacy_class=PrivacyClass.LOCAL,
    )
    registry = ProviderRegistry()
    registry.register("local", provider)
    profile = ModelProfile(model_pattern="profiled", context_window=32_000, output_limit=4_000)
    registry.set_model_profile("local", "profiled", profile)

    inventory = await registry.list_effective_models()
    assert inventory[0].info.context_limit == 32_000
    assert inventory[0].info.max_output_tokens == 4_000
    assert inventory[0].model_profile is profile

    selection = await ModelRouter(registry).select(
        policy=ModelPolicy(privacy="offline"),
        requirements=ModelRequirements(minimum_context_window_tokens=16_000),
    )
    assert selection.effective is inventory[0]


@pytest.mark.asyncio
async def test_router_exposes_structured_admission_rejections():
    provider = FakeModelProvider(
        model="small",
        provider="local",
        context_limit=1_024,
        privacy_class=PrivacyClass.LOCAL,
    )
    registry = ProviderRegistry()
    registry.register("local", provider)

    with pytest.raises(ModelUnavailable) as error:
        await ModelRouter(registry).select(
            requirements=ModelRequirements(minimum_context_window_tokens=8_192),
        )

    assert error.value.data["rejections"] == (
        {
            "reason": "capacity",
            "provider": "local",
            "model": "small",
            "detail": "context capacity too small",
        },
    )


@pytest.mark.asyncio
async def test_router_rejects_insufficient_declared_output_capacity():
    provider = FakeModelProvider(
        model="short-output",
        provider="local",
        context_limit=16_000,
        max_output_tokens=512,
        privacy_class=PrivacyClass.LOCAL,
    )
    registry = ProviderRegistry()
    registry.register("local", provider)

    with pytest.raises(ModelUnavailable) as error:
        await ModelRouter(registry).select(
            requirements=ModelRequirements(requested_output_tokens=4_096),
        )

    assert error.value.data["rejections"][0]["detail"] == "output capacity too small"


@pytest.mark.asyncio
async def test_unknown_output_capacity_is_rejected_above_conservative_threshold():
    provider = FakeModelProvider(model="unknown-output", provider="local")
    registry = ProviderRegistry()
    registry.register("local", provider)

    with pytest.raises(ModelUnavailable) as error:
        await ModelRouter(registry).select(
            requirements=ModelRequirements(requested_output_tokens=4_097),
        )

    assert error.value.data["rejections"][0]["detail"] == "output capacity unknown"


def test_context_compilation_keeps_input_window_and_output_request_separate():
    requirements = build_model_requirements(
        TaskSpec(id="task-1", objective="answer"),
        (),
        1_200,
        reserve_output=400,
        safety_margin=50,
    )

    assert requirements.estimated_input_tokens == 1_200
    assert requirements.minimum_context_window_tokens == 1_650
    assert requirements.requested_output_tokens == 400


@pytest.mark.asyncio
async def test_cost_admission_does_not_charge_context_reserve_as_input_again():
    provider = FakeModelProvider(
        model="priced",
        provider="local",
        context_limit=8_000,
        max_output_tokens=4_096,
        privacy_class=PrivacyClass.LOCAL,
        cost={"per_1m_input": 1.0, "per_1m_output": 2.0},
    )
    registry = ProviderRegistry()
    registry.register("local", provider)

    selection = await ModelRouter(registry).select(
        policy=ModelPolicy(max_cost_usd=0.0095),
        requirements=ModelRequirements(
            estimated_input_tokens=1_000,
            minimum_context_window_tokens=5_500,
            requested_output_tokens=4_000,
        ),
    )

    assert selection.model == "priced"
