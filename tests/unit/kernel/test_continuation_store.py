"""Durable approval continuation store (review item 19)."""
from __future__ import annotations

import asyncio

import pytest

from athena.kernel.continuations import ContinuationStore
from athena.state.database import Database


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "athena.db")


def test_record_pending_resolve_round_trip(db_path):
    async def run():
        db = Database(db_path)
        store = ContinuationStore(db)
        cid = await store.record(
            task_id="task-1",
            call_id="call-1",
            capability_id="fs.write_file",
            canonical_arguments={"path": "a.txt", "content": "hi"},
            schema_hash="abc123",
            effects=["FILESYSTEM_WRITE"],
            workspace_id="root",
        )
        assert cid.startswith("cont")

        pending = await store.pending()
        assert len(pending) == 1
        row = pending[0]
        assert row["id"] == cid
        assert row["task_id"] == "task-1"
        assert row["call_id"] == "call-1"
        assert row["capability_id"] == "fs.write_file"
        assert row["canonical_arguments"] == {"path": "a.txt", "content": "hi"}
        assert row["schema_hash"] == "abc123"
        assert row["effects"] == ["FILESYSTEM_WRITE"]
        assert row["workspace_id"] == "root"
        assert row["resolved_at"] is None

        # task_id filter
        assert await store.pending(task_id="task-2") == []
        assert len(await store.pending(task_id="task-1")) == 1

        await store.mark_resolved(cid)
        assert await store.pending() == []

        await db.close()

    asyncio.run(run())


def test_continuations_survive_restart(db_path):
    """Two ContinuationStore instances on the same DB file see the same rows."""

    async def first_process():
        db = Database(db_path)
        store = ContinuationStore(db)
        await store.record(
            task_id="task-1",
            call_id="call-9",
            capability_id="shell.exec",
            canonical_arguments={"cmd": "ls"},
            effects=[],
            workspace_id="root",
        )
        await db.close()  # simulate process exit

    async def restarted_process():
        db = Database(db_path)
        store = ContinuationStore(db)  # fresh instance on the same file
        pending = await store.pending()
        assert len(pending) == 1
        row = pending[0]
        assert row["call_id"] == "call-9"
        assert row["canonical_arguments"] == {"cmd": "ls"}
        assert row["resolved_at"] is None

        # Resolution also persists across instances.
        await store.mark_resolved(row["id"])
        assert await store.pending() == []
        await db.close()

    asyncio.run(first_process())
    asyncio.run(restarted_process())
