"""Unit tests for athena.causal: TaskForker and CheckpointManager."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

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


def test_shadow_checkpoint_worker_has_no_state_execution_import_cycle(tmp_path: Path):
    """The isolated clone worker must start from a clean interpreter.

    Importing ``athena.state.database`` first used to enter the eager
    ``athena.execution`` and ``athena.state`` package initializers in a cycle.
    The speculative restart path exercises this worker, so keep the boundary
    regression explicit rather than relying only on the parent process import
    order used by the rest of the suite.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("value", encoding="utf-8")
    state = tmp_path / "state"
    env = os.environ.copy()
    repo_src = str(Path(__file__).parents[3] / "src")
    env["PYTHONPATH"] = os.pathsep.join(item for item in (repo_src, env.get("PYTHONPATH")) if item)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "athena.causal.checkpoint_worker",
            "clone",
            "--root",
            str(state),
            "--checkpoint-id",
            "branch_test",
            "--workspace-root",
            str(source),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout)["base_manifest"]


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


@pytest.mark.asyncio
async def test_checkpoint_classifies_reconstructible_resources(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "state.txt").write_text("state", encoding="utf-8")
    manager = CheckpointManager(str(tmp_path / "checkpoints"))
    captured = await manager.capture(
        task_id="task-resources",
        workspace_root=str(workspace),
        label="resume",
        metadata={
            "resources": [
                {"name": "python", "reattachable": True, "identity": "session-1"},
                {"name": "dependencies", "environment_id": "env-1"},
                {"name": "browser", "recipe": "restore-storage"},
                {"name": "lost-process"},
            ]
        },
    )
    inspected = await manager.inspect(captured["id"])
    states = {item["name"]: item["state"] for item in inspected["metadata"]["resources"]}
    assert states == {
        "python": "reattached",
        "dependencies": "reconstructed",
        "browser": "reconstructed",
        "lost-process": "lost",
    }


@pytest.mark.asyncio
async def test_checkpoint_manifest_carries_recovery_contract_and_typed_outcomes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "state.txt").write_text("state", encoding="utf-8")
    manager = CheckpointManager(str(tmp_path / "checkpoints"))
    captured = await manager.capture(
        task_id="task-manifest",
        workspace_root=str(workspace),
        label="full-manifest",
        metadata={
            "continuation_id": "continuation-7",
            "dependency_environment_id": "env-7",
            "runtime_session_receipts": [{"session_id": "runtime-1", "state": "closed"}],
            "scheduler_checkpoint": {"next_trigger": "2026-09-09T12:00:00Z"},
            "workflow_checkpoint": {"workflow_id": "wf-1", "step": "verify"},
            "browser_storage_state_ref": "secret://browser-state-7",
            "database_checkpoint_receipt": {"wal_frame": 42},
            "world_state_revision": "world-19",
            "non_restorable_obligations": ["live-child-process"],
            "resources": [
                {"name": "reattach", "state": "reattached", "identity": "r-1"},
                {"name": "rebuild", "state": "reconstructed", "recipe": "rebuild"},
                {"name": "stale", "stale": True, "identity": "stale-1"},
                {"name": "conflict", "conflict": True, "identity": "conflict-1"},
                {"name": "lost"},
            ],
        },
    )

    contract = captured["computational_checkpoint"]
    assert contract["task_id"] == "task-manifest"
    assert contract["continuation_id"] == "continuation-7"
    assert contract["workspace_fingerprint"] == captured["workspace_fingerprint"]
    assert contract["dependency_environment_id"] == "env-7"
    assert contract["runtime_session_receipts"] == [{"session_id": "runtime-1", "state": "closed"}]
    assert contract["scheduler_checkpoint"] == {"next_trigger": "2026-09-09T12:00:00Z"}
    assert contract["workflow_checkpoint"] == {"workflow_id": "wf-1", "step": "verify"}
    assert contract["browser_storage_state_ref"] == "secret://browser-state-7"
    assert contract["database_checkpoint_receipt"] == {"wal_frame": 42}
    assert contract["world_state_revision"] == "world-19"
    assert contract["non_restorable_obligations"] == ["live-child-process"]
    assert {item["state"] for item in contract["recovery_outcomes"]} == {
        "reattached",
        "reconstructed",
        "stale",
        "conflict",
        "lost",
    }

    restored = await manager.restore(captured["id"], str(workspace))
    assert restored["recovery_outcomes"] == contract["recovery_outcomes"]
    assert restored["computational_checkpoint"] == contract


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


async def test_checkpoint_fingerprint_ignores_runtime_caches_consistently(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "value.txt").write_text("before")
    (ws / ".mypy_cache").mkdir()
    (ws / ".mypy_cache" / "noise").write_text("ignored")
    mgr = CheckpointManager(root=str(tmp_path / "ckpts"))

    before = await mgr.fingerprint(str(ws))
    manifest = await mgr.capture(task_id="task_1", workspace_root=str(ws), label="consistent")
    assert manifest["workspace_fingerprint"] == before

    (ws / ".mypy_cache" / "noise").write_text("changed")
    assert await mgr.fingerprint(str(ws)) == before
    (ws / "value.txt").write_text("after")
    assert await mgr.fingerprint(str(ws)) != before


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
