from decimal import Decimal

from athena.models.fake import FakeModelProvider
import pytest
from athena.models.registry import ProviderRegistry
from athena.models.router import CAP_TOOLS, ModelRequirements, ModelRouter
from athena.protocol.errors import ModelUnavailable
from athena.protocol.models import CostInfo, PrivacyClass
from athena.protocol.tasks import ModelPolicy


def _registry(providers) -> ProviderRegistry:
    reg = ProviderRegistry()
    for name, provider in providers.items():
        reg.register(name, provider)
    return reg


def _fake(name: str, *, tool_calling=False, privacy=None):
    kw = {"model": name, "provider": name}
    if tool_calling:
        kw["tool_calling"] = True
    if privacy is not None:
        kw["privacy_class"] = privacy
    return FakeModelProvider(**kw)


@pytest.mark.athena_claim("BHV-035")
@pytest.mark.athena_evidence("test", "invariant")
async def test_selects_fake_provider_when_tools_required():
    """BHV-034: provider declaring tool_calling is selected when tools required."""
    tools = _fake("tools", tool_calling=True)
    plain = _fake("plain")
    reg = _registry({"tools": tools, "plain": plain})

    router = ModelRouter(reg)
    reqs = ModelRequirements(required_capabilities=frozenset({CAP_TOOLS}), needs_tools=True)
    sel = await router.select(requirements=reqs)

    assert sel.info.tool_calling is True


@pytest.mark.athena_claim("BHV-035", "BHV-037")
@pytest.mark.athena_evidence("test", "invariant")
async def test_offline_policy_selects_only_local_models():
    """BHV-038: offline policy gates to LOCAL privacy only."""
    local = _fake("local", privacy=PrivacyClass.LOCAL)
    remote = _fake("remote", privacy=PrivacyClass.REMOTE)
    reg = _registry({"local": local, "remote": remote})

    router = ModelRouter(reg)
    sel = await router.select(policy=ModelPolicy(privacy="offline", require_tools=False))

    assert sel.info.privacy_class is PrivacyClass.LOCAL
    # The remote model must have been filtered out.
    assert sel.provider == "local"


async def test_strict_cost_policy_rejects_partial_or_non_usd_pricing():
    partial = _fake("partial", privacy=PrivacyClass.LOCAL)
    partial._info_kwargs["cost"] = CostInfo(per_1m_input=0.0, per_1m_output=None)
    non_usd = _fake("non-usd", privacy=PrivacyClass.LOCAL)
    non_usd._info_kwargs["cost"] = CostInfo(per_1m_input=0.0, per_1m_output=0.0, currency="EUR")
    registry = _registry({"partial": partial, "non-usd": non_usd})

    with pytest.raises(ModelUnavailable):
        await ModelRouter(registry).select(policy=ModelPolicy(max_cost_usd=Decimal("1.00")))


@pytest.mark.athena_claim("BHV-035")
@pytest.mark.athena_evidence("test", "invariant")
async def test_router_has_no_provider_specific_branches():
    """INV-006: the router performs no provider-specific branching."""
    import inspect

    from athena.models import router as router_mod

    src = inspect.getsource(router_mod)
    for token in (
        "provider == ",
        "provider in (",
        'provider == "',
        "if provider",
        "openai",
        "anthropic",
    ):
        assert token.lower() not in src.lower()


@pytest.mark.athena_claim("BHV-037")
@pytest.mark.athena_evidence("test", "security")
async def test_fallback_after_first_choice_fails_respects_privacy():
    """BHV-037: when the first choice fails, re-select respects locality."""
    local_a = _fake("local-a", privacy=PrivacyClass.LOCAL)
    local_b = _fake("local-b", privacy=PrivacyClass.LOCAL)
    remote = _fake("remote", privacy=PrivacyClass.REMOTE)
    reg = _registry({"local-a": local_a, "local-b": local_b, "remote": remote})

    router = ModelRouter(reg)
    policy = ModelPolicy(privacy="local-pref", require_tools=False)

    first = await router.select(policy=policy)
    # Simulate failure of the first choice by removing it from the registry.
    reg.unregister(first.provider)

    fallback = await router.select(policy=policy)
    assert fallback.provider != first.provider
    # Privacy discipline is preserved: still local.
    assert fallback.info.privacy_class is PrivacyClass.LOCAL


async def test_router_uses_role_scoped_reliability_after_policy_filters():
    slow = _fake("slow", privacy=PrivacyClass.LOCAL)
    reliable = _fake("reliable", privacy=PrivacyClass.LOCAL)
    reg = _registry({"slow": slow, "reliable": reliable})

    class Usage:
        async def list_recent(self, limit=500):
            del limit
            return [
                {
                    "provider": "slow",
                    "model": "slow",
                    "metadata": {
                        "role": "primary",
                        "state": "failed",
                        "duration_ms": 1,
                    },
                },
                {
                    "provider": "slow",
                    "model": "slow",
                    "metadata": {
                        "role": "primary",
                        "state": "failed",
                        "duration_ms": 1,
                    },
                },
                {
                    "provider": "reliable",
                    "model": "reliable",
                    "metadata": {
                        "role": "primary",
                        "state": "success",
                        "duration_ms": 50,
                    },
                },
            ]

    selection = await ModelRouter(reg, usage_provider=Usage()).select(
        policy=ModelPolicy(require_tools=False),
    )
    assert selection.provider == "reliable"


async def test_router_uses_declared_latency_class_on_cold_start():
    slow = _fake("slow", privacy=PrivacyClass.LOCAL)
    fast = _fake("fast", privacy=PrivacyClass.LOCAL)
    slow._info_kwargs["latency_class"] = "slow"
    fast._info_kwargs["latency_class"] = "fast"
    router = ModelRouter(_registry({"slow": slow, "fast": fast}))

    selection = await router.select(policy=ModelPolicy(require_tools=False))

    assert selection.provider == "fast"


async def test_router_cache_keeps_role_histories_separate():
    primary_model = _fake("primary-model", privacy=PrivacyClass.LOCAL)
    judge_model = _fake("judge-model", privacy=PrivacyClass.LOCAL)
    reg = _registry(
        {
            "primary-model": primary_model,
            "judge-model": judge_model,
        }
    )

    class Usage:
        async def list_recent(self, limit=500):
            del limit
            return [
                {
                    "provider": "primary-model",
                    "model": "primary-model",
                    "metadata": {"role": "primary", "state": "failed", "duration_ms": 1},
                },
                {
                    "provider": "judge-model",
                    "model": "judge-model",
                    "metadata": {"role": "primary", "state": "success", "duration_ms": 1},
                },
                {
                    "provider": "primary-model",
                    "model": "primary-model",
                    "metadata": {"role": "judge", "state": "success", "duration_ms": 1},
                },
                {
                    "provider": "judge-model",
                    "model": "judge-model",
                    "metadata": {"role": "judge", "state": "failed", "duration_ms": 1},
                },
            ]

    router = ModelRouter(reg, usage_provider=Usage())
    primary = await router.select(
        policy=ModelPolicy(role="primary", require_tools=False),
    )
    judge = await router.select(
        policy=ModelPolicy(role="judge", require_tools=False),
    )

    assert primary.provider == "judge-model"
    assert judge.provider == "primary-model"
    assert "history=rolling_attempts" in primary.rationale
    assert "history=rolling_attempts" in judge.rationale


async def test_provider_registry_resolve_uses_cached_model_inventory():
    class CountingProvider(FakeModelProvider):
        def __init__(self):
            super().__init__(model="counted", provider="counted")
            self.list_calls = 0

        async def list_models(self):
            self.list_calls += 1
            return await super().list_models()

    provider = CountingProvider()
    registry = _registry({"counted": provider})

    first = await registry.resolve("counted", "counted")
    second = await registry.resolve("counted", "counted")

    assert second is first
    assert provider.list_calls == 1
    assert registry.generation == 1

    await registry.refresh_models()
    assert provider.list_calls == 2
