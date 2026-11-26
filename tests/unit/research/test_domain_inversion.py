"""Research domain authority is run_command; capability execute is an adapter."""

from __future__ import annotations


import pytest

from athena.capabilities.research import ResearchCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.research.commands import ResearchCommand
from athena.research.service import ResearchService
from athena.research.store import ResearchStore
from athena.state.database import Database


class _Recorder(ResearchService):
    def __init__(self, store):
        super().__init__(store)
        self.commands = []

    async def run_command(self, command):
        self.commands.append(command)
        return await super().run_command(command)


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database._ensure_ready()
    yield database
    await database.close()


async def test_capability_request_translates_through_domain_command(db):
    store = ResearchStore(db)
    service = _Recorder(store)
    capability = ResearchCapability.__new__(ResearchCapability)
    capability._service = service
    request = CapabilityRequest(
        capability_id="research",
        arguments={"operation": "sources"},
        task_id="task-domain",
        session_id="session-domain",
        call_id="call-domain",
    )

    result = await capability.invoke(request)

    assert result.status is CapabilityResultStatus.OK
    assert service.commands
    command = service.commands[0]
    assert command.operation == "sources"
    assert command.task_id == "task-domain"
    assert command.session_id == "session-domain"
    assert command.call_id == "call-domain"


async def test_domain_command_failure_translates_to_capability_failure(db):
    service = ResearchService(ResearchStore(db))
    command = ResearchCommand(
        operation="fetch",
        arguments={"uri": "https://not-in-policy.example.test/source"},
        task_id="task-domain",
    )
    result = await service.run_command(command)
    assert result.ok is False
    capability = ResearchCapability.__new__(ResearchCapability)
    capability._service = service
    capability_result = await capability.invoke(
        CapabilityRequest(
            capability_id="research",
            arguments={"operation": "fetch", "uri": "https://not-in-policy.example.test/source"},
            task_id="task-domain",
            call_id="call-domain",
        )
    )
    assert capability_result.status is CapabilityResultStatus.FAILED
    assert capability_result.error
