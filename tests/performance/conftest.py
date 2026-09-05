"""Fixtures for the performance lane (mirrors tests/integration/conftest)."""

from __future__ import annotations

import os
import tempfile

import pytest

from athena.service.config import AthenaConfig, ProviderConfig
from athena.service.service import AthenaService


@pytest.fixture
async def make_service():
    """Return an async factory ``make(scripts=None, files=None) -> AthenaService``.

    ``files`` maps workspace-relative paths to text content, seeded into the
    temp workspace before start — the lane's read scenarios need durable
    workspace state the scripted model cannot create itself.
    """
    started: list[AthenaService] = []

    async def make(scripts=None, files=None) -> AthenaService:
        tmp = tempfile.mkdtemp(prefix="athena-ws-perf-")
        for rel, content in (files or {}).items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        svc = AthenaService(
            config=AthenaConfig(
                db_path=":memory:",
                workspace_root=tmp,
                artifact_root=os.path.join(tmp, "artifacts"),
                providers=(
                    ProviderConfig(
                        kind="fake", name="fake", extra={"scripts": list(scripts or [])}
                    ),
                ),
            )
        )
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
