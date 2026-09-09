"""SQLite-backed lease and recovery coverage for the Pack-hook outbox."""

from __future__ import annotations

from datetime import timedelta

from athena.protocol.messages import utcnow
from athena.state.database import Database
from athena.state.pack_hooks import PackHookOutbox


async def _outbox() -> tuple[Database, PackHookOutbox]:
    db = Database(":memory:")
    await db._ensure_ready()
    return db, PackHookOutbox(db)


async def _enqueue(outbox: PackHookOutbox) -> dict:
    return await outbox.enqueue(
        pack_id="pack-a",
        hook_id="hook-a",
        event_id="event-a",
        event_type="task.completed",
        task_id="task-a",
        session_id="session-a",
        payload={"ok": True},
        depth=0,
    )


async def test_pack_hook_outbox_claim_and_token_ownership() -> None:
    db, outbox = await _outbox()
    try:
        pending = await _enqueue(outbox)
        assert pending["status"] == "PENDING"
        assert pending["attempts"] == 0

        claimed = await outbox.claim(pending["id"])
        assert claimed is not None
        assert claimed["status"] == "CLAIMED"
        assert claimed["claim_token"]
        assert claimed["attempts"] == 1

        assert await outbox.claim(pending["id"]) is None
        await outbox.mark_dispatched(
            pending["id"], "task-created", claim_token=claimed["claim_token"]
        )
        row = await db.fetch_one("SELECT * FROM pack_hook_outbox WHERE id = ?", (pending["id"],))
        assert row["status"] == "DISPATCHED"
        assert row["dispatched_task_id"] == "task-created"
    finally:
        await db.close()


async def test_pack_hook_outbox_expired_lease_reclaims_and_rejects_stale_token() -> None:
    db, outbox = await _outbox()
    try:
        pending = await _enqueue(outbox)
        claimed = await outbox.claim(pending["id"])
        assert claimed is not None
        await db.execute(
            "UPDATE pack_hook_outbox SET claim_expires_at = ? WHERE id = ?",
            ((utcnow() - timedelta(seconds=1)).isoformat(), pending["id"]),
        )

        reclaimed = await outbox.claim(pending["id"])
        assert reclaimed is not None
        assert reclaimed["claim_token"] != claimed["claim_token"]
        assert reclaimed["attempts"] == 2

        await outbox.mark_dispatched(
            pending["id"], "stale-task", claim_token=claimed["claim_token"]
        )
        still_claimed = await db.fetch_one(
            "SELECT status FROM pack_hook_outbox WHERE id = ?", (pending["id"],)
        )
        assert still_claimed["status"] == "CLAIMED"
        await outbox.mark_failed(
            pending["id"], "worker crashed", claim_token=reclaimed["claim_token"]
        )
        failed = await db.fetch_one(
            "SELECT status FROM pack_hook_outbox WHERE id = ?", (pending["id"],)
        )
        assert failed["status"] == "FAILED"
    finally:
        await db.close()


async def test_pack_hook_outbox_suspend_resume_and_cancel() -> None:
    db, outbox = await _outbox()
    try:
        first = await _enqueue(outbox)
        await outbox.suspend_pack("pack-a", "maintenance")
        suspended = await db.fetch_one(
            "SELECT status FROM pack_hook_outbox WHERE id = ?", (first["id"],)
        )
        assert suspended["status"] == "SUSPENDED"
        assert await outbox.pending() == []

        await outbox.resume_pack("pack-a")
        assert (await outbox.pending())[0]["id"] == first["id"]

        second = await outbox.enqueue(
            pack_id="pack-a",
            hook_id="hook-b",
            event_id="event-b",
            event_type="task.failed",
            task_id=None,
            session_id=None,
            payload={},
            depth=0,
        )
        await outbox.cancel_pack("pack-a", "uninstalled")
        rows = await db.fetch_all(
            "SELECT status FROM pack_hook_outbox WHERE pack_id = ?", ("pack-a",)
        )
        assert {row["status"] for row in rows} == {"CANCELLED"}
        assert second["id"] != first["id"]
    finally:
        await db.close()


async def test_pack_hook_outbox_reclaims_after_database_restart(tmp_path) -> None:
    database_path = tmp_path / "pack-hooks.sqlite"
    first_db = Database(str(database_path))
    await first_db._ensure_ready()
    first_outbox = PackHookOutbox(first_db)
    pending = await _enqueue(first_outbox)
    claimed = await first_outbox.claim(pending["id"])
    assert claimed is not None
    await first_db.close()

    second_db = Database(str(database_path))
    await second_db._ensure_ready()
    second_outbox = PackHookOutbox(second_db)
    await second_db.execute(
        "UPDATE pack_hook_outbox SET claim_expires_at = ? WHERE id = ?",
        ((utcnow() - timedelta(seconds=1)).isoformat(), pending["id"]),
    )
    reclaimed = await second_outbox.claim(pending["id"])
    assert reclaimed is not None
    assert reclaimed["attempts"] == 2
    await second_db.close()
