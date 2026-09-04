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


class _ReadinessFake(FakeModelProvider):
    def __init__(self, name: str, readiness_state: str, *, provider: str | None = None) -> None:
        super().__init__(
            model=name,
            provider=provider or name,
            tool_calling=True,
            privacy_class=PrivacyClass.LOCAL,
        )
        self.readiness_state = readiness_state

    def readiness(self) -> dict[str, str]:
        return {"state": self.readiness_state}


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


async def test_router_excludes_unready_providers_and_rechecks_readiness():
    ready = _ReadinessFake("ready-model", "ready", provider="ready")
    unavailable = _ReadinessFake("missing-auth", "auth_missing", provider="missing")
    reg = _registry({"ready": ready, "missing": unavailable})
    router = ModelRouter(reg)

    selected = await router.select(policy=ModelPolicy(require_tools=True))
    assert selected.provider == "ready"

    with pytest.raises(ModelUnavailable):
        await router.select(
            policy=ModelPolicy(allowed=("missing/missing-auth",), require_tools=True)
        )

    unavailable.readiness_state = "ready"
    selected = await router.select(
        policy=ModelPolicy(allowed=("missing/missing-auth",), require_tools=True)
    )
    assert selected.provider == "missing"


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


async def test_router_role_preference_can_choose_cost_over_cold_latency():
    expensive_fast = _fake("fast", privacy=PrivacyClass.LOCAL)
    cheap_slow = _fake("cheap", privacy=PrivacyClass.LOCAL)
    expensive_fast._info_kwargs["latency_class"] = "fast"
    cheap_slow._info_kwargs["latency_class"] = "slow"
    expensive_fast._info_kwargs["cost"] = CostInfo(per_1m_input=10, per_1m_output=10)
    cheap_slow._info_kwargs["cost"] = CostInfo(per_1m_input=0, per_1m_output=0)
    router = ModelRouter(_registry({"fast": expensive_fast, "cheap": cheap_slow}))

    selection = await router.select(
        policy=ModelPolicy(require_tools=False, routing_preference="cost")
    )

    assert selection.provider == "cheap"


async def test_configured_role_preference_is_narrowed_into_task_policy():
    fast = _fake("fast", privacy=PrivacyClass.LOCAL, tool_calling=True)
    cheap = _fake("cheap", privacy=PrivacyClass.LOCAL, tool_calling=True)
    fast._info_kwargs["latency_class"] = "fast"
    cheap._info_kwargs["latency_class"] = "slow"
    fast._info_kwargs["cost"] = CostInfo(per_1m_input=10, per_1m_output=10)
    cheap._info_kwargs["cost"] = CostInfo(per_1m_input=0, per_1m_output=0)
    router = ModelRouter(
        _registry({"fast": fast, "cheap": cheap}),
        role_policies={"coder": ModelPolicy(role="coder", routing_preference="cost")},
    )

    selection = await router.select(policy=ModelPolicy(role="coder", require_tools=False))

    assert selection.provider == "cheap"


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


# ---------------------------------------------------------------------- #
# Minimum-context regression (P0-5): the kernel used to read a field that
# does not exist (``min_context_window``), silently dropping the compiler's
# context requirement before routing. These tests pin the field name and
# prove capacity filtering actually excludes too-small models.
# ---------------------------------------------------------------------- #


async def test_minimum_context_tokens_requirement_selects_the_large_model():
    """model A context=8k, model B context=128k, requirement=20k -> B."""
    small = FakeModelProvider(
        model="ctx-8k",
        provider="smallprov",
        context_limit=8 * 1024,
    )
    large = FakeModelProvider(
        model="ctx-128k",
        provider="largeprov",
        context_limit=128 * 1024,
    )
    router = ModelRouter(_registry({"smallprov": small, "largeprov": large}))

    selection = await router.select(
        policy=ModelPolicy(role="primary", require_tools=False),
        requirements=ModelRequirements(minimum_context_tokens=20 * 1024),
    )
    assert (selection.provider, selection.model) == ("largeprov", "ctx-128k")


async def test_minimum_context_tokens_rejects_undersized_only_model():
    """When the only registered model cannot hold the request, selection
    must fail loudly rather than silently route to an undersized context
    (the old ``min_context_window`` getattr always got None and let it
    through)."""
    from athena.protocol.errors import ModelUnavailable

    narrow = FakeModelProvider(
        model="only-model",
        provider="onlyprov",
        context_limit=4 * 1024,
    )
    router = ModelRouter(_registry({"onlyprov": narrow}))

    # Below the limit: selectable.
    ok = await router.select(
        policy=ModelPolicy(role="primary", require_tools=False),
        requirements=ModelRequirements(minimum_context_tokens=2 * 1024),
    )
    assert (ok.provider, ok.model) == ("onlyprov", "only-model")

    # Above the limit: loud failure, never a silent undersized route.
    with pytest.raises(ModelUnavailable):
        await router.select(
            policy=ModelPolicy(role="primary", require_tools=False),
            requirements=ModelRequirements(minimum_context_tokens=64 * 1024),
        )


async def test_kernel_selects_model_by_compiled_minimum_context():
    """End-to-end P0-5 regression: AgentKernel._select_model must forward
    the compiler's ``minimum_context_tokens`` (not the nonexistent
    ``min_context_window``) so a 128k model is chosen when the compiled
    request needs 20k and an 8k model is also registered."""
    from athena.kernel.kernel import AgentKernel

    field = ModelRequirements.__dataclass_fields__.get("minimum_context_tokens")
    assert field is not None, "ModelRequirements.minimum_context_tokens must exist"

    class Holder:
        """Minimal stand-in for the kernel's compiled-context handle."""

        def __init__(self, requirements: ModelRequirements):
            self.requirements = requirements

    class TaskStub:
        model_policy = ModelPolicy(role="primary", require_tools=False)

    kernel = AgentKernel.__new__(AgentKernel)
    small = FakeModelProvider(
        model="ctx-8k", provider="smallprov", context_limit=8 * 1024, tool_calling=True
    )
    large = FakeModelProvider(
        model="ctx-128k", provider="largeprov", context_limit=128 * 1024, tool_calling=True
    )
    kernel._router = ModelRouter(
        _registry({"smallprov": small, "largeprov": large})
    )
    compiled = Holder(
        ModelRequirements(minimum_context_tokens=20 * 1024, needs_tools=True)
    )

    selection = await kernel._select_model(task=TaskStub(), compiled=compiled)
    assert (selection.provider, selection.model) == ("largeprov", "ctx-128k")


# ---------------------------------------------------------------------- #
# Model-granular fallback (task #12): excluding a failed (provider, model)
# pair must NOT ban the provider's healthy sibling models. A bare provider
# name in ``exclude`` still bans the whole provider (legacy behavior).
# ---------------------------------------------------------------------- #


class _MultiModelProvider(FakeModelProvider):
    """One provider that offers several models under a single name."""

    def __init__(self, provider: str, models: list[str], **kwargs) -> None:
        super().__init__(provider=provider, model=models[0], **kwargs)
        self._models = models

    async def list_models(self) -> list:
        from athena.protocol.models import ModelInfo

        return [
            ModelInfo(id=m, provider=self._provider, **self._info_kwargs)
            for m in self._models
        ]


async def test_excluding_one_model_pair_keeps_healthy_sibling_models():
    """Excluding (provider, bad) leaves (provider, good) eligible."""
    provider = _MultiModelProvider("megaprov", ["model-good", "model-bad"])
    reg = _registry({"megaprov": provider})
    router = ModelRouter(reg)

    sel = await router.select(exclude=frozenset({("megaprov", "model-bad")}))

    assert sel.provider == "megaprov"
    assert sel.model == "model-good"


async def test_model_pair_exclusion_does_not_ban_provider_for_that_model_only():
    """A healthy model survives even when a sibling pair is excluded."""
    provider = _MultiModelProvider("megaprov", ["model-a", "model-b", "model-c"])
    reg = _registry({"megaprov": provider})
    router = ModelRouter(reg)

    sel = await router.select(exclude=frozenset({("megaprov", "model-a")}))

    assert sel.provider == "megaprov"
    assert sel.model in {"model-b", "model-c"}
    assert sel.model != "model-a"


async def test_excluding_every_model_pair_exhausts_that_provider():
    """When all of a provider's pairs are excluded, it cannot be selected."""
    provider = _MultiModelProvider("megaprov", ["model-a", "model-b"])
    reg = _registry({"megaprov": provider})
    router = ModelRouter(reg)

    with pytest.raises(ModelUnavailable):
        await router.select(
            exclude=frozenset({("megaprov", "model-a"), ("megaprov", "model-b")})
        )


async def test_bare_provider_name_still_excludes_the_whole_provider():
    """Legacy whole-provider ban (bare provider string) is preserved."""
    provider = _MultiModelProvider("megaprov", ["model-a", "model-b"])
    reg = _registry({"megaprov": provider})
    router = ModelRouter(reg)

    with pytest.raises(ModelUnavailable):
        await router.select(exclude=frozenset({"megaprov"}))
