"""Shared root-level fixtures for tests that need a durable (file-backed) DB.

A file-backed :class:`AthenaService` is required wherever state must survive a
service restart/stop (session resume, crash recovery, scheduler recovery),
because :meth:`AthenaService.in_memory` hard-codes ``db_path=":memory:"``.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from athena.service.service import AthenaService
from athena.service.config import AthenaConfig, ProviderConfig


@pytest.fixture
def durable_db_path():
    """Yield a throwaway .db file path (not :memory:) with cleanup."""
    tmp = tempfile.mkdtemp(prefix="athena-db-")
    path = os.path.join(tmp, "athena.db")
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.unlink(path + "-wal")
    except OSError:
        pass
    try:
        os.unlink(path + "-shm")
    except OSError:
        pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass


@pytest.fixture
async def make_durable_service():
    """Async factory building a started service against a given db path.

    ``make(db_path, scripts=None, **cfg) -> AthenaService``. The service is
    registered for teardown so ``stop()`` is always called.
    """
    started: list[AthenaService] = []

    async def make(db_path, scripts=None, **cfg):
        workspace = cfg.pop("workspace_root", None) or tempfile.mkdtemp(prefix="athena-ws-")
        config = AthenaConfig(
            db_path=db_path,
            workspace_root=workspace,
            artifact_root=os.path.join(workspace, "artifacts"),
            providers=(
                ProviderConfig(
                    kind="fake", name="fake",
                    extra={"scripts": list(scripts or ())},
                ),
            ),
            worker_max_parallel=cfg.get("worker_max_parallel", 4),
            scheduler_interval_seconds=cfg.get("scheduler_interval_seconds", 1.0),
        )
        svc = AthenaService(config=config)
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