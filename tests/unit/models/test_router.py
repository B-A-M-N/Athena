from athena.models.fake import FakeModelProvider
import pytest
from athena.models.registry import ProviderRegistry
from athena.models.router import CAP_TOOLS, ModelRequirements, ModelRouter
from athena.protocol.models import PrivacyClass
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