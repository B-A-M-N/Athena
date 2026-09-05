"""Structured browser automation capability (P1-28, SPEC §65).

Structured interaction first: navigate, snapshot the accessibility
tree, query elements, fill fields, click selectors, read text. The
visual fallback for pages where structure is unavailable is the
``computer`` capability (P1-20) — this module deliberately does not
screenshot; §65 prefers structure and the computer capability already
owns pixels.

The capability is OPTIONAL: it registers only when a Playwright-style
driver is importable and wired. The driver seam (``BrowserDriver``
protocol) keeps the capability testable headlessly and lets an operator
supply a remote/connect driver instead of a local browser.

Governance: navigation and interaction are NETWORK_READ/NETWORK_WRITE/
COMPUTER_INPUT effects respectively — the workspace NetworkPolicy gate,
the COMPUTER_INPUT ask-by-default rule, and the reality gate's
dangerous/persistent classification all apply through the standard
descriptor contract.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

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
        status=(
            CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED
        ),
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
        "click": frozenset({EffectClass.COMPUTER_INPUT}),
    },
    resources=frozenset({ResourceClass.NETWORK}),
    origin=CapabilityOrigin.NATIVE,
    availability=_BROWSER_AVAILABILITY,
)


class BrowserCapability:
    """Structured browser automation through a pluggable driver."""

    descriptor = _BROWSER_DESCRIPTOR

    def __init__(self, driver_factory: Callable[[], BrowserDriver] | None = None) -> None:
        # The factory owns session reuse policy; the capability calls it per
        # invocation and closes what it opened.
        self._driver_factory = driver_factory

    @staticmethod
    def available() -> bool:
        return _BROWSER_AVAILABILITY is Availability.AVAILABLE

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
        driver: BrowserDriver | None = None
        try:
            driver = self._driver_factory()
            return await self._dispatch(request, driver, operation, args)
        except Exception as exc:  # noqa: BLE001 - driver failures are results
            return _result(request, ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            if driver is not None:
                try:
                    await driver.close()
                except Exception:  # noqa: BLE001 - close is best-effort
                    pass

    async def _dispatch(
        self,
        request: CapabilityRequest,
        driver: BrowserDriver,
        operation: str,
        args: dict[str, Any],
    ) -> CapabilityResult:
        if operation == "navigate":
            url = str(args.get("url") or "").strip()
            if not url:
                return _result(request, ok=False, error="navigate requires url")
            if not url.lower().startswith(("http://", "https://")):
                return _result(
                    request, ok=False, error="url must use http or https"
                )
            outcome = await driver.navigate(url)
            return _result(request, output=json.dumps(outcome, default=str))

        if operation == "snapshot":
            elements = await driver.snapshot()
            lines = [
                f"{e.role}\t{e.name}\t{e.selector}"
                + (f"\t{e.value}" if e.value else "")
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
]
