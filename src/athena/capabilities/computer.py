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
from io import BytesIO
import importlib
import json
import sys
import time
from typing import Any, Protocol

from athena.artifacts.store import ArtifactStore
from athena.execution.async_call import run_blocking
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
from athena.protocol.messages import Provenance, SourceType, TrustClass

_screen_backend: Any = None
try:  # optional dependency; the capability registers only when present
    _screen_backend = importlib.import_module("pyautogui")
except Exception:  # ImportError and headless-platform load errors alike
    pass

_COMPUTER_AVAILABILITY = (
    Availability.AVAILABLE
    if _screen_backend is not None and sys.platform.startswith("linux")
    else Availability.UNAVAILABLE
)

_COMPUTER_SEMANTICS = {
    "interaction_mode": "foreground",
    "focus_required": True,
    "window_targeting": False,
    "background_safe": False,
    "supported_platform": "linux",
}

# Bounded screen-observation budget: images are huge in context, so the
# descriptor promises a bounded artifact, not raw pixels in the transcript.
_MAX_OBSERVE_NOTE_CHARS = 512
_MAX_OBSERVE_BYTES = 4 * 1024 * 1024


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
    ref_uri: str | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        call_id=request.call_id,
        capability_id=request.capability_id,
        status=(CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED),
        output=output,
        error=None if ok else (error or "computer operation failed"),
        ref_uri=ref_uri,
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

    def __init__(
        self,
        backend: ScreenBackend | None = None,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._backend = backend
        self._artifacts = artifact_store
        self._health: dict[str, Any] = {
            "state": "unknown",
            "backend": type(backend).__name__ if backend is not None else "pyautogui",
        }

    @staticmethod
    def available() -> bool:
        return _COMPUTER_AVAILABILITY is Availability.AVAILABLE

    def health(self) -> dict[str, Any]:
        """Return the last non-blocking runtime health observation."""
        if self._backend is None and _screen_backend is None:
            return {
                **_COMPUTER_SEMANTICS,
                **self._health,
                "state": "unavailable",
                "reason": "no screen backend is importable",
            }
        return {**_COMPUTER_SEMANTICS, **self._health}

    async def probe_health(self) -> dict[str, Any]:
        """Probe whether the backend can address a display without mutation."""
        backend = self._backend or _screen_backend
        if backend is None:
            self._health = {
                "state": "unavailable",
                "backend": "none",
                "reason": "no screen backend is importable",
            }
            return {**_COMPUTER_SEMANTICS, **self._health}
        size = getattr(backend, "size", None)
        if size is None:
            self._health = {
                "state": "degraded",
                "backend": type(backend).__name__,
                "reason": "screen backend has no display-size probe",
            }
            return {**_COMPUTER_SEMANTICS, **self._health}
        try:
            dim = await run_blocking(size)
            width, height = int(dim.width), int(dim.height)
            if width <= 0 or height <= 0:
                raise ValueError(f"invalid display size {width}x{height}")
        except Exception as exc:  # noqa: BLE001 - health is a reported outcome
            self._health = {
                "state": "degraded",
                "backend": type(backend).__name__,
                "reason": f"display probe failed: {type(exc).__name__}: {exc}",
            }
            return {**_COMPUTER_SEMANTICS, **self._health}
        self._health = {
            "state": "ready",
            "backend": type(backend).__name__,
            "screen_size": [width, height],
        }
        return {**_COMPUTER_SEMANTICS, **self._health}

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
        if operation == "observe":
            return await self._observe(request, backend)

        if operation == "wait":
            seconds = min(float(args.get("seconds") or 1.0), 30.0)
            await asyncio.sleep(seconds)
            return _result(request, output=json.dumps({"waited_s": seconds}))

        if operation == "click":
            button = str(args.get("button") or "left")
            x, y = args.get("x"), args.get("y")
            await run_blocking(
                backend.click,
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
                button=button,
            )
            return _result(
                request,
                output=json.dumps({"clicked": True, "x": x, "y": y, "button": button}),
            )

        if operation == "type":
            text = str(args.get("text") or "")
            if not text:
                return _result(request, ok=False, error="type requires text")
            await run_blocking(backend.typewrite, text, interval=0.01)
            return _result(
                request,
                output=json.dumps({"typed_chars": len(text)}),
            )

        if operation == "key":
            keys = [str(k) for k in (args.get("keys") or []) if str(k)]
            if not keys:
                return _result(request, ok=False, error="key requires keys")
            await run_blocking(backend.hotkey, *keys)
            return _result(request, output=json.dumps({"pressed": keys}))

        if operation == "scroll":
            amount = int(args.get("amount") or 0)
            if amount == 0:
                return _result(request, ok=False, error="scroll requires a non-zero amount")
            x, y = args.get("x"), args.get("y")
            await run_blocking(
                backend.scroll,
                amount,
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
            )
            return _result(request, output=json.dumps({"scrolled": amount}))

        if operation == "move":
            x, y = args.get("x"), args.get("y")
            if x is None or y is None:
                return _result(request, ok=False, error="move requires x and y")
            await run_blocking(backend.moveTo, int(x), int(y), duration=0.1)
            return _result(request, output=json.dumps({"moved_to": [int(x), int(y)]}))

        return _result(request, ok=False, error=f"unknown computer operation: {operation!r}")

    async def _observe(
        self,
        request: CapabilityRequest,
        backend: ScreenBackend,
    ) -> CapabilityResult:
        started = time.monotonic()
        try:
            screenshot = await run_blocking(backend.screenshot)
        except Exception as exc:  # headless hosts cannot grab a framebuffer
            self._health = {
                "state": "degraded",
                "backend": type(backend).__name__,
                "reason": f"screenshot failed: {type(exc).__name__}: {exc}",
            }
            return _result(
                request,
                ok=False,
                error=f"screen observation unavailable on this host: {exc}",
            )
        try:
            image_bytes = await run_blocking(_encode_screenshot, screenshot)
        except Exception as exc:  # opaque backend handles are not visual evidence
            self._health = {
                "state": "degraded",
                "backend": type(backend).__name__,
                "reason": f"screenshot could not be encoded: {type(exc).__name__}: {exc}",
            }
            return _result(
                request,
                ok=False,
                error=f"screen observation captured no serializable image: {exc}",
            )
        size = getattr(backend, "size", None)
        screen_size = None
        if size is not None:
            try:
                dim = size()
                screen_size = [int(dim.width), int(dim.height)]
            except Exception:  # noqa: BLE001 - size probe is best-effort
                screen_size = None
        if self._artifacts is None:
            self._health = {
                "state": "degraded",
                "backend": type(backend).__name__,
                "reason": "visual artifact store is not configured",
            }
            return _result(
                request,
                ok=False,
                error="screen observation cannot be persisted: artifact store unavailable",
            )

        ref = await self._artifacts.save(
            task_id=request.task_id,
            content=image_bytes,
            mime_type="image/png",
            producer=Provenance(
                source_type=SourceType.CAPABILITY,
                source_id="computer.observe",
                trust=TrustClass.AGENT_CURATED,
            ),
            metadata={
                "kind": "screen_observation",
                "width": screen_size[0] if screen_size else None,
                "height": screen_size[1] if screen_size else None,
            },
        )
        self._health = {
            "state": "ready",
            "backend": type(backend).__name__,
            "screen_size": screen_size,
            "last_artifact_uri": ref.uri,
        }
        artifact_ref = {
            "id": ref.id,
            "uri": ref.uri,
            "hash": ref.hash,
            "mime_type": ref.mime_type,
            "size": ref.size,
            "storage_path": ref.storage_path,
            "producer": ref.producer,
            "task_id": ref.task_id,
            "metadata": dict(ref.metadata),
        }
        note = {
            "observed": True,
            "screen_size": screen_size,
            "artifact_uri": ref.uri,
            "artifact_sha256": ref.hash,
            "artifact_bytes": ref.size,
            "model_input": "image",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "note": (
                "screenshot captured as an immutable image artifact; the next "
                "model turn receives the visual input when its provider supports it"
            )[0:_MAX_OBSERVE_NOTE_CHARS],
        }
        return _result(
            request,
            output=json.dumps(note),
            ref_uri=ref.uri,
            meta={
                **note,
                "mime_type": ref.mime_type,
                "artifact_ref": artifact_ref,
            },
        )


def _encode_screenshot(screenshot: Any) -> bytes:
    """Serialize a backend screenshot without accepting opaque handles."""
    if isinstance(screenshot, bytes):
        if not screenshot:
            raise ValueError("screenshot returned empty bytes")
        data = screenshot
    else:
        save = getattr(screenshot, "save", None)
        if not callable(save):
            raise TypeError("backend returned an opaque screenshot handle")
        buffer = BytesIO()
        save(buffer, format="PNG")
        data = buffer.getvalue()
        if not data:
            raise ValueError("screenshot encoder returned empty bytes")
    if len(data) > _MAX_OBSERVE_BYTES:
        raise ValueError(
            f"screenshot is too large to retain safely ({len(data)} bytes; "
            f"limit {_MAX_OBSERVE_BYTES})"
        )
    return data


__all__ = ["ComputerCapability"]
