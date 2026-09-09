"""Credential-slot pools for one logical model provider."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from athena.protocol.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeout,
    ProviderUnavailable,
)


@dataclass
class CredentialSlot:
    label: str
    provider: Any
    failures: int = 0
    state: str = "healthy"
    retry_at: datetime | None = None
    last_error: str | None = None
    last_success: datetime | None = None
    max_concurrency: int = 1
    active_requests: int = 0


class ProviderCredentialPool:
    """Keep credential rotation behind one stable provider/model identity."""

    _RETRYABLE = (
        ProviderAuthenticationError,
        ProviderRateLimitError,
        ProviderTimeout,
        ProviderUnavailable,
    )

    def __init__(
        self,
        name: str,
        factories: list[tuple[str, Callable[[], Any]]],
        *,
        max_concurrency: int = 1,
    ) -> None:
        if not factories:
            raise ValueError("credential pool requires at least one slot")
        self.name = str(name)
        limit = max(1, int(max_concurrency))
        self._slots = [
            CredentialSlot(label=str(label), provider=factory(), max_concurrency=limit)
            for label, factory in factories
        ]
        self._lock = asyncio.Lock()
        self._availability = asyncio.Condition(self._lock)
        self._cursor = 0
        self._active: dict[str, CredentialSlot] = {}

    async def list_models(self):
        for _ in range(len(self._slots)):
            try:
                slot, lease = await self._acquire()
            except ProviderUnavailable:
                return []
            try:
                models = await slot.provider.list_models()
                return [self._with_provider(info) for info in models]
            except Exception as exc:  # readiness reports the pool, not a crash
                self._mark_failure(slot, exc)
            finally:
                await self._release(slot, lease)
        return []

    def readiness(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        slots = []
        for index, slot in enumerate(self._slots):
            if slot.retry_at is not None and slot.retry_at <= now and slot.state != "auth_failed":
                slot.state = "healthy"
                slot.retry_at = None
            slots.append(
                {
                    "slot": f"slot-{index + 1}",
                    "credential": slot.label,
                    "state": slot.state,
                    "failures": slot.failures,
                    "retry_at": slot.retry_at.isoformat() if slot.retry_at else None,
                    "last_error": slot.last_error,
                    "last_success": slot.last_success.isoformat() if slot.last_success else None,
                    "active_requests": slot.active_requests,
                    "max_concurrency": slot.max_concurrency,
                }
            )
        state = "ready" if any(item["state"] == "healthy" for item in slots) else "degraded"
        return {"state": state, "credential_slots": slots}

    async def complete(self, request):
        last: Exception | None = None
        for _ in range(len(self._slots)):
            slot, lease = await self._acquire()
            response = []
            try:
                async for event in slot.provider.complete(request):
                    response.append(event)
                    yield event
                self._mark_success(slot)
                return
            except self._RETRYABLE as exc:
                last = exc
                self._mark_failure(slot, exc)
                # A partially streamed response cannot be replayed safely.
                if response:
                    raise
            finally:
                await self._release(slot, lease)
        if last is not None:
            raise last
        raise ProviderUnavailable(f"provider credential pool {self.name!r} is exhausted")

    async def cancel(self, request_id: str) -> None:
        for slot in self._slots:
            cancel = getattr(slot.provider, "cancel", None)
            if callable(cancel):
                await cancel(request_id)

    async def transcribe_audio(self, data: bytes, **kwargs: Any) -> Any:
        return await self._audio_call("transcribe_audio", data, **kwargs)

    async def synthesize_audio(self, text: str, **kwargs: Any) -> Any:
        return await self._audio_call("synthesize_audio", text, **kwargs)

    async def _audio_call(self, method_name: str, value: Any, **kwargs: Any) -> Any:
        last: Exception | None = None
        for _ in range(len(self._slots)):
            slot, lease = await self._acquire()
            method = getattr(slot.provider, method_name, None)
            if not callable(method):
                await self._release(slot, lease)
                continue
            try:
                result = await method(value, **kwargs)
                self._mark_success(slot)
                return result
            except self._RETRYABLE as exc:
                last = exc
                self._mark_failure(slot, exc)
            finally:
                await self._release(slot, lease)
        if last is not None:
            raise last
        raise ProviderUnavailable(f"provider credential pool {self.name!r} lacks {method_name}")

    def _ordered(self) -> list[CredentialSlot]:
        now = datetime.now(timezone.utc)
        eligible = [
            slot
            for slot in self._slots
            if slot.state != "auth_failed" and (slot.retry_at is None or slot.retry_at <= now)
        ]
        if not eligible:
            return []
        start = self._cursor % len(eligible)
        self._cursor += 1
        return eligible[start:] + eligible[:start]

    async def _acquire(self) -> tuple[CredentialSlot, str]:
        """Lease one eligible slot without ever bypassing cooldown/quarantine."""
        async with self._availability:
            while True:
                eligible = [
                    slot for slot in self._ordered() if slot.active_requests < slot.max_concurrency
                ]
                if eligible:
                    slot = eligible[0]
                    slot.active_requests += 1
                    lease = f"{slot.label}:{uuid.uuid4().hex}"
                    self._active[lease] = slot
                    return slot, lease
                # Active healthy slots are busy, not failed: wait for a lease
                # rather than issuing an over-concurrency request. If every
                # slot is cooling down or quarantined, fail immediately.
                if any(slot.active_requests > 0 for slot in self._slots) and any(
                    (
                        slot.state != "auth_failed"
                        and (slot.retry_at is None or slot.retry_at <= datetime.now(timezone.utc))
                    )
                    for slot in self._slots
                ):
                    await self._availability.wait()
                    continue
                raise self._pool_exhausted()

    async def _release(self, slot: CredentialSlot, lease: str) -> None:
        async with self._availability:
            if self._active.pop(lease, None) is not None:
                slot.active_requests = max(0, slot.active_requests - 1)
            self._availability.notify_all()

    def _pool_exhausted(self) -> ProviderUnavailable:
        now = datetime.now(timezone.utc)
        retry_times = [
            slot.retry_at
            for slot in self._slots
            if slot.state != "auth_failed" and slot.retry_at is not None and slot.retry_at > now
        ]
        retry_after = min(retry_times) if retry_times else None
        seconds = max(0.0, (retry_after - now).total_seconds()) if retry_after else None
        reason = "all credential slots are quarantined or cooling down"
        return ProviderUnavailable(
            f"provider credential pool {self.name!r} exhausted: {reason}",
            retry_after_seconds=seconds,
            credential_pool=self.name,
        )

    def _mark_failure(self, slot: CredentialSlot, error: Exception) -> None:
        slot.failures += 1
        slot.last_error = f"{type(error).__name__}: {error}"
        if isinstance(error, ProviderAuthenticationError):
            slot.state = "auth_failed"
            delay = 300.0
        elif isinstance(error, ProviderRateLimitError):
            slot.state = "rate_limited"
            retry_after = getattr(error, "retry_after", None)
            delay = (
                float(retry_after)
                if retry_after is not None
                else min(300.0, 2.0 ** min(slot.failures, 8))
            )
        else:
            slot.state = "degraded"
            delay = min(60.0, 2.0 ** min(slot.failures, 6))
        slot.retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

    def _mark_success(self, slot: CredentialSlot) -> None:
        slot.failures = 0
        slot.state = "healthy"
        slot.retry_at = None
        slot.last_error = None
        slot.last_success = datetime.now(timezone.utc)

    def reset_authentication(self, label: str | None = None) -> int:
        """Operator action to release permanent auth-failure quarantine."""
        changed = 0
        for slot in self._slots:
            if slot.state == "auth_failed" and (label is None or slot.label == str(label)):
                slot.state = "healthy"
                slot.retry_at = None
                slot.last_error = None
                changed += 1
        return changed

    def _with_provider(self, info: Any) -> Any:
        try:
            from dataclasses import replace

            return replace(info, provider=self.name)
        except (TypeError, ValueError):
            return info


__all__ = ["CredentialSlot", "ProviderCredentialPool"]
