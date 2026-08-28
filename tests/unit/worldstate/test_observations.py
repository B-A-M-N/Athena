from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.protocol.events import make_event
from athena.protocol.tasks import WorkspaceSpec
from athena.service.service import AthenaService
from athena.worldstate import ClaimRegistry
from athena.worldstate import TaskWorldState


@pytest.mark.asyncio
async def test_task_world_state_retains_scheduler_trigger_observation():
    triggering = make_event(
        "TaskCreated",
        {
            "status": "CREATED",
            "trigger_event": {
                "id": "watch-event-1",
                "type": "WatchObserved",
                "payload": {
                    "watch": "watch-1",
                    "kind": "files",
                    "changes": ["config.toml"],
                },
            },
        },
        task_id="maintenance-task",
    )
    service = SimpleNamespace(
        _world_state_store=None,
        _store_events=SimpleNamespace(
            list_for_task=lambda _task_id: _events(triggering),
        ),
        _store_runtime_sessions=None,
        _store_mutations=None,
    )

    snapshot = await TaskWorldState(service=service, task_id="maintenance-task").snapshot()

    assert snapshot["observations"] == [
        {
            "event_id": "watch-event-1",
            "type": "WatchObserved",
            "payload": {
                "watch": "watch-1",
                "kind": "files",
                "changes": ["config.toml"],
            },
            "received_at": triggering.timestamp.isoformat(),
        }
    ]


async def _events(event):
    return [event]


@pytest.mark.asyncio
async def test_watch_change_invalidates_overlapping_claims():
    claims = ClaimRegistry()
    claim = claims.record(
        text="configuration is valid",
        evidence={"execution_id": "exec-1"},
        task_id="maintenance-task",
        depends_on_paths=("config/",),
    )
    service = SimpleNamespace(
        _default_workspace=WorkspaceSpec(id="repo", root="/workspace"),
        _world_states={"maintenance-task": SimpleNamespace(claims=claims)},
        _world_state_store=None,
    )

    await AthenaService._invalidate_watch_claims(
        service,
        {
            "kind": "files",
            "root": "/workspace/config",
            "changes": ["settings.toml"],
        },
    )

    assert claim.status == "STALE"
    assert claim.invalidated_by[0]["paths"] == ["config/settings.toml"]
