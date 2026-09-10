from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from athena.capabilities.browser import BrowserCapability, ElementSnapshot, PlaywrightBrowserDriver
from athena.network.target_policy import validate_target
from athena.protocol.capabilities import CapabilityRequest
from athena.protocol.tasks import NetworkPolicy, WorkspaceSpec


def test_restricted_target_policy_rejects_local_and_private_resolution():
    _target, error = validate_target("http://127.0.0.1:8080", NetworkPolicy.RESTRICTED)
    assert error and "local" in error

    _target, error = validate_target("http://localhost", "restricted")
    assert error and "local" in error

    _target, error = validate_target(
        "https://public.example.test", "restricted", resolver=lambda _host, _port: ("10.0.0.7",)
    )
    assert error and "private" in error


def test_restricted_target_policy_returns_pinned_public_addresses():
    target, error = validate_target(
        "https://public.example.test/path",
        "restricted",
        resolver=lambda _host, _port: ("93.184.216.34", "2606:4700:4700::1111"),
    )
    assert error is None
    assert target is not None
    assert target.hostname == "public.example.test"
    assert target.addresses == ("93.184.216.34", "2606:4700:4700::1111")


@pytest.mark.parametrize(
    "target",
    [
        "http://[::ffff:127.0.0.1]/",
        "http://user:pass@example.test/",
        "ftp://example.test/",
        "http://example.test:99999/",
        "http://[broken/",
    ],
)
def test_target_policy_rejects_hostile_http_authorities(target):
    _validated, error = validate_target(target, "restricted")
    assert error


def test_restricted_target_rejects_dns_rebinding_to_private_address():
    calls = []

    def resolver(host, port):
        calls.append((host, port))
        return ("93.184.216.34", "169.254.169.254")

    target, error = validate_target("https://public.example.test", "restricted", resolver=resolver)
    assert target is None
    assert "private/local" in (error or "")
    assert calls == [("public.example.test", 0)]


@given(st.integers(min_value=0, max_value=2**32 - 1))
def test_restricted_policy_never_accepts_non_public_resolved_ipv4(value):
    """The resolver result, not the hostname spelling, is the authority."""
    address = ipaddress.IPv4Address(value)
    target, error = validate_target(
        "https://arbitrary.example.test/resource",
        "restricted",
        resolver=lambda _host, _port: (str(address),),
    )
    non_public = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )
    if non_public:
        assert target is None
        assert error
    else:
        assert target is not None
        assert error is None


class _PolicyDriver:
    def __init__(self) -> None:
        self.policies: list[object] = []

    async def set_network_policy(self, policy):
        self.policies.append(policy)

    async def navigate(self, url):
        return {"url": url, "status": 200}

    async def snapshot(self):
        return [ElementSnapshot(role="link", name="ok", selector="#ok")]

    async def query(self, selector):
        return None

    async def fill(self, selector, value):
        return {}

    async def click(self, selector):
        return {}

    async def text(self):
        return ""

    async def close(self):
        pass


def _request(**arguments):
    return CapabilityRequest(
        capability_id="browser",
        arguments=arguments,
        task_id="task-1",
        call_id="call-1",
    )


@pytest.mark.asyncio
async def test_browser_applies_workspace_policy_before_navigation(monkeypatch):
    driver = _PolicyDriver()
    cap = BrowserCapability(driver_factory=lambda: driver)
    context = SimpleNamespace(
        workspace=WorkspaceSpec(
            id="workspace",
            root="/tmp",
            network_policy=NetworkPolicy.RESTRICTED,
        )
    )
    monkeypatch.setattr(
        "athena.network.target_policy.resolve_addresses",
        lambda _host, _port: ("93.184.216.34",),
    )

    blocked = await cap.invoke(
        _request(operation="navigate", url="http://127.0.0.1:8080"), context=context
    )
    assert blocked.status.value == "failed"
    assert not any(call[0] == "navigate" for call in getattr(driver, "calls", ()))

    allowed = await cap.invoke(
        _request(operation="navigate", url="https://public.example.test"), context=context
    )
    assert allowed.status.value == "ok"
    assert driver.policies == [NetworkPolicy.RESTRICTED]
    await cap.close()


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.action: str | None = None

    async def abort(self, reason):
        self.action = f"abort:{reason}"

    async def continue_(self):
        self.action = "continue"


class _Context:
    def __init__(self) -> None:
        self.handler = None

    async def route(self, _pattern, handler):
        self.handler = handler


@pytest.mark.asyncio
async def test_playwright_driver_policy_intercepts_private_subresources(monkeypatch):
    monkeypatch.setattr(
        "athena.network.target_policy.resolve_addresses",
        lambda host, _port: (
            ("10.0.0.8",) if host == "internal.example.test" else ("93.184.216.34",)
        ),
    )
    context = _Context()
    driver = PlaywrightBrowserDriver.__new__(PlaywrightBrowserDriver)
    driver._context = context
    driver._network_policy = "allow"
    await driver.set_network_policy(NetworkPolicy.RESTRICTED)
    assert context.handler is not None

    public = _Route("https://public.example.test/app.js")
    await context.handler(public)
    assert public.action == "continue"

    private = _Route("https://internal.example.test/secret.js")
    await context.handler(private)
    assert private.action == "abort:blockedbyclient"
