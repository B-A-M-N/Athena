from __future__ import annotations

from athena.protocol.messages import TextBlock
from athena.protocol.models import ModelResponse, UsageInfo
from athena.state.database import Database
from athena.state.model_responses import ModelResponseStore


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
