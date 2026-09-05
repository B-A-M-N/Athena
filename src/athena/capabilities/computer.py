"""Computer control capability (P1-20, SPEC §36).

Visual computer interaction as fallback/general-purpose infrastructure:
observe the screen, click, type, press keys, scroll, move the pointer,
wait. Higher-level browser interaction should prefer structured browser
APIs (see ``athena.capabilities.browser``, P1-28); this capability is the
general-purpose surface that still works where structure is unavailable.

The capability is OPTIONAL: it registers only when a screen backend
(``pyautogui``) is importable, and ``computer.observe`` further degrades
gracefully when the framebuffer grab is unsupported on the host.

Governance is pre-built and has been waiting for a provider:

* ``EffectClass.COMPUTER_INPUT`` policy rule fires at priority 95 — ASK
  in every autonomy profile.
* The dispatcher lists COMPUTER_INPUT in HIGH_RISK_EFFECTS: reusable
  grants degrade to CALL scope; the operator binds an explicit envelope.
* The reality gate classifies it dangerous, persistent, and
  environment-affecting: no speculative shadow claim, verification
  deferred to completion.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from typing import Any, Protocol

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

_screen_backend: Any = None
try:  # optional dependency; the capability registers only when present
    _screen_backend = importlib.import_module("pyautogui")
except Exception:  # ImportError and headless-platform load errors alike
    pass

_COMPUTER_AVAILABILITY = (
    Availability.AVAILABLE if _screen_backend is not None else Availability.UNAVAILABLE
)

# Bounded screen-observation budget: images are huge in context, so the
# descriptor promises a bounded artifact, not raw pixels in the transcript.
_MAX_OBSERVE_NOTE_CHARS = 512


class ScreenBackend(Protocol):
    """What the capability needs from a screen driver (pyautogui-shaped)."""

    def screenshot(self) -> Any: ...

    def click(self, x: int | None = None, y: int | None = None, **kw) -> Any: ...

    def typewrite(self, text: str, interval: float = 0.0) -> Any: ...

    def hotkey(self, *names: str) -> Any: ...

    def scroll(self, amount: int, x: int | None = None, y: int | None = None) -> Any: ...

    def moveTo(self, x: int, y: int, duration: float = 0.0) -> Any: ...

    def position(self) -> Any: ...

    def size(self) -> Any: ...


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
        error=None if ok else (error or "computer operation failed"),
        metadata=dict(meta or {}),
    )


def _operation_effects(operation: str) -> frozenset[EffectClass]:
    if operation == "observe":
        return frozenset()  # pure read of the screen
    return frozenset({EffectClass.COMPUTER_INPUT})


_COMPUTER_DESCRIPTOR = CapabilityDescriptor(
    id="computer",
    description=(
        "Visual computer control (fallback/general-purpose infrastructure per "
        "SPEC 36): observe the screen, click, type, press keys, scroll, move "
        "the pointer, wait. Structured browser APIs (browser capability) are "
        "preferred when available. Every mutating operation is COMPUTER_INPUT "
        "governed: ask-by-default, call-scoped approvals."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "enum": [
                    "observe",
                    "click",
                    "type",
                    "key",
                    "scroll",
                    "move",
                    "wait",
                ],
            },
            "x": {"type": "integer", "minimum": 0},
            "y": {"type": "integer", "minimum": 0},
            "text": {"type": "string", "maxLength": 4096},
            "keys": {"type": "array", "items": {"type": "string", "maxLength": 32}, "maxItems": 6},
            "amount": {"type": "integer"},
            "seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
            "button": {"enum": ["left", "right", "middle"]},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
    effects=frozenset({EffectClass.COMPUTER_INPUT}),
    operation_effects={
        "observe": frozenset(),
        "click": frozenset({EffectClass.COMPUTER_INPUT}),
        "type": frozenset({EffectClass.COMPUTER_INPUT}),
        "key": frozenset({EffectClass.COMPUTER_INPUT}),
        "scroll": frozenset({EffectClass.COMPUTER_INPUT}),
        "move": frozenset({EffectClass.COMPUTER_INPUT}),
        "wait": frozenset(),
    },
    resources=frozenset({ResourceClass.PROCESS}),
    origin=CapabilityOrigin.NATIVE,
    availability=_COMPUTER_AVAILABILITY,
)


class ComputerCapability:
    """Screen observation and input through a pluggable backend."""

    descriptor = _COMPUTER_DESCRIPTOR

    def __init__(self, backend: ScreenBackend | None = None) -> None:
        self._backend = backend

    @staticmethod
    def available() -> bool:
        return _COMPUTER_AVAILABILITY is Availability.AVAILABLE

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        backend = self._backend or _screen_backend
        if backend is None:
            return _result(
                request,
                ok=False,
                error="computer control unavailable: install the 'pyautogui' extra",
            )
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        try:
            return await self._dispatch(request, backend, operation, args)
        except Exception as exc:  # noqa: BLE001 - backend failures are results
            return _result(request, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _dispatch(
        self,
        request: CapabilityRequest,
        backend: ScreenBackend,
        operation: str,
        args: dict[str, Any],
    ) -> CapabilityResult:
        loop = asyncio.get_running_loop()

        if operation == "observe":
            return await self._observe(request, backend, loop)

        if operation == "wait":
            seconds = min(float(args.get("seconds") or 1.0), 30.0)
            await asyncio.sleep(seconds)
            return _result(request, output=json.dumps({"waited_s": seconds}))

        if operation == "click":
            button = str(args.get("button") or "left")
            x, y = args.get("x"), args.get("y")
            await loop.run_in_executor(
                None,
                lambda: backend.click(
                    x=int(x) if x is not None else None,
                    y=int(y) if y is not None else None,
                    button=button,
                ),
            )
            return _result(
                request,
                output=json.dumps({"clicked": True, "x": x, "y": y, "button": button}),
            )

        if operation == "type":
            text = str(args.get("text") or "")
            if not text:
                return _result(request, ok=False, error="type requires text")
            await loop.run_in_executor(None, lambda: backend.typewrite(text, interval=0.01))
            return _result(
                request,
                output=json.dumps({"typed_chars": len(text)}),
            )

        if operation == "key":
            keys = [str(k) for k in (args.get("keys") or []) if str(k)]
            if not keys:
                return _result(request, ok=False, error="key requires keys")
            await loop.run_in_executor(None, lambda: backend.hotkey(*keys))
            return _result(request, output=json.dumps({"pressed": keys}))

        if operation == "scroll":
            amount = int(args.get("amount") or 0)
            if amount == 0:
                return _result(request, ok=False, error="scroll requires a non-zero amount")
            x, y = args.get("x"), args.get("y")
            await loop.run_in_executor(
                None,
                lambda: backend.scroll(
                    amount,
                    x=int(x) if x is not None else None,
                    y=int(y) if y is not None else None,
                ),
            )
            return _result(request, output=json.dumps({"scrolled": amount}))

        if operation == "move":
            x, y = args.get("x"), args.get("y")
            if x is None or y is None:
                return _result(request, ok=False, error="move requires x and y")
            await loop.run_in_executor(None, lambda: backend.moveTo(int(x), int(y), duration=0.1))
            return _result(request, output=json.dumps({"moved_to": [int(x), int(y)]}))

        return _result(request, ok=False, error=f"unknown computer operation: {operation!r}")

    async def _observe(
        self,
        request: CapabilityRequest,
        backend: ScreenBackend,
        loop: asyncio.AbstractEventLoop,
    ) -> CapabilityResult:
        started = time.monotonic()
        try:
            await loop.run_in_executor(None, backend.screenshot)
        except Exception as exc:  # headless hosts cannot grab a framebuffer
            return _result(
                request,
                ok=False,
                error=f"screen observation unavailable on this host: {exc}",
            )
        size = getattr(backend, "size", None)
        screen_size = None
        if size is not None:
            try:
                dim = size()
                screen_size = [int(dim.width), int(dim.height)]
            except Exception:  # noqa: BLE001 - size probe is best-effort
                screen_size = None
        note = {
            "observed": True,
            "screen_size": screen_size,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "note": (
                "screenshot captured; persist it via the artifacts capability to "
                "inspect it visually (raw image data is not inlined in context)"
            )[0:_MAX_OBSERVE_NOTE_CHARS],
        }
        return _result(request, output=json.dumps(note))


__all__ = ["ComputerCapability"]
