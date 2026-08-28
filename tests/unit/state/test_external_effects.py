from __future__ import annotations

import pytest

from athena.protocol.capabilities import ExternalEffectPhase
from athena.state.database import Database
from athena.state.external_effects import (
    ExternalEffectRecoveryRequired,
    ExternalEffectStore,
)


async def _prepared(store: ExternalEffectStore, transaction_id: str = "tx-1"):
    return await store.prepare(
        transaction_id=transaction_id,
        task_id="task-1",
        capability_id="network",
        external_identity="POST https://example.test/items",
        request_digest="digest",
        idempotency_key="key-1",
        phase=ExternalEffectPhase.PREPARE,
    )


@pytest.mark.asyncio
async def test_interrupted_apply_cannot_be_replayed():
    store = ExternalEffectStore()
    await _prepared(store)
    applying, replay = await store.begin_apply(
        transaction_id="tx-1",
        task_id="task-1",
        capability_id="network",
        external_identity="POST https://example.test/items",
        request_digest="digest",
        idempotency_key="key-1",
    )

    assert applying["status"] == "APPLYING"
    assert replay is False
    with pytest.raises(ExternalEffectRecoveryRequired):
        await store.begin_apply(
            transaction_id="tx-1",
            task_id="task-1",
            capability_id="network",
            external_identity="POST https://example.test/items",
            request_digest="digest",
            idempotency_key="key-1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["APPLYING", "VERIFYING", "COMPENSATING"])
async def test_startup_reconciles_inflight_external_effect(status):
    store = ExternalEffectStore()
    await _prepared(store)
    await store.finish("tx-1", status=status)

    recovered = await store.reconcile_startup()

    assert len(recovered) == 1
    assert recovered[0]["status"] == "RECOVERY_REQUIRED"
    assert recovered[0]["response"]["recovery"]["previous_status"] == status
    assert (await store.get("tx-1"))["status"] == "RECOVERY_REQUIRED"


@pytest.mark.asyncio
async def test_prepared_external_effect_remains_actionable_after_startup_reconcile():
    store = ExternalEffectStore()
    await _prepared(store)

    assert await store.reconcile_startup() == []
    receipt, replay = await store.begin_apply(
        transaction_id="tx-1",
        task_id="task-1",
        capability_id="network",
        external_identity="POST https://example.test/items",
        request_digest="digest",
        idempotency_key="key-1",
    )
    assert receipt["status"] == "APPLYING"
    assert replay is False


@pytest.mark.asyncio
async def test_recovery_provenance_survives_durable_restart():
    db = Database(":memory:")
    store = ExternalEffectStore(db)
    await _prepared(store)
    await store.finish("tx-1", status="COMPENSATING", phase=ExternalEffectPhase.COMPENSATE)

    recovered = await store.reconcile_startup()
    assert recovered[0]["verification_target"] == "COMPENSATION_PRESTATE"

    restarted = ExternalEffectStore(db)
    receipt = await restarted.get("tx-1")
    assert receipt["recovery_origin_status"] == "COMPENSATING"
    assert receipt["recovery_origin_phase"] == "compensate"
    assert receipt["verification_target"] == "COMPENSATION_PRESTATE"
    await db.close()
