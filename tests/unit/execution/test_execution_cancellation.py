from __future__ import annotations

import pytest

from athena.execution.manager import ExecutionManager, RuntimeCancellationResult


class _Runtime:
    name = "fake"

    def __init__(self, *, fail_close: bool = False, fail_cancel: bool = False) -> None:
        self.fail_close = fail_close
        self.fail_cancel = fail_cancel
        self.closed: list[str] = []
        self.cancelled: list[str] = []

    async def close(self, session_id: str) -> None:
        if self.fail_close:
            raise RuntimeError(f"close failed for {session_id}")
        self.closed.append(session_id)

    async def cancel_task(self, task_id: str) -> None:
        self.cancelled.append(task_id)
        if self.fail_cancel:
            raise RuntimeError(f"task cancel failed for {task_id}")


@pytest.mark.asyncio
async def test_real_execution_manager_preserves_remaining_session_for_retry() -> None:
    manager = ExecutionManager()
    first = _Runtime()
    second = _Runtime(fail_close=True)
    manager._cancellation_registry.task_sessions["task-1"] = [
        (first, "session-1"),
        (second, "session-2"),
    ]
    manager._cancellation_registry.runtime_by_session.update(
        {"session-1": first, "session-2": second}
    )

    with pytest.raises(RuntimeError, match="close failed"):
        await manager.cancel_task("task-1")

    assert first.closed == ["session-1"]
    assert manager._cancellation_registry.task_sessions["task-1"] == [(second, "session-2")]

    second.fail_close = False
    result = await manager.cancel_task("task-1")

    assert isinstance(result, RuntimeCancellationResult)
    assert result.confirmed is True
    assert first.closed == ["session-1"]
    assert second.closed == ["session-2"]
    assert "task-1" not in manager._cancellation_registry.task_sessions


@pytest.mark.asyncio
async def test_real_execution_manager_does_not_reclose_when_persisting_close_fails() -> None:
    class _SessionStore:
        def __init__(self) -> None:
            self.fail = True
            self.closed: list[str] = []

        async def mark_closed(self, session_id: str) -> None:
            self.closed.append(session_id)
            if self.fail:
                raise RuntimeError("session persistence failed")

    store = _SessionStore()
    runtime = _Runtime()
    manager = ExecutionManager(runtime_session_store=store)
    manager._cancellation_registry.task_sessions["task-2"] = [(runtime, "session-2")]
    manager._cancellation_registry.runtime_by_session["session-2"] = runtime

    with pytest.raises(RuntimeError, match="session persistence failed"):
        await manager.cancel_task("task-2")
    assert runtime.closed == ["session-2"]

    store.fail = False
    result = await manager.cancel_task("task-2")

    assert result.confirmed is True
    assert runtime.closed == ["session-2"]
    assert runtime.cancelled == ["task-2"]
    assert store.closed == ["session-2", "session-2"]


@pytest.mark.asyncio
async def test_destroy_session_retries_persistence_without_reclosing() -> None:
    class _SessionStore:
        def __init__(self) -> None:
            self.fail = True

        async def mark_closed(self, session_id: str) -> None:
            if self.fail:
                raise RuntimeError("session persistence failed")

    store = _SessionStore()
    runtime = _Runtime()
    manager = ExecutionManager(runtime_session_store=store)
    manager._cancellation_registry.task_sessions["task-2b"] = [(runtime, "session-2b")]
    manager._cancellation_registry.runtime_by_session["session-2b"] = runtime

    with pytest.raises(RuntimeError, match="session persistence failed"):
        await manager.cancel_task("task-2b")

    store.fail = False
    await manager.destroy_session("session-2b")

    assert runtime.closed == ["session-2b"]
    assert runtime.cancelled == ["task-2b"]
    assert "task-2b" not in manager._cancellation_registry.task_sessions


@pytest.mark.asyncio
async def test_destroy_session_direct_retry_does_not_reclose_runtime() -> None:
    class _SessionStore:
        def __init__(self) -> None:
            self.fail = True

        async def mark_closed(self, session_id: str) -> None:
            if self.fail:
                raise RuntimeError(f"session persistence failed for {session_id}")

    store = _SessionStore()
    runtime = _Runtime()
    manager = ExecutionManager(runtime_session_store=store)
    manager._cancellation_registry.task_sessions["task-direct"] = [(runtime, "session-direct")]
    manager._cancellation_registry.runtime_by_session["session-direct"] = runtime

    with pytest.raises(RuntimeError, match="session persistence failed"):
        await manager.destroy_session("session-direct")

    store.fail = False
    await manager.destroy_session("session-direct")

    assert runtime.closed == ["session-direct"]
    assert "task-direct" not in manager._cancellation_registry.task_sessions


@pytest.mark.asyncio
async def test_real_execution_manager_retries_failed_runtime_cancellation() -> None:
    manager = ExecutionManager()
    runtime = _Runtime(fail_cancel=True)
    manager._cancellation_registry.task_sessions["task-3"] = [(runtime, "session-3")]
    manager._cancellation_registry.runtime_by_session["session-3"] = runtime

    with pytest.raises(RuntimeError, match="task cancel failed"):
        await manager.cancel_task("task-3")
    assert manager.has_live_runtime("task-3") is True

    runtime.fail_cancel = False
    result = await manager.cancel_task("task-3")

    assert result.confirmed is True
    assert runtime.closed == ["session-3"]
    assert runtime.cancelled == ["task-3", "task-3"]


@pytest.mark.asyncio
async def test_destroy_session_keeps_ownership_when_backend_close_fails() -> None:
    manager = ExecutionManager()
    runtime = _Runtime(fail_close=True)
    manager._cancellation_registry.task_sessions["task-4"] = [(runtime, "session-4")]
    manager._cancellation_registry.runtime_by_session["session-4"] = runtime

    with pytest.raises(RuntimeError, match="close failed"):
        await manager.destroy_session("session-4")

    assert manager._cancellation_registry.task_sessions["task-4"] == [(runtime, "session-4")]
    assert manager._cancellation_registry.runtime_by_session["session-4"] is runtime


@pytest.mark.asyncio
async def test_cancel_task_reconciles_session_adopted_by_late_execution() -> None:
    """Cancellation closes a session adopted before its final event arrives."""
    manager = ExecutionManager()
    runtime = _Runtime()
    manager._cancellation_registry.executions["execution-race"] = "task-race"
    manager._cancellation_registry.exec_runtimes["execution-race"] = (runtime, "session-race")
    manager._cancellation_registry.runtime_by_session["session-race"] = runtime

    result = await manager.cancel_task("task-race")

    assert result.confirmed is True
    assert result.closed_sessions == ("session-race",)
    assert runtime.closed == ["session-race"]
    assert runtime.cancelled == ["task-race"]
