"""Durable approval resolution and expiration invariants."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from athena.state.approvals import ApprovalStore
from athena.state.database import Database


def _expired_at() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


@pytest.mark.asyncio
async def test_expired_grant_commits_expiration_before_raising(tmp_path):
    db = Database(str(tmp_path / "approvals.db"))
    store = ApprovalStore(db)
    try:
        approval_id = await store.create_request(
            None,
            "execute",
            metadata={"expires_at": _expired_at()},
        )

        with pytest.raises(ValueError, match="Approval expired"):
            await store.record_grant(approval_id, resolver="operator")

        record = await store.get(approval_id)
        assert record is not None
        assert record["status"] == ApprovalStore.EXPIRED
        assert await store.list_pending() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expired_deny_commits_expiration_before_raising(tmp_path):
    db = Database(str(tmp_path / "approvals.db"))
    store = ApprovalStore(db)
    try:
        approval_id = await store.create_request(
            None,
            "execute",
            metadata={"expires_at": _expired_at()},
        )

        with pytest.raises(ValueError, match="Approval expired"):
            await store.record_deny(approval_id, resolver="operator")

        record = await store.get(approval_id)
        assert record is not None
        assert record["status"] == ApprovalStore.EXPIRED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_grant_and_deny_have_one_winner(tmp_path):
    db = Database(str(tmp_path / "approvals.db"))
    store = ApprovalStore(db)
    try:
        approval_id = await store.create_request(None, "execute")
        outcomes = await asyncio.gather(
            store.record_grant(approval_id, resolver="grant", scope="call"),
            store.record_deny(approval_id, resolver="deny"),
            return_exceptions=True,
        )

        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        record = await store.get(approval_id)
        assert record is not None
        assert record["status"] in {ApprovalStore.GRANTED, ApprovalStore.DENIED}
        grants = await store.list_granted()
        assert len(grants) in {0, 1}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_expired_resolution_has_one_expiration_winner(tmp_path):
    db = Database(str(tmp_path / "approvals.db"))
    store = ApprovalStore(db)
    try:
        approval_id = await store.create_request(
            None,
            "execute",
            metadata={"expires_at": _expired_at()},
        )
        outcomes = await asyncio.gather(
            store.record_grant(approval_id, resolver="grant"),
            store.record_deny(approval_id, resolver="deny"),
            return_exceptions=True,
        )

        assert all(isinstance(outcome, ValueError) for outcome in outcomes)
        assert sum("Approval expired" in str(outcome) for outcome in outcomes) == 1
        assert sum("already resolved" in str(outcome) for outcome in outcomes) == 1
        record = await store.get(approval_id)
        assert record is not None
        assert record["status"] == ApprovalStore.EXPIRED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_reconciles_expired_approval_out_of_pending(tmp_path):
    path = str(tmp_path / "approvals.db")
    db = Database(path)
    store = ApprovalStore(db)
    approval_id = await store.create_request(
        None,
        "execute",
        metadata={"expires_at": _expired_at()},
    )
    await db.close()

    reopened = Database(path)
    reopened_store = ApprovalStore(reopened)
    try:
        assert await reopened_store.list_pending() == []
        record = await reopened_store.get(approval_id)
        assert record is not None
        assert record["status"] == ApprovalStore.EXPIRED
    finally:
        await reopened.close()
