"""Startup resource-unwind contract."""

from __future__ import annotations

import pytest

from athena.service.service import AthenaService


@pytest.mark.asyncio
async def test_start_unwinds_when_a_later_start_stage_fails():
    service = AthenaService.in_memory()
    stopped = False

    async def fail_start_impl():
        raise RuntimeError("synthetic startup failure")

    async def fake_stop():
        nonlocal stopped
        stopped = True

    service._start_impl = fail_start_impl
    service.stop = fake_stop

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        await service.start()
    assert stopped is True
