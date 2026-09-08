"""Hermes supervision must remain scoped to self-host operations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from athena.acp.adapter import ACPAdapter, ACPRequest
from athena.protocol.ids import new_id
from athena.protocol.tasks import AgentRequest, AutonomyLevel, TaskStatus
from athena.scheduler.scheduler import TaskTemplate
from athena.service.config import AthenaConfig, HermesRefereeConfig, ProviderConfig
from athena.service.service import AthenaService


async def _wait_for_status(service: AthenaService, task_id: str, expected: str) -> None:
    for _ in range(200):
        if await service.get_task_status(task_id) == expected:
            return
        await asyncio.sleep(0.02)
    assert await service.get_task_status(task_id) == expected


@pytest.mark.asyncio
async def test_required_hermes_only_blocks_self_host_operations(tmp_path):
    config = AthenaConfig(
        db_path=":memory:",
        workspace_root=str(tmp_path),
        artifact_root=str(tmp_path / "artifacts"),
        providers=(
            ProviderConfig(
                kind="fake",
                name="fake",
                extra={
                    "scripts": [
                        {
                            "match": {"user_contains": "ORDINARY_SCOPE"},
                            "respond": {"text": "ordinary", "done": True},
                        },
                        {
                            "match": {"user_contains": "SCHEDULE_SCOPE"},
                            "respond": {"text": "scheduled", "done": True},
                        },
                        {
                            "match": {"user_contains": "ACP_SCOPE"},
                            "respond": {"text": "acp", "done": True},
                        },
                        {
                            "match": {"capability_result_ok": True},
                            "respond": {"text": "parent done", "done": True},
                        },
                        {
                            "match": {"user_contains": "DELEGATE_SCOPE_PARENT"},
                            "respond": {
                                "capability_call": {
                                    "capability_id": "delegate",
                                    "arguments": {
                                        "operation": "spawn",
                                        "objective": "DELEGATE_SCOPE_CHILD",
                                    },
                                }
                            },
                        },
                        {
                            "match": {"user_contains": "DELEGATE_SCOPE_CHILD"},
                            "respond": {"text": "child", "done": True},
                        },
                    ]
                },
            ),
        ),
        hermes_referee=HermesRefereeConfig(
            enabled=True,
            endpoint="http://127.0.0.1:1",
            self_host_supervision="required",
        ),
    )
    service = AthenaService(config=config)
    await service.start()
    try:
        # Startup records the unavailable optional transport, but ordinary
        # provider readiness remains independent of it.
        await service.require_agent_ready()

        ordinary = await service.submit(
            AgentRequest(prompt="ORDINARY_SCOPE"),
            wait=True,
        )
        assert await service.get_task_status(ordinary.id) == TaskStatus.COMPLETE.value

        await service._scheduler.stop()  # noqa: SLF001 - isolate one deterministic tick
        due = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        await service._store_schedules.upsert_job(  # noqa: SLF001
            "hermes-scope-schedule",
            "Hermes scope schedule",
            payload={
                "template": TaskTemplate(
                    objective="SCHEDULE_SCOPE",
                    session_id=new_id("session"),
                ).__dict__
            },
            trigger_spec={"type": "once", "at": due},
            enabled=True,
            next_run=due,
        )
        assert await service._scheduler.tick() == 1  # noqa: SLF001
        scheduled_run = await service._store_schedules.last_run("hermes-scope-schedule")  # noqa: SLF001
        assert scheduled_run and scheduled_run["task_id"]
        await _wait_for_status(service, scheduled_run["task_id"], TaskStatus.COMPLETE.value)

        acp = ACPAdapter(
            service._task_manager,  # noqa: SLF001
            service._sessions,  # noqa: SLF001
            event_store=service._store_events,  # noqa: SLF001
            admission=service.require_task_ready,
        )
        acp_task_id = new_id("acp-scope")
        accepted = await acp.submit(ACPRequest(objective="ACP_SCOPE", task_id=acp_task_id))
        assert accepted.task_id == acp_task_id
        await _wait_for_status(service, acp_task_id, TaskStatus.COMPLETE.value)

        parent = await service.submit(
            AgentRequest(
                prompt="DELEGATE_SCOPE_PARENT",
                session_id=new_id("session"),
                autonomy=AutonomyLevel.AUTONOMOUS,
            ),
            wait=False,
        )
        for _ in range(200):
            if await service.get_task_status(parent.id) == TaskStatus.WAITING_APPROVAL.value:
                break
            await asyncio.sleep(0.02)
        approval_id = await service.pending_approval_id(parent.id)
        assert approval_id is not None
        await service.approve(approval_id, granted=True)
        await _wait_for_status(service, parent.id, TaskStatus.COMPLETE.value)
        child_rows = await service._db.fetch_all(  # noqa: SLF001
            "SELECT id FROM tasks WHERE parent_task_id = ?", (parent.id,)
        )
        assert len(child_rows) == 1
        await _wait_for_status(service, child_rows[0]["id"], TaskStatus.COMPLETE.value)

        with pytest.raises(RuntimeError, match="Self-hosting refused"):
            await service.submit_self_host("SELF_HOST_SCOPE", workspace_root=str(tmp_path))
    finally:
        await service.stop()
