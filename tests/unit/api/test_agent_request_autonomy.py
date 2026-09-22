"""Cross-interface autonomy resolution contract."""

from __future__ import annotations

from athena.api.app import build_agent_request
from athena.protocol.tasks import AgentRequest, AutonomyLevel
from athena.service.service import AthenaService
from athena.service.task_intake import TaskIntake


def test_http_omitted_autonomy_stays_unset_for_service_resolution():
    request = build_agent_request({"prompt": "inspect the release"})
    assert request.autonomy is None


def test_http_explicit_autonomy_is_preserved():
    request = build_agent_request({"prompt": "inspect the release", "autonomy": "coding"})
    assert request.autonomy is AutonomyLevel.CODING


def test_service_resolves_omitted_autonomy_from_configuration():
    full = AthenaService.in_memory()
    config = full.config
    request = AgentRequest(prompt="inspect the release")
    service = AthenaService.__new__(AthenaService)
    service.config = config
    service._default_workspace = full._default_workspace
    service._task_intake = TaskIntake(service)
    spec = service._build_task_spec(request, "session-autonomy")
    assert spec.metadata["autonomy"] == config.autonomy_level.value


def test_request_default_is_omitted_not_transport_supervised():
    assert AgentRequest(prompt="x").autonomy is None
