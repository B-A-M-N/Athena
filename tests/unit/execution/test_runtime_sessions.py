from __future__ import annotations

import json

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
