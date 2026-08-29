import pytest

from athena.recovery.manager import RecoveryManager


@pytest.mark.asyncio
async def test_runtime_restart_emits_explicit_state_loss_event():
    class Tasks:
        def __init__(self):
            self.hints = []

        async def persist_runtime_recovery_hint(self, task_id, **kwargs):
            self.hints.append((task_id, kwargs))

    class RuntimeSessions:
        async def list_alive(self):
            return [{"id": "runtime-1", "task_id": "task-1", "backend": "python"}]

        async def mark_dead(self, session_id):
            assert session_id == "runtime-1"

    class Events:
        def __init__(self):
            self.calls = []

        async def append_event(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    events = Events()
    tasks = Tasks()
    manager = RecoveryManager(
        task_store=tasks,
        runtime_session_store=RuntimeSessions(),
        event_store=events,
    )

    assert await manager._recover_runtime_sessions() == 1
    assert events.calls == [
        (
            (
                "RuntimeStateLost",
                {
                    "runtime_session_id": "runtime-1",
                    "backend": "python",
                    "reason": "Athena restarted without a reattachable runtime process",
                },
            ),
            {"task_id": "task-1"},
        )
    ]
    assert tasks.hints == [
        (
            "task-1",
            {"runtime_session_id": "runtime-1", "backend": "python"},
        )
    ]
