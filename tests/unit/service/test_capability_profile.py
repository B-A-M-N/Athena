from types import SimpleNamespace

import pytest
import httpx

from athena.api.app import create_app
from athena.protocol.capabilities import Availability
from athena.protocol.errors import ServiceNotReady
from athena.protocol.tasks import AgentRequest
from athena.service.config import AthenaConfig
from athena.service.service import AthenaService


class _Registry:
    def __init__(self, available: set[str]) -> None:
        self._available = available

    def resolve(self, capability_id: str):
        if capability_id not in self._available:
            raise KeyError(capability_id)
        return SimpleNamespace(availability=Availability.AVAILABLE)


@pytest.mark.asyncio
async def test_required_capability_profile_checks_live_native_and_mcp_surfaces():
    service = AthenaService(config=AthenaConfig(required_capabilities=("browser", "mcp:tools")))
    service._registry = _Registry({"browser"})
    service._mcp_connection_status = {"tools": {"state": "connected"}}

    result = await service._validate_required_capabilities()

    assert result["status"] == "ok"
    assert result["missing"] == []
    assert result["resolved"] == ["browser", "mcp:tools"]


@pytest.mark.asyncio
async def test_required_capability_profile_is_explicitly_failed_when_surface_is_missing():
    service = AthenaService(config=AthenaConfig(required_capabilities=("browser", "mcp:tools")))
    service._registry = _Registry(set())

    result = await service._validate_required_capabilities()

    assert result["status"] == "failed"
    assert result["blocking"] is True
    assert {item["id"] for item in result["missing"]} == {"browser", "mcp:tools"}


@pytest.mark.asyncio
async def test_required_mcp_profile_drift_fails_live_health_and_admission():
    class _ReadyRouter:
        async def select(self, **_kwargs):
            return SimpleNamespace(provider="local")

    service = AthenaService(config=AthenaConfig(required_capabilities=("mcp:tools",)))
    service._registry = _Registry(set())
    service._model_registry = SimpleNamespace(
        readiness=lambda: {"state": "ready"},
        names=lambda: ["local"],
    )
    service._router = _ReadyRouter()
    service._mcp_connection_status = {"tools": {"state": "connected"}}
    service._started = True
    service._startup_health = {"status": "ok", "checks": {}, "blocking_failures": []}
    service._db = SimpleNamespace(fetch_one=lambda *_args: _db_ok())
    service._worker_task = SimpleNamespace(done=lambda: False)
    service._scheduler = SimpleNamespace(
        is_running=lambda: True,
        health=lambda: {"health": "healthy"},
    )

    await service.require_capability_profile_ready()
    service._mcp_connection_status["tools"] = {
        "state": "failed",
        "last_error": "transport exited",
    }

    health = service.runtime_health()
    assert health["capability_profile"]["status"] == "failed"
    assert health["capability_profile"]["missing"][0]["id"] == "mcp:tools"
    with pytest.raises(ServiceNotReady):
        await service.require_capability_profile_ready()

    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["capability_profile"] is False

    service._mcp_connection_status["tools"] = {"state": "connected"}
    await service.require_capability_profile_ready()
    await service.require_agent_ready(AgentRequest(prompt="reconnected"))


async def _db_ok():
    return {"1": 1}


@pytest.mark.asyncio
async def test_close_session_closes_session_scoped_browser_before_persisting_terminal_state():
    class Sessions:
        async def get(self, session_id):
            return {"id": session_id, "metadata": {}}

        async def close(self, session_id):
            assert session_id == "session-1"
            return True

    class Browser:
        def __init__(self):
            self.closed: list[str] = []

        async def close_session(self, session_id):
            self.closed.append(session_id)

    class Events:
        def __init__(self):
            self.events = []

        async def append_event(self, event_type, payload, *, session_id=None):
            self.events.append((event_type, payload, session_id))

    service = AthenaService(config=AthenaConfig())
    service._sessions = Sessions()
    service._browser = Browser()
    service._store_events = Events()

    assert await service.close_session("session-1") is True
    assert service._browser.closed == ["session-1"]
    assert service._store_events.events == [
        ("SessionClosed", {"session_id": "session-1"}, "session-1")
    ]
