"""Provider-outcome dispositions survive a real service restart boundary."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from athena.state.database import Database
from athena.state.model_responses import ModelResponseStore
from athena.state.sessions import SessionRepository
from athena.state.tasks import TaskStore
from athena.tasks.budgets import BudgetTracker
from athena.protocol.tasks import TaskStatus


@pytest.mark.athena_claim("RECOVERY-PROVIDER-DISPOSITIONS")
@pytest.mark.athena_evidence("test", "crash-restart")
async def test_provider_dispositions_replay_on_service_restart(
    make_durable_service, durable_db_path
):
    db = Database(durable_db_path)
    await db._ensure_ready()
    try:
        sessions = SessionRepository(db)
        tasks = TaskStore(db)
        budgets = BudgetTracker(task_store=tasks)
        responses = ModelResponseStore(db)
        cases = (
            ("failed", "confirmed_failed", None),
            ("succeeded", "confirmed_succeeded", "0.40"),
            ("retry", "retry_authorized", None),
            ("abandoned", "abandoned_with_liability", None),
        )
        for suffix, resolution, actual_cost in cases:
            task_id = f"task-service-restart-{suffix}"
            session_id = f"session-service-restart-{suffix}"
            await sessions.create(session_id)
            await db.execute(
                "INSERT INTO tasks(id, session_id, status, autonomy, objective, resource_budget, "
                "created_at, updated_at) VALUES (?, ?, 'RECOVERY_REQUIRED', 'supervised', ?, ?, "
                "'2026-01-01', '2026-01-01')",
                (
                    task_id,
                    session_id,
                    f"service restart {suffix}",
                    json.dumps({"max_cost_usd": "2.00"}),
                ),
            )
            receipt = await responses.prepare(
                task_id=task_id,
                request_fingerprint=f"service-restart-fingerprint-{suffix}",
                request_id=f"service-restart-call-{suffix}",
                provider="fixture",
                model="fixture-model",
                reservation_amount=Decimal("1.00"),
            )
            attempt_id = str(receipt["attempt_id"])
            await budgets.reserve_model_cost(task_id, Decimal("1.00"), reservation_id=attempt_id)
            await responses.mark_reservation_applied(attempt_id=attempt_id)
            await responses.mark_provider_outcome_unknown(attempt_id=attempt_id)
            await responses.resolve_provider_outcome(
                attempt_id=attempt_id,
                resolution=resolution,
                note=f"committed before service restart: {suffix}",
                actual_cost=actual_cost,
            )
    finally:
        await db.close()

    # Startup, rather than a direct reconciliation method call, owns the
    # replay in this test. Disable the worker pool so only the explicit retry
    # disposition can launch work.
    service = await make_durable_service(
        durable_db_path,
        scripts=None,
        worker_max_parallel=0,
    )
    failed = await service._store_tasks.get("task-service-restart-failed")
    succeeded = await service._store_tasks.get("task-service-restart-succeeded")
    retry = await service._store_tasks.get("task-service-restart-retry")
    abandoned = await service._store_tasks.get("task-service-restart-abandoned")
    assert failed["status"] == TaskStatus.FAILED.value
    assert succeeded["status"] == TaskStatus.FAILED.value
    assert retry["status"] != TaskStatus.RECOVERY_REQUIRED.value
    assert abandoned["status"] == TaskStatus.FAILED.value

    success = next(
        row
        for row in await service._model_response_store.list_resolved_attempts()
        if row["task_id"] == "task-service-restart-succeeded"
    )
    assert success["budget_accounted_at"] is not None
    assert success["reservation_released_at"] is not None
    assert (await service._budgets.total("task-service-restart-succeeded")).cost == Decimal("0.40")
    assert not await service._model_response_store.has_unresolved_liability(
        "task-service-restart-failed"
    )
    assert await service._model_response_store.has_unresolved_liability(
        "task-service-restart-abandoned"
    )

    abandoned_attempt = next(
        row
        for row in await service._model_response_store.list_resolved_attempts()
        if row["task_id"] == "task-service-restart-abandoned"
    )
    closed = await service.close_provider_liability(
        str(abandoned_attempt["attempt_id"]),
        note="closed after restart verification",
    )
    assert closed["liability_closed"] is True
    assert not await service._model_response_store.has_unresolved_liability(
        "task-service-restart-abandoned"
    )
