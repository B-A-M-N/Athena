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


def test_claim_is_not_consumed_until_execution_finishes(db_path):
    async def run():
        db = Database(db_path)
        store = ContinuationStore(db)
        await store.record(
            task_id="task-1",
            call_id="call-claim",
            capability_id="fs.write_file",
            canonical_arguments={"path": "a.txt", "content": "ok"},
            approval_id="apr-claim",
        )
        pending = await store.pending(task_id="task-1")
        await store.mark_resolved(pending[0]["id"], "granted")

        claimed = await store.claim_resolved("task-1")
        assert claimed is not None
        assert claimed["claimed_at"] is not None
        assert claimed["consumed_at"] is None
        assert await store.claim_resolved("task-1") is None

        await store.release_claim("call-claim")
        reclaimed = await store.claim_resolved("task-1")
        assert reclaimed is not None
        await store.mark_consumed_for_call("call-claim")
        assert await store.claim_resolved("task-1") is None
        await db.close()

    asyncio.run(run())


def test_restart_releases_claims_and_lists_recoverable_tasks(db_path):
    async def run():
        db = Database(db_path)
        store = ContinuationStore(db)
        await store.record(
            task_id="task-restart",
            call_id="call-restart",
            capability_id="fs.write_file",
            canonical_arguments={"path": "a.txt", "content": "ok"},
            approval_id="apr-restart",
        )
        row = (await store.pending(task_id="task-restart"))[0]
        await store.mark_resolved(row["id"], "granted")
        assert await store.claim_resolved("task-restart") is not None

        # A fresh service owner makes a crashed claim available immediately;
        # it does not wait for the five-minute stale-claim timeout.
        await store.release_claims_for_restart()
        assert await store.recoverable_task_ids() == ["task-restart"]
        assert await store.claim_resolved("task-restart") is not None
        await db.close()

    asyncio.run(run())
