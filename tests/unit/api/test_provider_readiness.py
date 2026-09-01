from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from athena.api.app import create_app
from athena.models.registry import ProviderRegistry
from athena.protocol.errors import ModelProviderUnconfigured


@dataclass
class _FakeDB:
    async def fetch_one(self, query, *args):
        del query, args
        return {"1": 1}


class _FakeWorker:
    def health(self):
        return {}


class _FakeWorkerTask:
    def done(self):
        return False


class _FakeScheduler:
    def is_running(self):
        return True


@dataclass
class _NoProviderService:
    _started: bool = True
    _db: object = field(default_factory=_FakeDB)
    _worker: object = field(default_factory=_FakeWorker)
    _worker_task: object = field(default_factory=_FakeWorkerTask)
    _scheduler: object = field(default_factory=_FakeScheduler)
    _model_registry: ProviderRegistry = field(default_factory=ProviderRegistry)
    _recovery_status: str = "healthy"
    tasks_created: int = 0

    def require_agent_ready(self, request=None) -> None:
        del request
        if not self._model_registry.names():
            raise ModelProviderUnconfigured(
                "No model provider is configured.", provider_state="unconfigured"
            )

    async def submit(self, request, *, wait=False):
        del wait
        self.require_agent_ready(request)
        self.tasks_created += 1
        raise AssertionError("unreachable in no-provider readiness test")


@pytest.mark.asyncio
async def test_no_provider_is_live_but_not_ready_and_rejects_tasks() -> None:
    service = _NoProviderService(_model_registry=ProviderRegistry())
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/v1/live")
        health = await client.get("/v1/health")
        submit = await client.post("/v1/tasks", json={"prompt": "hello"})

    assert live.status_code == 200
    assert health.status_code == 503
    assert health.json()["checks"]["providers"] is False
    assert submit.status_code == 503
    assert submit.json()["code"] == "model_provider_unconfigured"
    assert service.tasks_created == 0
