from __future__ import annotations

import json
import sqlite3

import pytest

from athena.execution.manager import ExecutionManager
from athena.state.database import Database
from athena.state.runtime_sessions import RuntimeSessionStore
from athena.state.tasks import TaskStore


class _Runtime:
    def __init__(self, name: str) -> None:
        self.name = name

    async def create_session(self, **kwargs):
        return f"{self.name}-session"


class _Backend:
    name = "container"

    async def create_session(self, **kwargs):
        return "container-session"


async def test_runtime_session_persists_backend_and_runtime_independently():
    db = Database(":memory:")
    await db._ensure_ready()
    store = RuntimeSessionStore(db)
    tasks = TaskStore(db)
    for task_id in ("local-python", "local-shell", "container-python"):
        await tasks.insert_task(task_id, None, None, task_id)
    manager = ExecutionManager(runtime_session_store=store)
    manager.register_runtime(_Runtime("python"))
    manager.register_runtime(_Runtime("shell"))
    manager.register_backend(_Backend())

    await manager.create_session(task_id="local-python", runtime="python")
    await manager.create_session(task_id="local-shell", runtime="shell")
    await manager.create_session(
        task_id="container-python",
        runtime="python",
        backend="container",
        cwd="/workspace",
        workspace_root="/tmp/project",
        network_policy="restricted",
    )

    rows = await db.fetch_all(
        "SELECT task_id, backend, runtime, cwd, workspace_identity, network_policy "
        "FROM runtime_sessions ORDER BY task_id"
    )
    assert [(row["backend"], row["runtime"]) for row in rows] == [
        ("container", "python"),
        ("local", "python"),
        ("local", "shell"),
    ]
    assert rows[0]["cwd"] == "/workspace"
    assert rows[0]["workspace_identity"] == "/tmp/project"
    assert rows[0]["network_policy"] == "restricted"
    await db.close()


async def test_runtime_session_redacts_environment_secrets_before_durable_write():
    db = Database(":memory:")
    await db._ensure_ready()
    store = RuntimeSessionStore(db)
    tasks = TaskStore(db)
    await tasks.insert_task("secret-task", None, None, "secret-task")

    await store.start(
        "secret-session",
        task_id="secret-task",
        backend="local",
        runtime="python",
        metadata={
            "environment": {
                "PATH": "/usr/bin",
                "ATHENA_TEST": "1",
                "ATHENA_API_KEY": "do-not-persist",
                "SERVICE_TOKEN": "also-do-not-persist",
            }
        },
    )
    row = await db.fetch_one(
        "SELECT metadata, environment_fingerprint FROM runtime_sessions WHERE id = ?",
        ("secret-session",),
    )
    metadata = json.loads(row["metadata"])
    assert "do-not-persist" not in row["metadata"]
    assert "also-do-not-persist" not in row["metadata"]
    assert "env" not in metadata
    assert metadata["environment"] == {"ATHENA_TEST": "1", "PATH": "/usr/bin"}
    assert metadata["redacted_environment_keys"] == ["ATHENA_API_KEY", "SERVICE_TOKEN"]
    assert row["environment_fingerprint"]

    await store.mark_closed(
        "secret-session",
        metadata={"env": {"PASSWORD": "do-not-persist-on-close", "SAFE": "yes"}},
    )
    closed = await store.get("secret-session")
    assert closed is not None
    assert "do-not-persist-on-close" not in json.dumps(closed["metadata"])
    await db.close()


async def _reincarnation_store(*task_ids: str) -> tuple[Database, RuntimeSessionStore]:
    db = Database(":memory:")
    await db._ensure_ready()
    tasks = TaskStore(db)
    for task_id in task_ids:
        await tasks.insert_task(task_id, None, None, task_id)
    return db, RuntimeSessionStore(db)


async def test_runtime_session_reincarnation_reestablishes_dead_identity():
    """A deterministic session id re-created after restart revives its dead row.

    Recovery marks every non-reattachable live row dead at startup.  When the
    same runtime deterministically re-derives "{runtime}_{task_id}" for the
    same task, the durable ledger must re-establish the live identity instead
    of warning-and-dropping the record: a second crash must still find a live
    row to reconcile (RuntimeStateLost / recovery hints).
    """
    db, store = await _reincarnation_store("t1")

    await store.start("python_t1", task_id="t1", backend="local", runtime="python")
    await store.mark_closed("python_t1")
    before = await store.get("python_t1")
    assert before is not None and before["is_alive"] is False

    await store.start(
        "python_t1",
        task_id="t1",
        backend="local",
        runtime="python",
        pid=4242,
        metadata={"start_identity": "second-incarnation"},
    )
    revived = await store.get("python_t1")
    assert revived is not None
    assert revived["is_alive"] is True
    assert revived["pid"] == 4242
    assert revived["ended_at"] is None
    assert revived["metadata"]["reincarnations"] == 1
    assert revived["metadata"]["previous_ended_at"] == before["ended_at"]
    assert revived["metadata"]["start_identity"] == "second-incarnation"
    assert revived["started_at"] >= before["started_at"]
    await db.close()


async def test_runtime_session_reincarnation_is_counted_repeatedly():
    db, store = await _reincarnation_store("t9")
    for _ in range(3):
        await store.start("shell_t9", task_id="t9", backend="local", runtime="shell")
        await store.mark_closed("shell_t9")
    row = await store.get("shell_t9")
    assert row is not None
    assert row["is_alive"] is False
    assert row["metadata"]["reincarnations"] == 2
    await db.close()


async def test_runtime_session_reincarnation_fails_closed_on_identity_change():
    """Same id with changed workspace binding is a hard conflict, not a revive."""
    db, store = await _reincarnation_store("t1")
    await store.start(
        "python_t1",
        task_id="t1",
        backend="local",
        runtime="python",
        metadata={"workspace_root": "/workspace/a"},
    )
    await store.mark_closed("python_t1")

    with pytest.raises(RuntimeError, match="identity changed across re-establishment"):
        await store.start(
            "python_t1",
            task_id="t1",
            backend="local",
            runtime="python",
            metadata={"workspace_root": "/workspace/b"},
        )
    row = await store.get("python_t1")
    assert row is not None
    assert row["is_alive"] is False
    assert row["workspace_identity"] == "/workspace/a"
    await db.close()


async def test_runtime_session_reincarnation_fails_closed_on_live_row():
    """A live row is never silently superseded: the conflict must propagate."""
    db, store = await _reincarnation_store("t1")
    await store.start("python_t1", task_id="t1", backend="local", runtime="python")

    with pytest.raises(sqlite3.IntegrityError, match="runtime_sessions.id"):
        await store.start("python_t1", task_id="t1", backend="local", runtime="python")
    rows = await db.fetch_all("SELECT * FROM runtime_sessions")
    assert len(rows) == 1
    live = await store.get("python_t1")
    assert live is not None
    assert "metadata" in live and live["metadata"].get("reincarnations") is None
    await db.close()


async def test_runtime_session_reincarnation_fails_closed_on_owner_change():
    """A session id may never migrate to a different task across restart."""
    db, store = await _reincarnation_store("t1", "t2")
    await store.start("python_t1", task_id="t1", backend="local", runtime="python")
    await store.mark_closed("python_t1")

    with pytest.raises(sqlite3.IntegrityError):
        await store.start("python_t1", task_id="t2", backend="local", runtime="python")
    row = await store.get("python_t1")
    assert row is not None
    assert row["task_id"] == "t1"
    await db.close()
