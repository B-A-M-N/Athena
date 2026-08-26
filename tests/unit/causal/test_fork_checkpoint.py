"""Unit tests for athena.causal: TaskForker and CheckpointManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from athena.causal import CheckpointManager, TaskForker
from athena.protocol.tasks import AgentRequest
from athena.service.service import AthenaService


@pytest.fixture
async def svc():
    service = AthenaService.in_memory()
    await service.start()
    try:
        yield service
    finally:
        try:
            await service.stop()
        except Exception:
            pass


async def test_fork_creates_new_task_with_metadata(svc):
    task = await svc.submit(AgentRequest(prompt="x"), wait=True)

    forker = TaskForker(service=svc)
    result = await forker.fork(task_id=task.id, after_event_sequence=1)
    assert set(result) == {"fork_id", "parent", "resumed_at_event"}
    assert result["parent"] == task.id
    assert result["resumed_at_event"] == 1

    row = await svc._store_tasks.get(result["fork_id"])
    assert row is not None, "forked task should exist in the task store"
    assert row["id"] != task.id
    assert row["objective"] == task.objective == "x"
    assert row["metadata"]["fork_of"] == task.id
    assert row["metadata"]["fork_after_event"] == 1
    # Fork was enqueued like any other task.
    assert await svc._store_events.last_sequence(result["fork_id"]) > 0


async def test_fork_unknown_task_raises(svc):
    forker = TaskForker(service=svc)
    with pytest.raises(KeyError):
        await forker.fork(task_id="task_does_not_exist", after_event_sequence=0)


async def test_timeline_lists_task_events(svc):
    task = await svc.submit(AgentRequest(prompt="x"), wait=True)
    timeline = await TaskForker(service=svc).timeline(task.id)
    assert isinstance(timeline, list) and len(timeline) >= 1
    seqs = [e["sequence"] for e in timeline]
    assert seqs == sorted(seqs)
    for entry in timeline:
        assert {"sequence", "type", "payload_bits"} <= set(entry)


async def test_checkpoint_capture_and_restore(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "keep.txt").write_text("original")
    (ws / "sub" / "nested.txt").write_text("nested")

    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))
    manifest = await mgr.capture(
        task_id="task_1", workspace_root=str(ws), label="before-change"
    )
    assert manifest["task_id"] == "task_1"
    assert manifest["label"] == "before-change"
    assert manifest["file_count"] == 2
    assert Path(manifest["files"][0]).as_posix() in {"keep.txt", "sub/nested.txt"}

    # Mutate: change one file, delete another, add a new one.
    (ws / "keep.txt").write_text("mutated")
    (ws / "sub" / "nested.txt").unlink()
    (ws / "extra.txt").write_text("added")

    summary = await mgr.restore(manifest["id"], str(ws))
    assert summary["restored_files"] == 2
    assert (ws / "keep.txt").read_text() == "original"
    assert (ws / "sub" / "nested.txt").read_text() == "nested"
    assert not (ws / "extra.txt").exists(), \
        "file added after capture should be removed on restore"


async def test_restore_unknown_checkpoint(tmp_path: Path):
    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))
    with pytest.raises(KeyError):
        await mgr.restore("ckpt_missing", str(tmp_path / "ws"))
