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
    assert {row["hook_session_id"] for row in outbox_rows} == {
        stable_id("pack-hook-session", hook_id, row["event_id"]) for row in outbox_rows
    }
    assert all(row["pack_version"] == "1.0.0" for row in outbox_rows)
    assert all(row["pack_integrity"] for row in outbox_rows)
    assert all(row["workflow_id"] == "pack:hook-flow-pack:workflow:1" for row in outbox_rows)
    assert all(row["workflow_integrity"] for row in outbox_rows)
    assert all(json.loads(row["effect_ceiling"]) == ["READ_LOCAL"] for row in outbox_rows)
    assert all(row["recursion_limit"] == 1 for row in outbox_rows)
    assert all(row["hook_contract_digest"] for row in outbox_rows)

    for row in rows:
        events = await service._store_events.list_for_task(row["id"])
        completed = [event for event in events if event.type == "TaskCompleted"]
        assert completed
        assert completed[-1].causal_id is not None
        assert completed[-1].causal_id.startswith("athena-pack-hook:")


@pytest.mark.athena_claim("BHV-PACK-HOOK-CRASH-REPLAY")
@pytest.mark.athena_evidence("test", "integration", "invariant")
@pytest.mark.asyncio
async def test_pack_hook_reconciles_task_committed_before_dispatch_receipt(
    make_service,
) -> None:
    """A crash after Task commit but before the outbox receipt is idempotent."""
    service = await make_service()
    source = Path(service.config.workspace_root) / "hook-crash-pack"
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
                        "recursion_limit": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (source / "athena.pack.toml").write_text(
        "id = 'hook-crash-pack'\n"
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
    await service._pack_manager.stop_hook_dispatcher()
    outbox = service._pack_hook_outbox
    original_mark_dispatched = outbox.mark_dispatched
    injected = False

    async def fail_receipt(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("injected crash before DISPATCHED receipt")
        return await original_mark_dispatched(*args, **kwargs)

    outbox.mark_dispatched = fail_receipt
    root_event = await service._store_events.append_event(
        "TaskCompleted",
        {"status": "COMPLETE", "source": "crash-replay-test"},
    )
    hook_id = "pack:hook-crash-pack:hook:1"
    first_task_id = stable_id("pack-hook-task", hook_id, root_event.id)

    row = await service._db.fetch_one(
        "SELECT * FROM pack_hook_outbox WHERE hook_id = ? AND event_id = ?",
        (hook_id, root_event.id),
    )
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["hook_task_id"] == first_task_id
    assert row["hook_session_id"] == stable_id("pack-hook-session", hook_id, root_event.id)
    first_rows = [
        task
        for task in await service.list_tasks()
        if (task.get("metadata") or {}).get("_pack_hook") == hook_id
    ]
    assert len(first_rows) == 1
    assert first_rows[0]["id"] == first_task_id

    outbox.mark_dispatched = original_mark_dispatched
    await service._db.execute(
        "UPDATE pack_hook_outbox SET next_attempt_at = NULL WHERE id = ?",
        (row["id"],),
    )
    assert await service._pack_manager.replay_hook_outbox() == 1

    final = await service._db.fetch_one("SELECT * FROM pack_hook_outbox WHERE id = ?", (row["id"],))
    assert final["status"] == "DISPATCHED"
    assert final["attempts"] == 2
    assert final["dispatched_task_id"] == first_task_id
    all_rows = [
        task
        for task in await service.list_tasks()
        if (task.get("metadata") or {}).get("_pack_hook") == hook_id
    ]
    assert len(all_rows) == 1
    assert all_rows[0]["session_id"] == final["hook_session_id"]
    messages = await service._db.fetch_all(
        "SELECT id FROM messages WHERE id = ?", (f"msg_user_{first_task_id}",)
    )
    assert len(messages) == 1
    await service.wait_for(first_task_id, timeout=10.0)
    workflow_runs = await service._db.fetch_all(
        "SELECT id FROM workflow_runs WHERE task_id = ?", (first_task_id,)
    )
    assert len(workflow_runs) == 1


@pytest.mark.athena_claim("BHV-PACK-HOOK-CONTRACT-FENCE")
@pytest.mark.athena_evidence("test", "integration", "security")
@pytest.mark.asyncio
async def test_pack_hook_upgrade_marks_old_contract_stale(make_service) -> None:
    service = await make_service()
    source = Path(service.config.workspace_root) / "hook-contract-pack"
    (source / "hooks").mkdir(parents=True)
    (source / "workflows").mkdir()
    (source / "workflows" / "review.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Hook review v1",
                "description": "Original contract.",
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
                        "recursion_limit": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = """id = 'hook-contract-pack'
version = '1.0.0'
publisher = 'test'
[provides]
workflows = ['workflows/review.json']
hooks = ['hooks/events.json']
[authority]
requested_effects = ['READ_LOCAL']
"""
    (source / "athena.pack.toml").write_text(manifest, encoding="utf-8")
    await service.install_pack(str(source))
    await service._pack_manager.stop_hook_dispatcher()

    original_intake = service._pack_manager._task_intake

    async def reject_dispatch(*_args, **_kwargs):
        raise RuntimeError("injected unavailable task intake")

    service._pack_manager._task_intake = reject_dispatch
    root_event = await service._store_events.append_event("TaskCompleted", {"status": "COMPLETE"})
    hook_id = "pack:hook-contract-pack:hook:1"
    old_row = await service._db.fetch_one(
        "SELECT * FROM pack_hook_outbox WHERE hook_id = ? AND event_id = ?",
        (hook_id, root_event.id),
    )
    assert old_row and old_row["status"] == "FAILED"

    (source / "workflows" / "review.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Hook review v2",
                "description": "Changed contract.",
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
    (source / "athena.pack.toml").write_text(manifest.replace("1.0.0", "2.0.0"), encoding="utf-8")
    await service._pack_manager.upgrade(
        str(source), allowed_root=str(Path(service.config.workspace_root))
    )
    service._pack_manager._task_intake = original_intake

    resumed = await service._db.fetch_one(
        "SELECT status, pack_version FROM pack_hook_outbox WHERE id = ?", (old_row["id"],)
    )
    assert resumed["status"] == "PENDING"
    assert resumed["pack_version"] == "1.0.0"
    assert await service._pack_manager.replay_hook_outbox() == 0
    stale = await service._db.fetch_one(
        "SELECT status, error FROM pack_hook_outbox WHERE id = ?", (old_row["id"],)
    )
    assert stale["status"] == "STALE_CONTRACT"
    assert "contract" in stale["error"]
