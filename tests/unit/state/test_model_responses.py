from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from athena.protocol.messages import TextBlock
from athena.protocol.models import ModelInfo, ModelResponse, UsageInfo
from athena.protocol.models import ModelRequest
from athena.protocol.tasks import TaskSpec
from athena.kernel.inference_broker import InferenceBroker
from athena.models.router import ModelSelection
from athena.state.database import Database
from athena.state.model_responses import ModelResponseStore
from athena.state.provider_usage import ProviderUsageStore
from athena.state.tasks import TaskStore
from athena.tasks.budgets import BudgetTracker


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
        task_id="task-r",
        request_fingerprint="fingerprint-1",
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


async def test_model_response_completion_is_first_writer_wins():
    db = Database(":memory:")
    await db._ensure_ready()
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('task-first-writer', 'RUNNING', 'supervised', 'objective', '2026-01-01', '2026-01-01')"
    )
    store = ModelResponseStore(db)
    await store.prepare(
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
        task_id="task-first-writer",
        request_fingerprint="fingerprint-first-writer",
        response=first,
    )
    assert not await store.complete(
        task_id="task-first-writer",
        request_fingerprint="fingerprint-first-writer",
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
    await store.prepare(
        task_id="task-f",
        request_fingerprint="fingerprint-f",
        request_id="call-failed",
        provider="fixture",
        model="fixture-model",
    )
    assert await store.fail(task_id="task-f", request_fingerprint="fingerprint-f")
    recovered = await store.prepare(
        task_id="task-f",
        request_fingerprint="fingerprint-f",
        request_id="call-retry",
        provider="fixture",
        model="fixture-model",
    )
    assert recovered["status"] == "PENDING"
    assert recovered["request_id"] == "call-retry"
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
    await first_store.prepare(
        task_id="task-restart",
        request_fingerprint="fingerprint-restart",
        request_id="call-original",
        provider="fixture",
        model="fixture-model",
    )
    await first_store.complete(
        task_id="task-restart",
        request_fingerprint="fingerprint-restart",
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
    await store.prepare(
        task_id="task-markers",
        request_fingerprint="fingerprint-markers",
        request_id="call-markers",
        provider="fixture",
        model="fixture-model",
    )
    await store.complete(
        task_id="task-markers",
        request_fingerprint="fingerprint-markers",
        response=ModelResponse(
            request_id="call-markers",
            provider="fixture",
            model="fixture-model",
            blocks=(TextBlock(text="markers"),),
        ),
    )

    assert await store.mark_accounting_applied(
        task_id="task-markers", request_fingerprint="fingerprint-markers"
    )
    assert not await store.mark_accounting_applied(
        task_id="task-markers", request_fingerprint="fingerprint-markers"
    )
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
    await store.mark_reservation_applied(
        task_id=task.id, request_fingerprint="fingerprint-reconcile"
    )
    usage_id = await usage.record_attempt(
        provider="fixture", model="fixture-model", task_id=task.id, usage_id=attempt_id
    )
    await store.set_provider_usage_id(
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile",
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
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile",
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
        task_id=task.id,
        request_fingerprint="fingerprint-reconcile-missing",
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
