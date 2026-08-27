"""PID plus process-start identity prevents control races."""

import os

from athena.execution.manager import ExecutionManager
from athena.execution.process_tree import process_start_identity


class _Process:
    pid = os.getpid()

    @staticmethod
    def poll():
        return None


class _Session:
    process = _Process()


class _Runtime:
    def __init__(self) -> None:
        self._sessions = {"session-1": _Session()}


def test_owned_process_requires_matching_start_identity():
    manager = ExecutionManager()
    manager._task_sessions["task-1"] = [(_Runtime(), "session-1")]
    identity = process_start_identity(os.getpid())

    assert identity is not None
    assert manager.owns_process("task-1", os.getpid(), identity)
    assert not manager.owns_process("task-1", os.getpid(), "not-this-process")
