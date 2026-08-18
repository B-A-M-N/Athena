"""Shared fixtures for cross-subsystem integration tests.

Each test gets a freshly wired, isolated :class:`AthenaService` (in-memory DB,
temp workspace, fake model). Usage:

    svc = await make(scripts=[...])
    try:
        ... run the test ...
    finally:
        await svc.stop()
"""
from __future__ import annotations

import pytest

from athena.service.service import AthenaService


@pytest.fixture
async def make_service():
    """Return an async factory ``make(scripts=None) -> started AthenaService``.

    The factory records every service it starts so the fixture can guarantee
    :meth:`AthenaService.stop` is called at teardown even if a test fails mid-way.
    """
    started: list[AthenaService] = []

    async def make(scripts=None) -> AthenaService:
        svc = AthenaService.in_memory(extra_scripts=scripts)
        await svc.start()
        started.append(svc)
        return svc

    yield make

    for svc in started:
        try:
            if svc._started or svc._db is not None:
                await svc.stop()
        except Exception:
            pass