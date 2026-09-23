from types import SimpleNamespace

import pytest

from athena.capabilities.browser import BrowserCapability
from athena.capabilities.computer import ComputerCapability
from athena.capabilities.registry import CapabilityRegistry
from athena.service.interaction_capabilities import register_interaction_capabilities


class _HealthyComputer:
    descriptor = ComputerCapability.descriptor

    @staticmethod
    def available() -> bool:
        return True

    def __init__(self, *, artifact_store=None):
        self.artifact_store = artifact_store

    async def probe_health(self):
        return {"state": "ready", "backend": "test"}


class _AvailableBrowser:
    descriptor = BrowserCapability.descriptor

    @staticmethod
    def available() -> bool:
        return True


class _FailingBrowserDriver:
    @classmethod
    async def preflight(cls, **kwargs):
        raise RuntimeError("browser preflight failed")


@pytest.mark.asyncio
async def test_browser_preflight_failure_does_not_degrade_ready_computer():
    config = SimpleNamespace(
        browser_driver_factory=None,
        browser_enabled=True,
        browser_engine="chromium",
        browser_executable_path=None,
        browser_channel=None,
        browser_cdp_endpoint=None,
        browser_headless=True,
        browser_launch_args=(),
        browser_timeout_ms=1000,
        browser_viewport=(800, 600),
        browser_proxy_max_connections=2,
        browser_proxy_idle_timeout_seconds=1.0,
        browser_proxy_max_connection_seconds=2.0,
        browser_session_scope="task",
        browser_auth_profiles={},
    )
    ports = SimpleNamespace(
        artifacts=None,
        config=config,
        computer=None,
        browser=None,
        computer_health={},
        browser_health={},
        optional_capability_health={},
        register_shutdown_hook=lambda *_args: None,
    )
    registry = CapabilityRegistry()

    await register_interaction_capabilities(
        ports,
        registry,
        computer_type=_HealthyComputer,
        browser_type=_AvailableBrowser,
        browser_driver_type=_FailingBrowserDriver,
    )

    registered = {executor.descriptor.id for executor in registry.iter_executors()}
    assert registered == {"computer"}
    assert ports.computer is not None
    assert ports.computer_health["state"] == "ready"
    assert ports.optional_capability_health["computer"]["state"] == "ready"
    assert ports.browser is None
    assert ports.browser_health["state"] == "degraded"
    assert ports.optional_capability_health["browser"]["state"] == "degraded"
