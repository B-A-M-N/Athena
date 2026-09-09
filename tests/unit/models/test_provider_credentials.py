import asyncio
from datetime import datetime, timezone

import pytest

from athena.models.credentials import ProviderCredentialPool
from athena.protocol.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderUnavailable,
)


class _Provider:
    def __init__(self, *, delay: float = 0.002, error: Exception | None = None):
        self.delay = delay
        self.error = error
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def list_models(self):
        return []

    async def complete(self, _request):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            yield object()
        finally:
            self.active -= 1


async def _consume(pool, request):
    return [event async for event in pool.complete(request)]


@pytest.mark.asyncio
async def test_pool_uses_real_leases_without_duplicate_concurrency():
    providers = [_Provider(), _Provider()]
    pool = ProviderCredentialPool(
        "test",
        [("key-a", lambda: providers[0]), ("key-b", lambda: providers[1])],
    )

    await asyncio.gather(*(_consume(pool, object()) for _ in range(20)))

    assert sum(provider.calls for provider in providers) == 20
    assert max(provider.max_active for provider in providers) <= 1
    assert all(slot.active_requests == 0 for slot in pool._slots)  # noqa: SLF001


@pytest.mark.asyncio
async def test_auth_quarantine_has_no_cooldown_fallback():
    provider = _Provider(error=ProviderAuthenticationError("bad key"))
    pool = ProviderCredentialPool("test", [("key-a", lambda: provider)])

    with pytest.raises(ProviderAuthenticationError):
        await _consume(pool, object())
    with pytest.raises(ProviderUnavailable) as caught:
        await _consume(pool, object())
    assert caught.value.data["credential_pool"] == "test"
    assert await pool.list_models() == []


def test_retry_after_controls_rate_limit_cooldown():
    pool = ProviderCredentialPool("test", [("key-a", lambda: _Provider())])
    slot = pool._slots[0]  # noqa: SLF001
    pool._mark_failure(slot, ProviderRateLimitError("slow down", retry_after=17.0))  # noqa: SLF001
    assert slot.retry_at is not None
    remaining = (slot.retry_at - datetime.now(timezone.utc)).total_seconds()
    assert 15.0 <= remaining <= 17.5
