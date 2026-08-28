"""Watcher lifecycle and content-observation contracts."""

from __future__ import annotations

import os
from types import SimpleNamespace

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


def test_file_watch_reports_bounded_scan_degradation(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    watcher = _FileWatch(
        "watch-1",
        str(tmp_path),
        "*.txt",
        "task-1",
        max_files=1,
        max_bytes_per_poll=1,
    )

    assert watcher.degraded is True
    assert watcher.scanned_files == 1
    assert watcher.hashed_bytes == 1


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
    assert observations == [
        (
            "WatchObserved",
            {
                "watch": "watch-1",
                "kind": "process",
                "pid": 123,
                "exited": True,
                "exit_code": None,
            },
            "task-1",
        )
    ]
    assert await registry.poll_all(sink) == 0


@pytest.mark.asyncio
async def test_file_watch_observation_includes_root_for_relative_changes(tmp_path):
    path = tmp_path / "state.txt"
    path.write_text("before", encoding="utf-8")
    registry = WatchRegistry()
    watch_id = registry.add_file(
        root=str(tmp_path),
        pattern="*.txt",
        task_id="task-1",
    )
    path.write_text("after", encoding="utf-8")
    observations = []

    async def sink(event_type, payload, *, task_id):
        observations.append((event_type, payload, task_id))

    assert await registry.poll_all(sink) == 1
    assert observations[0][1]["watch"] == watch_id
    assert observations[0][1]["root"] == str(tmp_path)
    assert observations[0][1]["changes"] == ["state.txt"]


@pytest.mark.asyncio
async def test_watch_path_pattern_and_task_cleanup(tmp_path):
    registry = WatchRegistry()
    capability = WatchCapability(registry)
    context = type(
        "Context",
        (),
        {
            "workspace": WorkspaceSpec(id="workspace", root=str(tmp_path)),
        },
    )()
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


@pytest.mark.asyncio
async def test_process_watch_requires_execution_ownership(monkeypatch):
    manager = SimpleNamespace(
        owns_process=lambda task_id, pid, identity: False,
    )
    capability = WatchCapability(execution_manager=manager)
    monkeypatch.setattr("athena.capabilities.watch._process_identity", lambda pid: "start")
    request = CapabilityRequest(
        capability_id="watch",
        arguments={"operation": "process", "pid": os.getpid()},
        task_id="task-1",
        call_id="watch-process",
    )

    result = await capability.invoke(request)

    assert result.status is CapabilityResultStatus.FAILED
    assert "Athena-owned" in (result.error or "")


@pytest.mark.asyncio
async def test_stopping_unknown_watch_is_a_failure():
    capability = WatchCapability()
    result = await capability.invoke(
        CapabilityRequest(
            capability_id="watch",
            task_id="task-1",
            call_id="watch-stop",
            arguments={"operation": "stop", "watch_id": "missing"},
        )
    )

    assert result.status is CapabilityResultStatus.FAILED


def test_watch_registry_close_drops_all_subscriptions(tmp_path):
    registry = WatchRegistry()
    registry.file_watches["file"] = _FileWatch("file", str(tmp_path), "*", "task-1")
    registry.process_watches["process"] = {
        "id": "process",
        "task_id": "task-1",
        "pid": 1,
        "start_identity": "1",
    }

    registry.close()

    assert registry.file_watches == {}
    assert registry.process_watches == {}
