"""Release-proof Pack-hook delivery through the real service composition root."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from athena.protocol.ids import stable_id


@pytest.mark.athena_claim("BHV-PACK-HOOK-DURABILITY")
@pytest.mark.athena_evidence("test", "integration", "e2e")
@pytest.mark.asyncio
async def test_pack_hook_event_reaches_kernel_workflow_and_stops_at_causal_limit(
    make_service,
) -> None:
    """A real event becomes one deterministic workflow task per causal depth."""
    service = await make_service()
    source = Path(service.config.workspace_root) / "hook-flow-pack"
    (source / "hooks").mkdir(parents=True)
    (source / "workflows").mkdir()
    (source / "workflows" / "review.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Hook review",
                "description": "Read the durable truth surface.",
                "steps": [
                    {
                        "id": "status",
                        "capability": "truth",
                        "arguments": {"operation": "status"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (source / "hooks" / "events.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "event": "TaskCompleted",
                        "workflow": "review",
                        "effects": ["READ_LOCAL"],
                        "recursion_limit": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (source / "athena.pack.toml").write_text(
        "id = 'hook-flow-pack'\n"
        "version = '1.0.0'\n"
        "publisher = 'test'\n"
        "[provides]\n"
        "workflows = ['workflows/review.json']\n"
        "hooks = ['hooks/events.json']\n"
        "[authority]\n"
        "requested_effects = ['READ_LOCAL']\n",
        encoding="utf-8",
    )

    await service.install_pack(str(source))
    root_event = await service._store_events.append_event(
        "TaskCompleted",
        {"status": "COMPLETE", "source": "integration-test"},
    )

    hook_id = "pack:hook-flow-pack:hook:1"
    first_task_id = stable_id("pack-hook-task", hook_id, root_event.id)

    async def _hook_tasks() -> list[dict]:
        return [
            row
            for row in await service.list_tasks()
            if (row.get("metadata") or {}).get("_pack_hook") == hook_id
        ]

    for _ in range(500):
        rows = await _hook_tasks()
        if len(rows) == 2 and all(row.get("status") in {"COMPLETE", "FAILED"} for row in rows):
            break
        await asyncio.sleep(0.02)

    rows = await _hook_tasks()
    assert len(rows) == 2
    assert {row["id"] for row in rows} >= {first_task_id}
    details = []
    for row in rows:
        result = await service.get_result(row["id"])
        details.append(
            {
                "id": row["id"],
                "status": row.get("status"),
                "summary": result.summary if result is not None else None,
                "error": result.unresolved if result is not None else None,
            }
        )
    assert all(item["status"] == "COMPLETE" for item in details), details
    assert all(
        frozenset(row["capability_policy"]["effects"]) == frozenset({"READ_LOCAL"}) for row in rows
    )

    outbox_rows = await service._db.fetch_all(
        "SELECT * FROM pack_hook_outbox WHERE hook_id = ? ORDER BY created_at, id",
        (hook_id,),
    )
    assert len(outbox_rows) == 2
    assert all(row["status"] == "DISPATCHED" for row in outbox_rows)
    assert all(row["attempts"] == 1 for row in outbox_rows)
    assert {row["hook_task_id"] for row in outbox_rows} == {row["id"] for row in rows}

    for row in rows:
        events = await service._store_events.list_for_task(row["id"])
        completed = [event for event in events if event.type == "TaskCompleted"]
        assert completed
        assert completed[-1].causal_id is not None
        assert completed[-1].causal_id.startswith("athena-pack-hook:")
