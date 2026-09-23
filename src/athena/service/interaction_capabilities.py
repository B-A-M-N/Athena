"""Optional computer/browser capability registration and health evidence."""

from __future__ import annotations

import logging
from typing import Any

from athena.capabilities.browser import BrowserProxyConfig

_logger = logging.getLogger("athena.service")


def _assert_optional_consistency(
    registry: Any,
    capability_id: str,
    instance: Any,
    health: dict[str, Any],
) -> None:
    registered = any(
        getattr(getattr(executor, "descriptor", None), "id", None) == capability_id
        and executor is instance
        for executor in registry.iter_executors()
    )
    state = str(health.get("state") or "unknown")
    if registered != (instance is not None):
        raise RuntimeError(
            f"{capability_id} registry/instance mismatch: registered={registered}"
        )
    if registered and state not in {"ready", "available", "configured"}:
        raise RuntimeError(f"{capability_id} registered with non-ready health: {state}")


async def register_interaction_capabilities(
    ports: Any,
    registry: Any,
    *,
    computer_type: Any = None,
    browser_type: Any = None,
    browser_driver_type: Any = None,
) -> None:
    """Register computer and browser independently through the core registry.

    Each optional driver has its own exception boundary and health projection.
    A failure in one pack must not alter the registration or health evidence of
    its sibling.
    """
    if computer_type is None:
        from athena.capabilities.computer import ComputerCapability

        computer_type = ComputerCapability
    if browser_type is None:
        from athena.capabilities.browser import BrowserCapability

        browser_type = BrowserCapability
    if browser_driver_type is None:
        from athena.capabilities.browser import PlaywrightBrowserDriver

        browser_driver_type = PlaywrightBrowserDriver

    ports.computer = None
    ports.browser = None
    try:
        if computer_type.available():
            computer = computer_type(artifact_store=ports.artifacts)
            ports.computer_health = await computer.probe_health()
            ports.optional_capability_health["computer"] = {
                "installed": True,
                "configured": True,
                "state": ports.computer_health.get("state", "unknown"),
                "reason": ports.computer_health.get("reason"),
            }
            if ports.computer_health.get("state") == "ready":
                ports.computer = computer
                registry.register(ports.computer)
            else:
                _logger.info(
                    "computer capability unavailable at runtime: %s",
                    ports.computer_health.get("reason", "display probe failed"),
                )
        else:
            ports.computer_health = {
                "state": "unavailable",
                "backend": "none",
                "reason": "pyautogui is not importable",
            }
            ports.optional_capability_health["computer"] = {
                "installed": False,
                "configured": True,
                "state": "unavailable",
                "reason": ports.computer_health["reason"],
            }
            _logger.info(
                "computer capability unavailable: install the 'computer' extra (pyautogui)"
            )
    except Exception as exc:  # computer is independent from browser setup
        ports.computer = None
        ports.computer_health = {
            "state": "degraded",
            "backend": "unknown",
            "reason": f"computer setup failed: {type(exc).__name__}: {exc}",
        }
        ports.optional_capability_health["computer"] = {
            "installed": False,
            "configured": True,
            "state": "degraded",
            "reason": ports.computer_health["reason"],
        }
        _logger.info("computer capability unavailable: %s", exc)

    try:
        browser_factory = ports.config.browser_driver_factory
        if browser_factory is None and ports.config.browser_enabled:
            if browser_type.available():
                preflight = await browser_driver_type.preflight(
                    browser_name=ports.config.browser_engine,
                    executable_path=ports.config.browser_executable_path,
                    channel=ports.config.browser_channel,
                    cdp_endpoint=ports.config.browser_cdp_endpoint,
                )
                if preflight.get("state") in {"available", "configured"}:

                    async def browser_factory():
                        return await browser_driver_type.launch(
                            browser_name=ports.config.browser_engine,
                            headless=ports.config.browser_headless,
                            launch_args=ports.config.browser_launch_args,
                            executable_path=ports.config.browser_executable_path,
                            channel=ports.config.browser_channel,
                            cdp_endpoint=ports.config.browser_cdp_endpoint,
                            timeout_ms=ports.config.browser_timeout_ms,
                            viewport=ports.config.browser_viewport,
                            proxy_config=BrowserProxyConfig(
                                max_concurrent_tunnels=ports.config.browser_proxy_max_connections,
                                idle_timeout_seconds=ports.config.browser_proxy_idle_timeout_seconds,
                                max_tunnel_seconds=ports.config.browser_proxy_max_connection_seconds,
                            ),
                        )
                else:
                    ports.browser_health = {
                        "state": "unavailable",
                        "configured": True,
                        "active_sessions": 0,
                        "reason": preflight.get("reason") or "browser preflight failed",
                    }
                    ports.optional_capability_health["browser"] = {
                        "installed": True,
                        "configured": True,
                        "state": "unavailable",
                        "reason": ports.browser_health["reason"],
                    }
            else:
                ports.browser_health = {
                    "state": "unavailable",
                    "configured": True,
                    "active_sessions": 0,
                    "reason": "browser_enabled but Playwright is not installed",
                }
                ports.optional_capability_health["browser"] = {
                    "installed": False,
                    "configured": True,
                    "state": "unavailable",
                    "reason": ports.browser_health["reason"],
                }
        if browser_factory is not None:
            ports.browser = browser_type(
                driver_factory=browser_factory,
                session_scope=ports.config.browser_session_scope,
                artifact_store=ports.artifacts,
                auth_profiles=ports.config.browser_auth_profiles,
            )
            ports.browser_health = ports.browser.health()
            ports.optional_capability_health["browser"] = {
                "installed": True,
                "configured": True,
                "state": ports.browser_health.get("state", "unknown"),
                "reason": ports.browser_health.get("reason"),
            }
            registry.register(ports.browser)
            ports.register_shutdown_hook("browser_sessions", ports.browser.close)
        elif not ports.config.browser_enabled:
            ports.browser_health = {
                "state": "unavailable",
                "configured": False,
                "active_sessions": 0,
                "reason": "browser_driver_factory is not configured",
            }
            ports.optional_capability_health["browser"] = {
                "installed": browser_type.available(),
                "configured": False,
                "state": "unavailable",
                "reason": ports.browser_health["reason"],
            }
            _logger.info(
                "browser capability not wired: set browser_driver_factory to enable "
                "structured browser automation"
            )
    except Exception as exc:  # browser is independent from computer setup
        ports.browser = None
        ports.browser_health = {
            "state": "degraded",
            "configured": bool(ports.config.browser_enabled),
            "active_sessions": 0,
            "reason": f"browser setup failed: {type(exc).__name__}: {exc}",
        }
        ports.optional_capability_health["browser"] = {
            "installed": False,
            "configured": bool(ports.config.browser_enabled),
            "state": "degraded",
            "reason": ports.browser_health["reason"],
        }
        _logger.info("browser capability unavailable: %s", exc)

    _assert_optional_consistency(
        registry, "computer", ports.computer, ports.computer_health
    )
    _assert_optional_consistency(registry, "browser", ports.browser, ports.browser_health)


__all__ = ["register_interaction_capabilities"]
