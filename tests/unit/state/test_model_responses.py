from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from athena.protocol.errors import ProviderOutcomeUnknown
from athena.protocol.messages import TextBlock
from athena.protocol.models import ModelInfo, ModelResponse, UsageInfo
from athena.protocol.models import ModelRequest
from athena.protocol.tasks import TaskSpec, TaskStatus
from athena.kernel.inference_broker import InferenceBroker
from athena.models.router import ModelSelection
from athena.state.database import Database
from athena.state.model_responses import ModelResponseStore
from athena.state.provider_usage import ProviderUsageStore
from athena.state.tasks import TaskStore
from athena.service.provider_recovery import ProviderOutcomeRecoveryAPI
from athena.service.service import AthenaService
from athena.tasks.budgets import BudgetTracker
from athena.tasks.manager import TaskManager


async def test_model_response_receipt_is_durable_and_reused():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-r', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    first = await store.prepare(
        task_id="task-r",
        request_fingerprint="fingerprint-1",
        request_id="call-first",
        provider="fixture",
        model="fixture-model",
    )
    assert first["status"] == "PENDING"
    response = ModelResponse(
        request_id="call-first",
        provider="fixture",
        model="fixture-model",
        blocks=(TextBlock(text="durable answer"),),
        usage=UsageInfo(input_tokens=4, output_tokens=2),
        metadata={"task_id": "task-r"},
    )
    assert await store.complete(
        attempt_id=str(first["attempt_id"]),
        response=response,
    )

    recovered = await store.prepare(
        task_id="task-r",
        request_fingerprint="fingerprint-1",
        request_id="call-random-retry",
        provider="fixture",
        model="fixture-model",
    )
    assert recovered["request_id"] == "call-first"
    decoded = store.response_from_row(recovered)
    assert decoded is not None
    assert decoded.request_id == "call-first"
    assert decoded.blocks[0].text == "durable answer"
    assert decoded.usage.input_tokens == 4
    await db.close()


async def test_unmatched_pending_receipt_is_preserved_for_conservative_reconciliation():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-fingerprint-migration', 'RUNNING', 'supervised', 'objective', "
        "'2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    legacy = await store.prepare(
        task_id="task-fingerprint-migration",
        request_fingerprint="legacy-process-dependent-fingerprint",
        request_id="call-before-restart",
        provider="fixture",
        model="fixture-model",
    )

    with pytest.raises(ProviderOutcomeUnknown, match="prior fingerprint format"):
        await store.prepare(
            task_id="task-fingerprint-migration",
            request_fingerprint="v2-canonical-fingerprint",
            request_id="call-after-restart",
            provider="fixture",
            model="fixture-model",
        )

    preserved = await db.fetch_one(
        "SELECT request_fingerprint, status, attempt_id FROM model_response_receipts "
        "WHERE task_id = ?",
        ("task-fingerprint-migration",),
    )
    assert preserved == {
        "request_fingerprint": "legacy-process-dependent-fingerprint",
        "status": "PENDING",
        "attempt_id": legacy["attempt_id"],
    }
    await db.close()


async def test_model_response_completion_is_first_writer_wins():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-first-writer', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    prepared = await store.prepare(
        task_id="task-first-writer",
        request_fingerprint="fingerprint-first-writer",
        request_id="call-first",
        provider="fixture",
        model="fixture-model",
    )
    first = ModelResponse(
        request_id="call-first",
        provider="fixture",
        model="fixture-model",
        blocks=(TextBlock(text="first response"),),
        usage=UsageInfo(input_tokens=2, output_tokens=3, cost_usd=Decimal("0.10")),
    )
    second = ModelResponse(
        request_id="call-second",
        provider="fixture",
        model="fixture-model",
        blocks=(TextBlock(text="late duplicate"),),
        usage=UsageInfo(input_tokens=99, output_tokens=99, cost_usd=Decimal("9.99")),
    )
    assert await store.complete(
        attempt_id=str(prepared["attempt_id"]),
        response=first,
    )
    assert not await store.complete(
        attempt_id=str(prepared["attempt_id"]),
        response=second,
    )
    row = await store.prepare(
        task_id="task-first-writer",
        request_fingerprint="fingerprint-first-writer",
        request_id="retry",
        provider="fixture",
        model="fixture-model",
    )
    decoded = store.response_from_row(row)
    assert decoded is not None
    assert decoded.blocks[0].text == "first response"
    assert row["actual_cost"] == "0.10"
    await db.close()


async def test_failed_model_response_receipt_can_be_reprepared():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-f', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    prepared = await store.prepare(
        task_id="task-f",
        request_fingerprint="fingerprint-f",
        request_id="call-failed",
        provider="fixture",
        model="fixture-model",
    )
    assert await store.fail(attempt_id=str(prepared["attempt_id"]))
    recovered = await store.prepare(
        task_id="task-f",
        request_fingerprint="fingerprint-f",
        request_id="call-retry",
        provider="fixture",
        model="fixture-model",
    )
    assert recovered["status"] == "PENDING"
    assert recovered["request_id"] == "call-retry"
    attempts = await db.fetch_all(
        "SELECT attempt_id, status FROM model_response_attempts "
        "WHERE task_id = 'task-f' ORDER BY created_at"
    )
    assert len(attempts) == 2
    assert attempts[0]["status"] == "FAILED"
    assert attempts[1]["status"] == "PENDING"
    await db.close()


async def test_model_response_receipt_survives_database_restart(tmp_path):
    path = tmp_path / "responses.sqlite"
    first_db = Database(str(path))
    await first_db._ensure_ready()
    await first_db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-restart', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    first_store = ModelResponseStore(first_db)
    prepared = await first_store.prepare(
        task_id="task-restart",
        request_fingerprint="fingerprint-restart",
        request_id="call-original",
        provider="fixture",
        model="fixture-model",
    )
    await first_store.complete(
        attempt_id=str(prepared["attempt_id"]),
        response=ModelResponse(
            request_id="call-original",
            provider="fixture",
            model="fixture-model",
            blocks=(TextBlock(text="exactly once"),),
        ),
    )
    await first_db.close()

    second_db = Database(str(path))
    second_store = ModelResponseStore(second_db)
    recovered = await second_store.prepare(
        task_id="task-restart",
        request_fingerprint="fingerprint-restart",
        request_id="call-after-restart",
        provider="fixture",
        model="fixture-model",
    )
    response = second_store.response_from_row(recovered)
    assert response is not None
    assert response.request_id == "call-original"
    assert response.blocks[0].text == "exactly once"
    await second_db.close()


async def test_model_response_accounting_and_append_markers_are_idempotent():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-markers', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    prepared = await store.prepare(
        task_id="task-markers",
        request_fingerprint="fingerprint-markers",
        request_id="call-markers",
        provider="fixture",
        model="fixture-model",
    )
    await store.complete(
        attempt_id=str(prepared["attempt_id"]),
        response=ModelResponse(
            request_id="call-markers",
            provider="fixture",
            model="fixture-model",
            blocks=(TextBlock(text="markers"),),
        ),
    )

    assert await store.mark_accounting_applied(attempt_id=str(prepared["attempt_id"]))
    assert not await store.mark_accounting_applied(attempt_id=str(prepared["attempt_id"]))
    row = await db.fetch_one(
        "SELECT attempt_id FROM model_response_receipts WHERE task_id = ?",
        ("task-markers",),
    )
    assert row is not None
    attempt_id = str(row["attempt_id"])
    assert await store.mark_assistant_appended(attempt_id)
    assert not await store.mark_assistant_appended(attempt_id)
    await db.close()


async def test_replayed_receipt_charges_budget_and_provider_usage_once():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-reconcile', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    task = TaskSpec(id="task-reconcile", objective="objective")
    budgets = BudgetTracker(task_store=TaskStore(db))
    store = ModelResponseStore(db)
    usage = ProviderUsageStore(db)
    receipt = await store.prepare(
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile",
        request_id="call-reconcile",
        provider="fixture",
        model="fixture-model",
        reservation_amount=Decimal("1.00"),
    )
    attempt_id = str(receipt["attempt_id"])
    await budgets.reserve_model_cost(task.id, Decimal("1.00"), reservation_id=attempt_id)
    await store.mark_reservation_applied(attempt_id=attempt_id)
    usage_id = await usage.record_attempt(
        provider="fixture", model="fixture-model", task_id=task.id, usage_id=attempt_id
    )
    await store.set_provider_usage_id(
        attempt_id=attempt_id,
        provider_usage_id=usage_id,
    )
    response = ModelResponse(
        request_id="call-reconcile",
        provider="fixture",
        model="fixture-model",
        blocks=(TextBlock(text="replay"),),
        usage=UsageInfo(input_tokens=7, output_tokens=3, cost_usd=Decimal("0.40")),
    )
    await store.complete(
        attempt_id=attempt_id,
        response=response,
    )
    recovered = await store.prepare(
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile",
        request_id="retry-id",
        provider="fixture",
        model="fixture-model",
    )
    kernel = SimpleNamespace(
        _budgets=budgets,
        _provider_usage_store=usage,
        _model_response_store=store,
    )
    broker = InferenceBroker(kernel)
    request = ModelRequest(
        messages=(),
        model="fixture-model",
        provider="fixture",
        request_id="call-reconcile",
    )
    selection = ModelSelection(
        provider="fixture",
        model="fixture-model",
        info=ModelInfo(id="fixture-model", provider="fixture"),
    )
    for _ in range(2):
        await broker._reconcile_receipt(
            task,
            {**recovered, "request_fingerprint": "fingerprint-reconcile"},
            response=response,
            request=request,
            estimator=SimpleNamespace(),
            selection=selection,
        )

    total = await budgets.total(task.id)
    assert total.model_calls == 1
    assert total.input_tokens == 7
    assert total.output_tokens == 3
    assert total.cost == Decimal("0.40")
    rows = await usage.list_for_task(task.id)
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None
    final = await store.prepare(
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile",
        request_id="another-retry",
        provider="fixture",
        model="fixture-model",
    )
    assert final["accounting_applied_at"] is not None
    await db.close()


async def test_replayed_receipt_recovers_missing_provider_usage_row():
    db = Database(":memory:")
    await db._ensure_ready()
    task = TaskSpec(id="task-reconcile-missing", objective="objective")
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES (?, 'RUNNING', 'supervised', ?, '2026-01-01', '2026-01-01')",
        (task.id, task.objective),
    )
    budgets = BudgetTracker(task_store=TaskStore(db))
    store = ModelResponseStore(db)
    usage = ProviderUsageStore(db)
    receipt = await store.prepare(
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile-missing",
        request_id="call-reconcile-missing",
        provider="fixture",
        model="fixture-model",
        reservation_amount=Decimal("1.00"),
    )
    attempt_id = str(receipt["attempt_id"])
    await budgets.reserve_model_cost(task.id, Decimal("1.00"), reservation_id=attempt_id)
    response = ModelResponse(
        request_id="call-reconcile-missing",
        provider="fixture",
        model="fixture-model",
        blocks=(TextBlock(text="replay"),),
        usage=UsageInfo(input_tokens=7, output_tokens=3, cost_usd=Decimal("0.40")),
    )
    await store.complete(
        attempt_id=attempt_id,
        response=response,
    )
    recovered = await store.prepare(
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile-missing",
        request_id="retry-id",
        provider="fixture",
        model="fixture-model",
    )
    kernel = SimpleNamespace(
        _budgets=budgets,
        _provider_usage_store=usage,
        _model_response_store=store,
    )
    broker = InferenceBroker(kernel)
    request = ModelRequest(
        messages=(),
        model="fixture-model",
        provider="fixture",
        request_id="call-reconcile-missing",
    )
    selection = ModelSelection(
        provider="fixture",
        model="fixture-model",
        info=ModelInfo(id="fixture-model", provider="fixture"),
    )
    receipt_with_fingerprint = {
        **recovered,
        "request_fingerprint": "fingerprint-reconcile-missing",
    }
    await broker._reconcile_receipt(
        task,
        receipt_with_fingerprint,
        response=response,
        request=request,
        estimator=SimpleNamespace(),
        selection=selection,
    )
    await broker._reconcile_receipt(
        task,
        receipt_with_fingerprint,
        response=response,
        request=request,
        estimator=SimpleNamespace(),
        selection=selection,
    )
    rows = await usage.list_for_task(task.id)
    assert len(rows) == 1
    assert rows[0]["id"] == attempt_id
    assert rows[0]["ended_at"] is not None
    assert rows[0]["input_tokens"] == 7
    assert rows[0]["output_tokens"] == 3
    await db.close()


async def test_attempt_lifecycle_is_addressed_by_attempt_id_and_preserves_history():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-attempt-history', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)

    first = await store.prepare(
        task_id="task-attempt-history",
        request_fingerprint="fingerprint-history",
        request_id="call-1",
        provider="fixture",
        model="fixture-model",
    )
    first_id = str(first["attempt_id"])
    assert await store.fail(attempt_id=first_id)

    second = await store.prepare(
        task_id="task-attempt-history",
        request_fingerprint="fingerprint-history",
        request_id="call-2",
        provider="fixture",
        model="fixture-model",
    )
    second_id = str(second["attempt_id"])
    assert await store.mark_provider_usage_started(attempt_id=second_id)
    assert await store.mark_provider_outcome_unknown(attempt_id=second_id)
    resolved = await store.resolve_provider_outcome(
        attempt_id=second_id,
        resolution="retry_authorized",
        note="provider has no retrieval endpoint; operator authorized a bounded retry",
    )
    assert resolved["provider_outcome_status"] == "retry_authorized"
    with pytest.raises(ValueError, match="not awaiting reconciliation"):
        await store.resolve_provider_outcome(
            attempt_id=second_id,
            resolution="confirmed_failed",
            note="a second disposition must not revise the first",
        )

    third = await store.prepare(
        task_id="task-attempt-history",
        request_fingerprint="fingerprint-history",
        request_id="call-3",
        provider="fixture",
        model="fixture-model",
    )
    third_id = str(third["attempt_id"])
    assert third_id not in {first_id, second_id}
    assert await store.mark_provider_usage_started(attempt_id=third_id)
    assert await store.complete(
        attempt_id=third_id,
        response=ModelResponse(
            request_id="call-3",
            provider="fixture",
            model="fixture-model",
            blocks=(TextBlock(text="successful retry"),),
        ),
    )

    rows = await db.fetch_all(
        "SELECT attempt_id, status, provider_outcome_status, provider_usage_started_at "
        "FROM model_response_attempts WHERE task_id = ? ORDER BY created_at",
        ("task-attempt-history",),
    )
    assert [row["attempt_id"] for row in rows] == [first_id, second_id, third_id]
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["provider_usage_started_at"] is None
    assert rows[1]["provider_outcome_status"] == "retry_authorized"
    assert rows[2]["status"] == "COMPLETED"
    await db.close()


async def test_paired_projection_write_rolls_back_on_fault():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-paired-write', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    prepared = await store.prepare(
        task_id="task-paired-write",
        request_fingerprint="fingerprint-paired-write",
        request_id="call-paired-write",
        provider="fixture",
        model="fixture-model",
    )
    attempt_id = str(prepared["attempt_id"])

    def inject(name: str) -> None:
        if name == "attempt-provider-usage-id":
            raise RuntimeError("injected projection failure")

    store.set_fault_injector(inject)
    try:
        await store.set_provider_usage_id(attempt_id=attempt_id, provider_usage_id="usage-1")
    except RuntimeError as exc:
        assert str(exc) == "injected projection failure"
    else:
        raise AssertionError("fault injection did not interrupt paired write")
    finally:
        store.set_fault_injector(None)

    attempt = await store.get_attempt(attempt_id)
    receipt = await store.get_receipt(
        task_id="task-paired-write", request_fingerprint="fingerprint-paired-write"
    )
    assert attempt is not None and attempt["provider_usage_id"] is None
    assert receipt is not None and receipt["provider_usage_id"] is None
    await db.close()


async def test_service_reconciliation_releases_confirmed_failed_reservation():
    db = Database(":memory:")
    await db._ensure_ready()
    task_id = "task-service-reconcile"
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES (?, 'RECOVERY_REQUIRED', 'supervised', 'objective', '2026-01-01', '2026-01-01')",
        (task_id,),
    )
    budgets = BudgetTracker(task_store=TaskStore(db))
    store = ModelResponseStore(db)
    prepared = await store.prepare(
        task_id=task_id,
        request_fingerprint="fingerprint-service-reconcile",
        request_id="call-service-reconcile",
        provider="fixture",
        model="fixture-model",
        reservation_amount=Decimal("1.00"),
    )
    attempt_id = str(prepared["attempt_id"])
    await budgets.reserve_model_cost(task_id, Decimal("1.00"), reservation_id=attempt_id)
    await store.mark_reservation_applied(attempt_id=attempt_id)
    await store.mark_provider_outcome_unknown(attempt_id=attempt_id)

    service = SimpleNamespace(
        _model_response_store=store,
        _budgets=budgets,
        _store_events=None,
        _task_manager=None,
    )
    service._provider_recovery_runtime = ProviderOutcomeRecoveryAPI.compose(service)
    resolved = await AthenaService.resolve_provider_outcome(
        service,
        attempt_id,
        resolution="confirmed_failed",
        note="fixture provider confirmed the request failed",
    )

    assert resolved["provider_outcome_status"] == "confirmed_failed"
    assert not await store.has_unresolved_liability(task_id)
    assert (await store.get_attempt(attempt_id))["reservation_released_at"] is not None
    await db.close()


async def test_provider_outcome_dispositions_drive_task_and_accounting_state():
    db = Database(":memory:")
    await db._ensure_ready()
    task_store = TaskStore(db)
    budgets = BudgetTracker(task_store=task_store)
    response_store = ModelResponseStore(db)
    manager = TaskManager(task_store=task_store, budgets=budgets)
    retry_calls: list[str] = []

    class RetryKernel:
        async def run_task(self, task_id: str) -> None:
            retry_calls.append(task_id)

    service = SimpleNamespace(
        _model_response_store=response_store,
        _budgets=budgets,
        _store_events=None,
        _store_tasks=task_store,
        _task_manager=manager,
        _kernel=RetryKernel(),
        _approval_recovery_tasks=set(),
        _log_background_failure=AthenaService._log_background_failure,
    )
    service._provider_recovery_runtime = ProviderOutcomeRecoveryAPI.compose(service)
    cases = (
        ("failed", "confirmed_failed", None),
        ("succeeded", "confirmed_succeeded", "0.40"),
        ("retry", "retry_authorized", None),
        ("abandoned", "abandoned_with_liability", None),
    )

    for suffix, resolution, actual_cost in cases:
        task_id = f"task-provider-{suffix}"
        await db.execute(
            "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
            "VALUES (?, 'RECOVERY_REQUIRED', 'supervised', ?, '2026-01-01', '2026-01-01')",
            (task_id, f"provider {suffix}"),
        )
        receipt = await response_store.prepare(
            task_id=task_id,
            request_fingerprint=f"fingerprint-{suffix}",
            request_id=f"call-{suffix}",
            provider="fixture",
            model="fixture-model",
            reservation_amount=Decimal("1.00"),
        )
        attempt_id = str(receipt["attempt_id"])
        await budgets.reserve_model_cost(task_id, Decimal("1.00"), reservation_id=attempt_id)
        await response_store.mark_reservation_applied(attempt_id=attempt_id)
        await response_store.mark_provider_outcome_unknown(attempt_id=attempt_id)

        resolved = await AthenaService.resolve_provider_outcome(
            service,
            attempt_id,
            resolution=resolution,
            note=f"operator disposition for {suffix}",
            provider_response_id="provider-response-1" if actual_cost else None,
            actual_cost=actual_cost,
        )
        row = await response_store.get_attempt(attempt_id)
        task = await task_store.get(task_id)
        assert row is not None and task is not None
        assert row["provider_outcome_status"] == resolution
        assert (
            row["reservation_released_at"] is not None
            if resolution != "abandoned_with_liability"
            else True
        )
        if resolution == "confirmed_succeeded":
            assert row["status"] == "COMPLETED"
            assert task["status"] == TaskStatus.FAILED.value
            assert resolved["accounted_amount"] == "0.40"
            assert (await budgets.total(task_id)).cost == Decimal("0.40")
            assert not await response_store.has_unresolved_liability(task_id)
        elif resolution == "confirmed_failed":
            assert row["status"] == "FAILED"
            assert task["status"] == TaskStatus.FAILED.value
            assert not await response_store.has_unresolved_liability(task_id)
        elif resolution == "retry_authorized":
            assert task["status"] == TaskStatus.RUNNING.value
            assert not await response_store.has_unresolved_liability(task_id)
        else:
            assert row["status"] == "ABANDONED"
            assert task["status"] == TaskStatus.FAILED.value
            assert await response_store.has_unresolved_liability(task_id)
            assert resolved["next_actions"] == ["manually close provider liability"]
            closed = await AthenaService.close_provider_liability(
                service,
                attempt_id,
                note="operator reconciled the external billing record",
            )
            assert closed["liability_open"] is False
            assert closed["liability_closed"] is True
            assert not await response_store.has_unresolved_liability(task_id)

    await asyncio.sleep(0)
    assert retry_calls == ["task-provider-retry"]
    await db.close()


async def test_all_provider_dispositions_replay_task_effects_after_restart(tmp_path):
    path = tmp_path / "provider-disposition-restart.sqlite"
    first_db = Database(str(path))
    await first_db._ensure_ready()
    first_store = ModelResponseStore(first_db)
    first_tasks = TaskStore(first_db)
    first_budgets = BudgetTracker(task_store=first_tasks)
    cases = (
        ("failed", "confirmed_failed", None),
        ("succeeded", "confirmed_succeeded", "0.40"),
        ("retry", "retry_authorized", None),
        ("abandoned", "abandoned_with_liability", None),
    )

    for suffix, resolution, actual_cost in cases:
        task_id = f"task-restart-{suffix}"
        await first_db.execute(
            "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
            "VALUES (?, 'RECOVERY_REQUIRED', 'supervised', ?, '2026-01-01', '2026-01-01')",
            (task_id, f"restart {suffix}"),
        )
        receipt = await first_store.prepare(
            task_id=task_id,
            request_fingerprint=f"fingerprint-restart-{suffix}",
            request_id=f"call-restart-{suffix}",
            provider="fixture",
            model="fixture-model",
            reservation_amount=Decimal("1.00"),
        )
        attempt_id = str(receipt["attempt_id"])
        await first_budgets.reserve_model_cost(task_id, Decimal("1.00"), reservation_id=attempt_id)
        await first_store.mark_reservation_applied(attempt_id=attempt_id)
        await first_store.mark_provider_outcome_unknown(attempt_id=attempt_id)
        await first_store.resolve_provider_outcome(
            attempt_id=attempt_id,
            resolution=resolution,
            note=f"durable disposition before simulated restart: {suffix}",
            actual_cost=actual_cost,
        )
    await first_db.close()

    second_db = Database(str(path))
    await second_db._ensure_ready()
    second_tasks = TaskStore(second_db)
    second_budgets = BudgetTracker(task_store=second_tasks)
    second_store = ModelResponseStore(second_db)
    retry_calls: list[str] = []

    class RetryKernel:
        async def run_task(self, task_id: str) -> None:
            retry_calls.append(task_id)

    service = SimpleNamespace(
        _model_response_store=second_store,
        _budgets=second_budgets,
        _store_events=None,
        _store_tasks=second_tasks,
        _task_manager=TaskManager(task_store=second_tasks, budgets=second_budgets),
        _kernel=RetryKernel(),
        _approval_recovery_tasks=set(),
        _log_background_failure=AthenaService._log_background_failure,
    )
    service._provider_recovery_runtime = ProviderOutcomeRecoveryAPI.compose(service)
    replay = await AthenaService.reconcile_provider_outcomes(service)
    assert replay == {"replayed": 4, "failed": 0}

    failed = await second_tasks.get("task-restart-failed")
    succeeded = await second_tasks.get("task-restart-succeeded")
    retry = await second_tasks.get("task-restart-retry")
    abandoned = await second_tasks.get("task-restart-abandoned")
    assert failed["status"] == TaskStatus.FAILED.value
    assert succeeded["status"] == TaskStatus.FAILED.value
    assert retry["status"] == TaskStatus.RUNNING.value
    assert abandoned["status"] == TaskStatus.FAILED.value

    success_attempt = next(
        row
        for row in await second_store.list_resolved_attempts()
        if row["task_id"] == "task-restart-succeeded"
    )
    assert success_attempt["budget_accounted_at"] is not None
    assert success_attempt["reservation_released_at"] is not None
    assert (await second_budgets.total("task-restart-succeeded")).cost == Decimal("0.40")
    assert not await second_store.has_unresolved_liability("task-restart-failed")
    assert await second_store.has_unresolved_liability("task-restart-abandoned")
    assert retry_calls == ["task-restart-retry"]

    abandoned_attempt = next(
        row
        for row in await second_store.list_resolved_attempts()
        if row["task_id"] == "task-restart-abandoned"
    )
    closed = await AthenaService.close_provider_liability(
        service,
        str(abandoned_attempt["attempt_id"]),
        note="manual closeout after restart",
    )
    assert closed["liability_closed"] is True
    assert not await second_store.has_unresolved_liability("task-restart-abandoned")
    await second_db.close()


async def test_confirmed_success_rejects_non_finite_actual_cost():
    db = Database(":memory:")
    await db._ensure_ready()
    task_id = "task-provider-invalid-cost"
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES (?, 'RECOVERY_REQUIRED', 'supervised', 'objective', '2026-01-01', '2026-01-01')",
        (task_id,),
    )
    store = ModelResponseStore(db)
    receipt = await store.prepare(
        task_id=task_id,
        request_fingerprint="fingerprint-invalid-cost",
        request_id="call-invalid-cost",
        provider="fixture",
        model="fixture-model",
    )
    attempt_id = str(receipt["attempt_id"])
    await store.mark_provider_outcome_unknown(attempt_id=attempt_id)
    service = SimpleNamespace(
        _model_response_store=store,
        _budgets=None,
        _store_events=None,
        _task_manager=None,
    )
    service._provider_recovery_runtime = ProviderOutcomeRecoveryAPI.compose(service)

    for invalid in ("NaN", "Infinity", "-0.01"):
        with pytest.raises(ValueError):
            await AthenaService.resolve_provider_outcome(
                service,
                attempt_id,
                resolution="confirmed_succeeded",
                note="invalid cost fixture",
                actual_cost=invalid,
            )
    await db.close()


async def test_unknown_liability_rehydrates_against_budget_after_restart(tmp_path):
    path = tmp_path / "unknown-liability.sqlite"
    first_db = Database(str(path))
    await first_db._ensure_ready()
    task_id = "task-unknown-liability"
    await first_db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, resource_budget, created_at, updated_at) "
        "VALUES (?, 'RECOVERY_REQUIRED', 'supervised', 'unknown spend', ?, '2026-01-01', '2026-01-01')",
        (task_id, json.dumps({"max_cost_usd": "1.00"})),
    )
    task_store = TaskStore(first_db)
    budgets = BudgetTracker(task_store=task_store)
    response_store = ModelResponseStore(first_db)
    receipt = await response_store.prepare(
        task_id=task_id,
        request_fingerprint="fingerprint-liability",
        request_id="call-liability",
        provider="fixture",
        model="fixture-model",
        reservation_amount=Decimal("0.75"),
    )
    attempt_id = str(receipt["attempt_id"])
    await budgets.reserve_model_cost(task_id, Decimal("0.75"), reservation_id=attempt_id)
    await response_store.mark_reservation_applied(attempt_id=attempt_id)
    await response_store.mark_provider_outcome_unknown(attempt_id=attempt_id)
    manager = TaskManager(task_store=task_store, budgets=budgets)
    manager.set_model_response_store(response_store)
    await manager._release_model_reservations_if_safe(task_id)
    assert (await budgets.remaining(task_id))["cost_usd"] == Decimal("0.25")
    await first_db.close()

    second_db = Database(str(path))
    await second_db._ensure_ready()
    restarted_budgets = BudgetTracker(task_store=TaskStore(second_db))
    assert (await restarted_budgets.remaining(task_id))["cost_usd"] == Decimal("0.25")
    restarted_store = ModelResponseStore(second_db)
    assert await restarted_store.has_unresolved_liability(task_id)
    await second_db.close()
