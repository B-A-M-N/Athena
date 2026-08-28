"""Unit tests for athena.causal: TaskForker and CheckpointManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from athena.causal import CheckpointConflict, CheckpointManager, TaskForker
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


@pytest.mark.athena_scenario("FORK-001")
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


async def test_fork_task_creation_failure_removes_speculative_session(svc):
    task = await svc.submit(AgentRequest(prompt="x"), wait=True)
    sessions = svc._sessions
    assert sessions is not None
    before = {row["id"] for row in await sessions.list_all()}

    async def fail_create(_spec):
        raise RuntimeError("simulated task insert failure")

    task_manager = svc._task_manager
    assert task_manager is not None
    original_create = task_manager.create
    task_manager.create = fail_create
    try:
        with pytest.raises(RuntimeError, match="simulated task insert failure"):
            await TaskForker(service=svc).fork(
                task_id=task.id,
                after_event_sequence=1,
            )
    finally:
        task_manager.create = original_create

    after = {row["id"] for row in await sessions.list_all()}
    assert after == before


async def test_timeline_lists_task_events(svc):
    task = await svc.submit(AgentRequest(prompt="x"), wait=True)
    timeline = await TaskForker(service=svc).timeline(task.id)
    assert isinstance(timeline, list) and len(timeline) >= 1
    seqs = [e["sequence"] for e in timeline]
    assert seqs == sorted(seqs)
    for entry in timeline:
        assert {"sequence", "type", "payload_bits"} <= set(entry)


@pytest.mark.athena_scenario("FORK-002")
async def test_checkpoint_capture_and_restore(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "keep.txt").write_text("original")
    (ws / "sub" / "nested.txt").write_text("nested")

    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))
    manifest = await mgr.capture(task_id="task_1", workspace_root=str(ws), label="before-change")
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
    assert not (ws / "extra.txt").exists(), "file added after capture should be removed on restore"


async def test_checkpoint_inspects_immutable_metadata(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "value.txt").write_text("captured")
    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))

    manifest = await mgr.capture(
        task_id="task_1",
        workspace_root=str(ws),
        label="semantic",
        metadata={
            "type": "semantic_state_checkpoint",
            "version": 1,
            "state": {"event_boundary": {"last_sequence": 4}},
        },
    )
    inspected = await mgr.inspect(manifest["id"])

    assert inspected["id"] == manifest["id"]
    assert inspected["metadata"]["type"] == "semantic_state_checkpoint"
    assert inspected["metadata"]["state"]["event_boundary"]["last_sequence"] == 4


async def test_restore_unknown_checkpoint(tmp_path: Path):
    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))
    with pytest.raises(KeyError):
        await mgr.restore("ckpt_missing", str(tmp_path / "ws"))


async def test_checkpoint_restore_rejects_concurrent_workspace_change(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "value.txt").write_text("before")
    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))
    manifest = await mgr.capture(task_id="task_1", workspace_root=str(ws), label="before-change")
    expected = await mgr.fingerprint(str(ws))
    (ws / "value.txt").write_text("concurrent")

    with pytest.raises(CheckpointConflict):
        await mgr.restore(
            manifest["id"],
            str(ws),
            expected_fingerprint=expected,
        )
    assert (ws / "value.txt").read_text() == "concurrent"


async def test_checkpoint_materialize_creates_independent_workspace(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("captured")
    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))
    manifest = await mgr.capture(task_id="task_1", workspace_root=str(source), label="fork-base")

    destination = tmp_path / "fork"
    await mgr.materialize(manifest["id"], str(destination))
    (source / "value.txt").write_text("parent-changed")
    (destination / "value.txt").write_text("fork-changed")

    assert (source / "value.txt").read_text() == "parent-changed"
    assert (destination / "value.txt").read_text() == "fork-changed"


@pytest.mark.asyncio
async def test_checkpoint_owner_refs_survive_and_gc_after_last_release(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "value.txt").write_text("captured")
    root = tmp_path / "ckpts"
    mgr = CheckpointManager(root=str(root))
    manifest = await mgr.capture(
        task_id="task_1",
        workspace_root=str(ws),
        label="owned",
    )
    checkpoint_id = manifest["id"]
    mgr.retain(checkpoint_id, owner="recovery-worker")

    refs = (root / "refs.json").read_text()
    assert '"refcount": 2' in refs
    assert await mgr.release(checkpoint_id, owner="task_1") is False
    assert (root / checkpoint_id).is_dir()
    assert await mgr.release(checkpoint_id, owner="recovery-worker") is True
    assert not (root / checkpoint_id).exists()
    assert not (root / f"{checkpoint_id}.manifest.json").exists()
