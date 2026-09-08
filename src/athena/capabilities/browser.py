"""Structured browser automation capability (P1-28, SPEC §65).

Structured interaction first: navigate, snapshot the accessibility
tree, query elements, fill fields, click selectors, read text. The
visual fallback for pages where structure is unavailable is the
``computer`` capability (P1-20) — this module deliberately does not
screenshot; §65 prefers structure and the computer capability already
owns pixels.

The capability is OPTIONAL: it registers only when a driver is wired or the
first-party Playwright launcher is explicitly enabled. The driver seam
(``BrowserDriver`` protocol) keeps the capability testable headlessly and lets
an operator supply a remote/connect driver instead of a local browser.

Governance: navigation and interaction are NETWORK_READ/NETWORK_WRITE/
COMPUTER_INPUT effects respectively — the workspace NetworkPolicy gate,
the COMPUTER_INPUT ask-by-default rule, and the reality gate's
dangerous/persistent classification all apply through the standard
descriptor contract.
"""

from __future__ import annotations

import importlib
import json
import asyncio
import inspect
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from athena.protocol.capabilities import (
    Availability,
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
    ResourceClass,
)
from athena.network.target_policy import validate_target
from athena.protocol.resources import TaskResourceCloseResult

_playwright: Any = None
try:  # optional dependency; the capability registers only when present
    _playwright = importlib.import_module("playwright")
except Exception:  # ImportError and driver-load errors alike
    pass

_BROWSER_AVAILABILITY = (
    Availability.AVAILABLE if _playwright is not None else Availability.UNAVAILABLE
)

_MAX_SNAPSHOT_CHARS = 16_000
_MAX_TEXT_CHARS = 8_000
_MAX_URL_CHARS = 2048


@dataclass(frozen=True)
class ElementSnapshot:
    """One accessibility-tree row offered to the model."""

    role: str
    name: str
    selector: str
    value: str = ""


class BrowserDriver(Protocol):
    """The seam between the capability and a real browser session.

    A driver owns page state; the capability owns governance, argument
    validation, and result shaping. Playwright-shaped, but any structured
    browser may implement it (including a remote/connect driver).
    """

    async def navigate(self, url: str) -> dict[str, Any]: ...

    async def snapshot(self) -> list[ElementSnapshot]: ...

    async def query(self, selector: str) -> dict[str, Any] | None: ...

    async def fill(self, selector: str, value: str) -> dict[str, Any]: ...

    async def click(self, selector: str) -> dict[str, Any]: ...

    async def text(self) -> str: ...

    async def close(self) -> None: ...


class PlaywrightBrowserDriver:
    """First-party Playwright adapter implementing :class:`BrowserDriver`.

    Construction is asynchronous because Playwright starts a driver process
    and browser context. The capability accepts an async factory, so this
    adapter participates in the same task-scoped lifecycle as injected test
    or remote drivers.
    """

    def __init__(self, playwright, browser, context, page) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._network_policy = "allow"

    @classmethod
    async def preflight(
        cls,
        *,
        browser_name: str = "chromium",
        executable_path: str | None = None,
        channel: str | None = None,
        cdp_endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Check runtime usability without opening a durable browser session.

        Importability only proves that the Python adapter exists.  A local
        Playwright browser also needs its managed executable (or an explicit
        executable/channel); a CDP configuration instead needs a valid
        loopback/HTTP endpoint and is checked when the session connects.
        """
        if _playwright is None:
            return {
                "state": "unavailable",
                "installed": False,
                "reason": "Playwright is not installed",
            }
        if cdp_endpoint:
            parsed = urlsplit(str(cdp_endpoint))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return {
                    "state": "unavailable",
                    "installed": True,
                    "reason": "browser_cdp_endpoint must be an http(s) URL",
                }
            return {
                "state": "configured",
                "installed": True,
                "reason": "CDP endpoint will be checked on first connection",
            }
        try:
            async_playwright = importlib.import_module("playwright.async_api").async_playwright
            playwright = await async_playwright().start()
        except Exception as exc:
            return {
                "state": "unavailable",
                "installed": True,
                "reason": f"Playwright runtime could not start: {type(exc).__name__}: {exc}",
            }
        try:
            browser_type = getattr(playwright, browser_name, None)
            if browser_type is None:
                return {
                    "state": "unavailable",
                    "installed": True,
                    "reason": f"unsupported Playwright browser: {browser_name}",
                }
            if executable_path:
                executable = Path(executable_path).expanduser()
                if not executable.is_file() or not _is_executable(executable):
                    return {
                        "state": "unavailable",
                        "installed": True,
                        "reason": f"configured browser executable is missing: {executable}",
                    }
            elif not channel:
                executable = Path(str(browser_type.executable_path)).expanduser()
                if not executable.is_file() or not _is_executable(executable):
                    return {
                        "state": "unavailable",
                        "installed": True,
                        "reason": (
                            "Playwright is installed but its browser binary is missing; "
                            "run `playwright install " + browser_name + "`"
                        ),
                    }
            # A present executable is not sufficient proof: it may be
            # incompatible, broken, or a channel may resolve elsewhere on the
            # host. Launch and close one short-lived browser so the capability
            # is advertised only when the configured runtime actually works.
            launch_options: dict[str, Any] = {"headless": True, "timeout": 10_000}
            if executable_path:
                launch_options["executable_path"] = str(Path(executable_path).expanduser())
            if channel:
                launch_options["channel"] = channel
            browser = await browser_type.launch(**launch_options)
            try:
                await browser.close()
            except Exception:
                # Closing is best effort here; the process was launch-tested
                # and the outer Playwright runtime is stopped below.
                pass
            return {"state": "available", "installed": True, "reason": None}
        except Exception as exc:
            return {
                "state": "unavailable",
                "installed": True,
                "reason": f"configured browser could not launch: {type(exc).__name__}: {exc}",
            }
        finally:
            await playwright.stop()

    @classmethod
    async def launch(
        cls,
        *,
        browser_name: str = "chromium",
        headless: bool = True,
        launch_args: tuple[str, ...] = (),
        executable_path: str | None = None,
        channel: str | None = None,
        cdp_endpoint: str | None = None,
        timeout_ms: int = 12_000,
        viewport: tuple[int, int] | None = (1024, 768),
    ) -> "PlaywrightBrowserDriver":
        try:
            async_playwright = importlib.import_module("playwright.async_api").async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed") from exc
        playwright = await async_playwright().start()
        try:
            browser_type = getattr(playwright, browser_name, None)
            if browser_type is None:
                raise ValueError(f"unsupported Playwright browser: {browser_name}")
            if cdp_endpoint:
                if browser_name != "chromium":
                    raise ValueError("CDP connections require the Chromium Playwright engine")
                browser = await browser_type.connect_over_cdp(cdp_endpoint)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                options: dict[str, Any] = {
                    "headless": headless,
                    "args": list(launch_args),
                    "timeout": max(1, int(timeout_ms)),
                }
                if executable_path:
                    options["executable_path"] = executable_path
                if channel:
                    options["channel"] = channel
                browser = await browser_type.launch(**options)
                context = await browser.new_context(
                    viewport=(
                        {"width": viewport[0], "height": viewport[1]}
                        if viewport is not None
                        else None
                    )
                )
            page = await context.new_page()
            page.set_default_timeout(max(1, int(timeout_ms)))
            return cls(playwright, browser, context, page)
        except BaseException:
            await playwright.stop()
            raise

    async def navigate(self, url: str) -> dict[str, Any]:
        response = await self._page.goto(url, wait_until="domcontentloaded")
        return {
            "url": self._page.url,
            "title": await self._page.title(),
            "status": response.status if response is not None else None,
        }

    async def set_network_policy(self, policy: str | object | None) -> None:
        """Validate every Playwright request, including redirects/subresources."""
        requested = str(getattr(policy, "value", policy) or "allow").casefold()
        rank = {"allow": 0, "restricted": 1, "deny": 2}
        if rank.get(requested, 2) <= rank.get(self._network_policy, 0):
            return
        self._network_policy = requested

        async def _route(route) -> None:
            _target, error = validate_target(route.request.url, self._network_policy)
            if error:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await self._context.route("**/*", _route)

    async def snapshot(self) -> list[ElementSnapshot]:
        locator = self._page.locator("a,button,input,textarea,select,[role]")
        count = min(await locator.count(), 512)
        result: list[ElementSnapshot] = []
        for index in range(count):
            element = locator.nth(index)
            tag = str(await element.evaluate("node => node.tagName.toLowerCase()"))
            role = await element.get_attribute("role") or _default_role(tag)
            name = (
                await element.get_attribute("aria-label")
                or await element.get_attribute("placeholder")
                or (await element.inner_text()).strip()
            )
            selector = await element.evaluate(
                """
                node => {
                    const parts = [];
                    while (node && node.nodeType === 1 && node !== document.body) {
                        if (node.id) return `#${CSS.escape(node.id)}`;
                        let ordinal = 1;
                        let sibling = node.previousElementSibling;
                        while (sibling) {
                            if (sibling.tagName === node.tagName) ordinal += 1;
                            sibling = sibling.previousElementSibling;
                        }
                        parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${ordinal})`);
                        node = node.parentElement;
                    }
                    return parts.join(' > ');
                }
                """
            )
            result.append(ElementSnapshot(role=role, name=name[:512], selector=selector))
        return result

    async def query(self, selector: str) -> dict[str, Any] | None:
        element = self._page.locator(selector).first
        if await element.count() == 0:
            return None
        tag = str(await element.evaluate("node => node.tagName.toLowerCase()"))
        return {
            "role": await element.get_attribute("role") or _default_role(tag),
            "name": (
                await element.get_attribute("aria-label")
                or await element.get_attribute("placeholder")
                or (await element.inner_text()).strip()
            )[:512],
            "selector": selector,
            "value": await element.input_value() if tag in {"input", "textarea", "select"} else "",
        }

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        await self._page.locator(selector).fill(value)
        return {"selector": selector, "filled": True}

    async def click(self, selector: str) -> dict[str, Any]:
        await self._page.locator(selector).click()
        return {"selector": selector, "clicked": True}

    async def text(self) -> str:
        return await self._page.locator("body").inner_text()

    async def close(self) -> None:
        try:
            await self._context.close()
        finally:
            try:
                await self._browser.close()
            finally:
                await self._playwright.stop()


def _default_role(tag: str) -> str:
    return {
        "a": "link",
        "button": "button",
        "input": "textbox",
        "textarea": "textbox",
        "select": "combobox",
    }.get(tag, tag)


def _is_executable(path: Path) -> bool:
    """Avoid treating a downloaded browser data file as a runnable binary."""
    import os

    return bool(os.access(path, os.X_OK))


def _result(
    request: CapabilityRequest,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    meta: dict | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        call_id=request.call_id,
        capability_id=request.capability_id,
        status=(CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED),
        output=output,
        error=None if ok else (error or "browser operation failed"),
        metadata=dict(meta or {}),
    )


_BROWSER_DESCRIPTOR = CapabilityDescriptor(
    id="browser",
    description=(
        "Structured browser automation (SPEC 65 — prefer structure over "
        "pixels): navigate, snapshot the accessibility tree, query one "
        "element, fill fields, click selectors, read page text. Navigation "
        "is a NETWORK effect (workspace network policy applies); fill and "
        "click are COMPUTER_INPUT governed (ask-by-default). For pages "
        "with no usable structure, fall back to the computer capability."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"enum": ["navigate", "snapshot", "query", "fill", "click", "text"]},
            "url": {"type": "string", "maxLength": _MAX_URL_CHARS},
            "selector": {"type": "string", "maxLength": 512},
            "value": {"type": "string", "maxLength": 4096},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
    effects=frozenset(
        {EffectClass.NETWORK_READ, EffectClass.NETWORK_WRITE, EffectClass.COMPUTER_INPUT}
    ),
    operation_effects={
        "navigate": frozenset({EffectClass.NETWORK_READ, EffectClass.NETWORK_WRITE}),
        "snapshot": frozenset({EffectClass.NETWORK_READ}),
        "query": frozenset({EffectClass.NETWORK_READ}),
        "text": frozenset({EffectClass.NETWORK_READ}),
        "fill": frozenset({EffectClass.COMPUTER_INPUT}),
        # A click can submit a form or trigger a remote mutation; keep that
        # possibility inside the NETWORK_WRITE approval boundary.
        "click": frozenset({EffectClass.COMPUTER_INPUT, EffectClass.NETWORK_WRITE}),
    },
    resources=frozenset({ResourceClass.NETWORK}),
    origin=CapabilityOrigin.NATIVE,
    availability=_BROWSER_AVAILABILITY,
)


class BrowserCapability:
    """Structured browser automation through a pluggable driver."""

    descriptor = _BROWSER_DESCRIPTOR

    def __init__(
        self,
        driver_factory: Callable[[], BrowserDriver | Awaitable[BrowserDriver]] | None = None,
        *,
        session_scope: str = "task",
    ) -> None:
        # One driver belongs to one task (or session when no task id exists).
        # Keeping this state here, rather than in a provider/model turn,
        # preserves cookies, navigation, and page state across capability
        # calls while leaving governance on the dispatcher boundary.
        self._driver_factory = driver_factory
        if session_scope not in {"task", "session"}:
            raise ValueError("session_scope must be task or session")
        self._session_scope = session_scope
        self._drivers: dict[str, BrowserDriver] = {}
        self._driver_policies: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._last_error: str | None = None

    @staticmethod
    def available() -> bool:
        return _BROWSER_AVAILABILITY is Availability.AVAILABLE

    def health(self) -> dict[str, Any]:
        return {
            "state": (
                "degraded"
                if self._last_error
                else ("ready" if self._driver_factory is not None else "unavailable")
            ),
            "configured": self._driver_factory is not None,
            "active_sessions": len(self._drivers),
            **({"last_error": self._last_error} if self._last_error else {}),
        }

    async def close(self) -> None:
        """Close every task-scoped driver during capability/service shutdown."""
        async with self._lock:
            drivers = list(self._drivers.values())
            self._drivers.clear()
            self._driver_policies.clear()
        for driver in drivers:
            try:
                await driver.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass

    async def close_task(self, task_id: str) -> TaskResourceCloseResult:
        """Close a task-scoped driver and retain it if closure is unproven."""
        if self._session_scope != "task":
            return TaskResourceCloseResult(task_id=str(task_id), resource_type="browser")
        async with self._lock:
            driver = self._drivers.get(str(task_id))
        if driver is not None:
            try:
                await driver.close()
            except Exception as exc:  # noqa: BLE001 - retain driver for retry
                self._last_error = f"browser task cleanup failed: {type(exc).__name__}: {exc}"
                return TaskResourceCloseResult(
                    task_id=str(task_id),
                    resource_type="browser",
                    resource_ids=(str(task_id),),
                    unproven=({"session_id": str(task_id), "error": str(exc)},),
                    errors=({"session_id": str(task_id), "error": str(exc)},),
                )
            async with self._lock:
                self._drivers.pop(str(task_id), None)
                self._driver_policies.pop(id(driver), None)
            return TaskResourceCloseResult(
                task_id=str(task_id),
                resource_type="browser",
                resource_ids=(str(task_id),),
                closed_ids=(str(task_id),),
            )
        return TaskResourceCloseResult(task_id=str(task_id), resource_type="browser")

    async def close_session(self, session_id: str) -> None:
        """Close a session-scoped driver when its owning session is closed."""
        if self._session_scope != "session":
            return
        async with self._lock:
            driver = self._drivers.pop(str(session_id), None)
            if driver is not None:
                self._driver_policies.pop(id(driver), None)
        if driver is not None:
            await driver.close()

    def _scope_key(self, request: CapabilityRequest) -> str:
        preferred = request.task_id if self._session_scope == "task" else request.session_id
        return str(
            preferred
            or (request.session_id if self._session_scope == "task" else request.task_id)
            or request.call_id
            or "anonymous"
        )

    async def _driver_for(self, request: CapabilityRequest) -> BrowserDriver:
        key = self._scope_key(request)
        async with self._lock:
            driver = self._drivers.get(key)
            if driver is not None:
                return driver
            assert self._driver_factory is not None
            created = self._driver_factory()
            driver = await created if inspect.isawaitable(created) else created
            self._drivers[key] = driver
            return driver

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        if self._driver_factory is None:
            return _result(
                request,
                ok=False,
                error=(
                    "browser unavailable: install the 'playwright' extra and wire "
                    "a BrowserDriver factory into BrowserCapability"
                ),
            )
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        context = kw.get("context")
        network_policy = getattr(getattr(context, "workspace", None), "network_policy", None)
        policy_name = str(getattr(network_policy, "value", network_policy) or "allow").casefold()
        try:
            driver = await self._driver_for(request)
            if self._driver_policies.get(id(driver)) != policy_name:
                configure = getattr(driver, "set_network_policy", None)
                if callable(configure):
                    configured = configure(network_policy)
                    if inspect.isawaitable(configured):
                        await configured
                elif policy_name in {"restricted", "deny"}:
                    return _result(
                        request,
                        ok=False,
                        error="browser driver cannot enforce workspace network policy",
                    )
                self._driver_policies[id(driver)] = policy_name
            return await self._dispatch(
                request,
                driver,
                operation,
                args,
                policy_name=policy_name,
            )
        except Exception as exc:  # noqa: BLE001 - driver failures are results
            self._last_error = f"{type(exc).__name__}: {exc}"
            return _result(request, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _dispatch(
        self,
        request: CapabilityRequest,
        driver: BrowserDriver,
        operation: str,
        args: dict[str, Any],
        *,
        policy_name: str = "allow",
    ) -> CapabilityResult:
        if operation == "navigate":
            url = str(args.get("url") or "").strip()
            if not url:
                return _result(request, ok=False, error="navigate requires url")
            if not url.lower().startswith(("http://", "https://")):
                return _result(request, ok=False, error="url must use http or https")
            _target, error = validate_target(url, policy_name)
            if error:
                return _result(request, ok=False, error=error)
            outcome = await driver.navigate(url)
            return _result(request, output=json.dumps(outcome, default=str))

        if operation == "snapshot":
            elements = await driver.snapshot()
            lines = [
                f"{e.role}\t{e.name}\t{e.selector}" + (f"\t{e.value}" if e.value else "")
                for e in elements
            ]
            text = "\n".join(lines)[0:_MAX_SNAPSHOT_CHARS]
            return _result(
                request,
                output=text,
                meta={"elements": len(elements), "truncated": len(text) >= _MAX_SNAPSHOT_CHARS},
            )

        if operation == "query":
            selector = str(args.get("selector") or "")
            if not selector:
                return _result(request, ok=False, error="query requires selector")
            found = await driver.query(selector)
            if found is None:
                return _result(request, ok=False, error=f"no element matches: {selector}")
            return _result(request, output=json.dumps(found, default=str))

        if operation == "fill":
            selector = str(args.get("selector") or "")
            value = str(args.get("value") or "")
            if not selector:
                return _result(request, ok=False, error="fill requires selector")
            outcome = await driver.fill(selector, value)
            return _result(request, output=json.dumps(outcome, default=str))

        if operation == "click":
            selector = str(args.get("selector") or "")
            if not selector:
                return _result(request, ok=False, error="click requires selector")
            outcome = await driver.click(selector)
            return _result(request, output=json.dumps(outcome, default=str))

        if operation == "text":
            text = (await driver.text())[0:_MAX_TEXT_CHARS]
            return _result(
                request,
                output=text,
                meta={"truncated": len(text) >= _MAX_TEXT_CHARS},
            )

        return _result(request, ok=False, error=f"unknown browser operation: {operation!r}")


__all__ = [
    "BrowserCapability",
    "BrowserDriver",
    "ElementSnapshot",
    "PlaywrightBrowserDriver",
]
