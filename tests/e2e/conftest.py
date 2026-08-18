"""Shared fixtures for E2E tests that use the REAL shell/python runtimes.

Every test drives a fully-wired :class:`AthenaService` (in-memory DB, temp
workspace, fake model that emits ``execute`` / ``fs`` capability calls). The
fake provider only *decides* what to call; the actual code runs on the real
``ShellRuntime`` / ``PythonRuntime`` subprocesses via ``ExecutionManager``.
"""
from __future__ import annotations

import pytest

from athena.service.service import AthenaService


@pytest.fixture
async def make_service():
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