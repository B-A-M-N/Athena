"""Direct OI-style execution must still use Athena's canonical capability path."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from athena.protocol.capabilities import CapabilityResult, CapabilityResultStatus
from athena.service.config import AthenaConfig
from athena.service.service import AthenaService


@dataclass
class _Sessions:
    created: list[str]

    async def get(self, session_id):
        return None

    async def create(self, session_id):
        self.created.append(session_id)
        return session_id


@dataclass
class _Messages:
    messages: list

    async def append_to_session(self, session_id, message):
        self.messages.append((session_id, message))


class _Dispatcher:
    def __init__(self):
        self.requests = []

    async def dispatch(self, request, *, workspace, profile):
        self.requests.append((request, workspace, profile))
        return CapabilityResult(
            call_id="call-direct",
            capability_id="execute",
            status=CapabilityResultStatus.OK,
            output="hello\n",
        )


@pytest.mark.asyncio
@pytest.mark.athena_scenario("FUSE-003")
async def test_direct_execution_routes_through_dispatcher_and_records_audit(tmp_path):
    service = AthenaService(config=AthenaConfig(db_path=":memory:", workspace_root=str(tmp_path)))
    dispatcher = _Dispatcher()
    sessions = _Sessions([])
    messages = _Messages([])
    service._dispatcher = dispatcher
    service._sessions = sessions
    service._store_messages = messages

    result = await service.execute_direct(
        "printf hello",
        session_id="session-1",
        inject_into_context=False,
    )

    assert result["status"] == "completed"
    assert result["stdout"] == "hello\n"
    assert len(dispatcher.requests) == 1
    request, workspace, _profile = dispatcher.requests[0]
    assert request.capability_id == "execute"
    assert request.task_id is None
    assert request.session_id == "session-1"
    assert request.arguments == {"language": "shell", "code": "printf hello"}
    assert workspace.root == str(tmp_path)

    assert sessions.created == ["session-1"]
    session_id, message = messages.messages[0]
    assert session_id == "session-1"
    assert message.metadata["direct_execution"] is True
    assert message.metadata["inject_into_context"] is False
