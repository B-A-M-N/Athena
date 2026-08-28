"""Tests for role-divided model selection and the wiring built on it."""

from __future__ import annotations

import pytest

from athena.models.registry import ProviderRegistry
from athena.models.router import ModelRouter
from athena.protocol.models import (
    CostInfo,
    ModelEvent,
    ModelEventType,
    ModelInfo,
    PrivacyClass,
)
from athena.protocol.tasks import ModelPolicy


class _StaticProvider:
    """Declares two models; satisfies the registry's duck-typed check."""

    def __init__(self, models, name: str) -> None:
        self._models = list(models)
        self.provider = name

    async def list_models(self):
        return list(self._models)

    def provider_for(self, name):
        return self

    async def complete(self, request):
        yield ModelEvent(type=ModelEventType.DELTA, request_id=request.request_id)


def _info(model_id: str, cost_usd: float) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider="prov",
        tool_calling=True,
        streaming=True,
        cost=CostInfo(per_1m_input=cost_usd, per_1m_output=0.0),
        privacy_class=PrivacyClass.LOCAL,
    )


@pytest.fixture
def registry():
    reg = ProviderRegistry()
    reg.register("prov", _StaticProvider([_info("cheap", 0.10), _info("pricey", 5.00)], "prov"))
    return reg


async def test_role_policy_pins_model(registry):
    router = ModelRouter(
        registry,
        role_policies={"summarizer": ModelPolicy(role="summarizer", allowed=("prov/cheap",))},
    )
    sel = await router.select(policy=ModelPolicy(role="summarizer"))
    assert sel.model == "cheap"


async def test_primary_fallback_when_role_unassigned(registry):
    """No role entry -> falls back to the primary (user's global choice)."""
    router = ModelRouter(
        registry,
        role_policies={"primary": ModelPolicy(role="primary", allowed=("prov/pricey",))},
    )
    sel = await router.select(policy=ModelPolicy(role="judge"))
    assert sel.model == "pricey"


async def test_no_roles_uses_cost_ordering(registry):
    router = ModelRouter(registry)
    sel = await router.select(policy=ModelPolicy())
    assert sel.model == "cheap"


async def test_caller_allowlist_wins_over_role(registry):
    router = ModelRouter(
        registry,
        role_policies={"summarizer": ModelPolicy(role="summarizer", allowed=("prov/cheap",))},
    )
    sel = await router.select(policy=ModelPolicy(role="summarizer", allowed=("prov/pricey",)))
    assert sel.model == "pricey"


@pytest.mark.athena_scenario("FUSE-001")
def test_router_exposes_selected_provider_without_second_authority(registry):
    router = ModelRouter(registry)
    assert router.provider_for("prov") is registry.provider_for("prov")
