"""`/v1/health` scheduler check — public predicate, unchanged truth table.

The health handler previously reached into ``service._scheduler._task``
directly; it must now use :meth:`Scheduler.is_running`. These tests pin the
readiness truth table before/after the refactor:

  - scheduler present and running           -> scheduler check True
  - scheduler absent (service._scheduler None) -> scheduler check False
  - scheduler present but loop task done/never started -> False
  - scheduler object without the predicate (legacy duck-type) -> False
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")

from athena.api.app import create_app  # noqa: E402
from athena.scheduler.scheduler import Scheduler  # noqa: E402
from athena.state.database import Database  # noqa: E402
from athena.state.schedules import ScheduleStore  # noqa: E402


@dataclass
class _FakeDB:
    fail: bool = False

    async def fetch_one(self, query, *args):
        if self.fail:
            raise RuntimeError("db down")
        return {"1": 1}


@dataclass
class _FakeWorker:
    health_data: dict = field(default_factory=dict)

    def health(self):
        return self.health_data


@dataclass
class _FakeWorkerTask:
    done_flag: bool = False

    def done(self):
        return self.done_flag


@dataclass
class _Service:
    _started: bool = True
    _db: object = field(default_factory=_FakeDB)
    _worker: object = field(default_factory=_FakeWorker)
    _worker_task: object = field(default_factory=_FakeWorkerTask)
    _scheduler: object | None = None
    _model_registry: object | None = None

    def with_scheduler(self, scheduler):
        self._scheduler = scheduler
        return self


async def _health(service) -> tuple[int, dict]:
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/v1/health")
        return resp.status_code, resp.json()


def _ready_service() -> _Service:
    return _Service(_model_registry=SimpleNamespace())


async def test_health_ok_when_scheduler_running():
    async def _hold():  # a loop task that stays alive until cancelled
        await asyncio.Event().wait()

    db = Database(":memory:")
    await db._ensure_ready()
    store = ScheduleStore(db)
    scheduler = Scheduler(store, task_manager=SimpleNamespace())
    loop_task = asyncio.create_task(_hold())
    scheduler._task = loop_task
    service = _ready_service().with_scheduler(scheduler)
    service._db = db
    try:
        status, body = await _health(service)
        assert body["checks"] == {
            "service": True,
            "database": True,
            "worker": True,
            "scheduler": True,
            "providers": True,
            "worker_persistence": True,
        }, body
        assert status == 200
        assert body["status"] == "ok"
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await db.close()


async def test_health_reports_scheduler_false_when_absent():
    # Started service with NO scheduler: scheduler check False -> degraded 503.
    # This is the exact behavior the pre-refactor code had (getattr None-guard).
    service = _ready_service()
    status, body = await _health(service)
    assert status == 503
    assert body["status"] == "degraded"
    assert body["checks"]["scheduler"] is False


async def test_health_reports_scheduler_false_when_loop_done():
    db = Database(":memory:")
    await db._ensure_ready()
    scheduler = Scheduler(ScheduleStore(db), task_manager=SimpleNamespace())
    loop_task = asyncio.create_task(_noop())
    await loop_task  # completes immediately -> done
    scheduler._task = loop_task
    service = _ready_service().with_scheduler(scheduler)
    service._db = db
    status, body = await _health(service)
    assert status == 503
    assert body["checks"]["scheduler"] is False
    await db.close()


async def test_health_reports_scheduler_false_for_legacy_duck_type():
    # A scheduler look-alike without is_running() must not 500 the handler:
    # the check degrades to False (matches pre-refactor behavior where a
    # missing _task attribute also yielded False via getattr default).
    legacy = SimpleNamespace()  # no _task, no is_running
    service = _ready_service().with_scheduler(legacy)
    status, body = await _health(service)
    assert status == 503
    assert body["checks"]["scheduler"] is False


async def test_scheduler_is_running_truth_table():
    db = Database(":memory:")
    await db._ensure_ready()
    scheduler = Scheduler(ScheduleStore(db), task_manager=SimpleNamespace())
    assert scheduler.is_running() is False  # never started

    loop_task = asyncio.create_task(_hold())
    scheduler._task = loop_task
    assert scheduler.is_running() is True
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    assert scheduler.is_running() is False  # task done

    await scheduler.start()
    try:
        assert scheduler.is_running() is True
    finally:
        await scheduler.stop()
    assert scheduler.is_running() is False  # stopped
    await db.close()


async def _noop():
    return None


async def _hold():
    await asyncio.Event().wait()
