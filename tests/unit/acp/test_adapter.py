from __future__ import annotations

import pytest

from athena.acp.adapter import ACPAdapter, ACPRequest
from athena.protocol.errors import ModelProviderUnconfigured


class _TaskManager:
    def __init__(self) -> None:
        self.created = 0

    async def create(self, spec) -> None:
        del spec
        self.created += 1

    async def enqueue(self, task_id) -> None:
        del task_id


class _Sessions:
    def __init__(self) -> None:
        self.created = 0

    async def create(self, session_id, *, parent_id=None, metadata=None) -> None:
        del session_id, parent_id, metadata
        self.created += 1


@pytest.mark.asyncio
async def test_acp_admission_rejects_before_session_or_task_creation() -> None:
    tasks = _TaskManager()
    sessions = _Sessions()

    def reject(request) -> None:
        del request
        raise ModelProviderUnconfigured("No model provider is configured.")

    adapter = ACPAdapter(tasks, sessions, admission=reject)
    with pytest.raises(ModelProviderUnconfigured) as exc_info:
        await adapter.submit(ACPRequest(objective="hello"))

    assert exc_info.value.code == "model_provider_unconfigured"
    assert sessions.created == 0
    assert tasks.created == 0
