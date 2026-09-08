from types import SimpleNamespace

import pytest

from athena.protocol.capabilities import Availability
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
    service = AthenaService(
        config=AthenaConfig(required_capabilities=("browser", "mcp:tools"))
    )
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
