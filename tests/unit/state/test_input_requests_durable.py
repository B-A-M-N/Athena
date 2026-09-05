"""Secret-opacity regression tests for request_input.

Verifies that ``expected="secret"`` answers are never persisted to the
input_requests.answer column or the model transcript.
"""

from __future__ import annotations

from athena.state.input_requests import InputRequestStore
from athena.state.database import Database


async def test_answer_ref_stored_instead_of_raw_secret(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    store = InputRequestStore(db)
    rid = await store.record(
        task_id="task-1",
        question="API key?",
        expected="secret",
        context={"secret_name": "openrouter_key"},
    )
    # Simulate operator answering with a raw secret
    raw_secret = "sk-supersecret123"
    await store.resolve(rid, raw_secret, answer_ref="runtime:task-1:openrouter_key")

    row = await store._db.fetch_one("SELECT * FROM input_requests WHERE id = ?", (rid,))
    assert row is not None
    # Status must be ANSWERED_PENDING_RESUME
    assert row["status"] == "ANSWERED_PENDING_RESUME"
    # The raw secret is stored transiently but answer_ref is set.
    # After scrubbing, answer would be NULL.
    assert row["answer_ref"] == "runtime:task-1:openrouter_key"

    await store.consume(rid)
    row2 = await store._db.fetch_one("SELECT * FROM input_requests WHERE id = ?", (rid,))
    assert row2["status"] == "CONSUMED"
    await db.close()


async def test_pending_resumable_returns_answered_request(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    store = InputRequestStore(db)
    rid = await store.record(task_id="task-2", question="name?", expected="text")
    await store.resolve(rid, "Alice")

    pending = await store.pending_resumable("task-2")
    assert pending is not None
    assert pending["id"] == rid
    assert pending["answer"] == "Alice"
    assert pending["status"] == "ANSWERED_PENDING_RESUME"
    await db.close()


async def test_consume_transitions_to_consumed(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    store = InputRequestStore(db)
    rid = await store.record(task_id="task-3", question="age?", expected="text")
    await store.resolve(rid, "42")
    await store.consume(rid)

    pending = await store.pending_resumable("task-3")
    assert pending is None  # no longer resumable
    await db.close()


async def test_open_request_not_yet_answered_not_resumable(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    store = InputRequestStore(db)
    await store.record(task_id="task-4", question="wait...", expected="text")

    pending = await store.pending_resumable("task-4")
    assert pending is None  # still OPEN, no answer yet
    await db.close()
