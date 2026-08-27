"""Watcher lifecycle and content-observation contracts."""

from __future__ import annotations

import os

import pytest

from athena.capabilities.watch import WatchCapability, WatchRegistry, _FileWatch
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.tasks import WorkspaceSpec


@pytest.mark.athena_scenario("ENV-003")
def test_file_watch_detects_same_size_edit_with_preserved_timestamp(tmp_path):
    path = tmp_path / "state.txt"
    path.write_text("true", encoding="utf-8")
    original = path.stat()
    watcher = _FileWatch("watch-1", str(tmp_path), "*.txt", "task-1")

    path.write_text("null", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert watcher.poll() == ["state.txt"]


@pytest.mark.asyncio
async def test_process_exit_observation_is_one_shot_and_truthful(monkeypatch):
    registry = WatchRegistry()
    registry.process_watches["watch-1"] = {
        "id": "watch-1",
        "task_id": "task-1",
        "pid": 123,
        "start_identity": "old",
    }
    monkeypatch.setattr(
        "athena.capabilities.watch._process_identity",
        lambda _pid: "new",
    )
    observations = []

    async def sink(event_type, payload, *, task_id):
        observations.append((event_type, payload, task_id))

    assert await registry.poll_all(sink) == 1
    assert observations == [(
        "WatchObserved",
        {
            "watch": "watch-1",
            "kind": "process",
            "pid": 123,
            "exited": True,
            "exit_code": None,
        },
        "task-1",
    )]
    assert await registry.poll_all(sink) == 0


@pytest.mark.asyncio
async def test_watch_path_pattern_and_task_cleanup(tmp_path):
    registry = WatchRegistry()
    capability = WatchCapability(registry)
    context = type("Context", (), {
        "workspace": WorkspaceSpec(id="workspace", root=str(tmp_path)),
    })()
    request = CapabilityRequest(
        capability_id="watch",
        arguments={"operation": "file", "pattern": "*.json"},
        task_id="task-1",
        call_id="watch-call",
    )

    result = await capability.invoke(request, context=context)

    assert result.status is CapabilityResultStatus.OK
    assert len(registry.file_watches) == 1
    watcher = next(iter(registry.file_watches.values()))
    assert watcher.pattern == "*.json"
    registry.remove_task("task-1")
    assert registry.file_watches == {}


def test_watch_registry_close_drops_all_subscriptions(tmp_path):
    registry = WatchRegistry()
    registry.file_watches["file"] = _FileWatch(
        "file", str(tmp_path), "*", "task-1"
    )
    registry.process_watches["process"] = {
        "id": "process", "task_id": "task-1", "pid": 1,
        "start_identity": "1",
    }

    registry.close()

    assert registry.file_watches == {}
    assert registry.process_watches == {}
