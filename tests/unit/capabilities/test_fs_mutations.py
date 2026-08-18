from __future__ import annotations

import pytest

from athena.artifacts.store import ArtifactStore
from athena.capabilities.fs import FilesystemCapability
from athena.protocol.capabilities import CapabilityRequest, CapabilityResultStatus
from athena.protocol.ids import new_id
from athena.protocol.tasks import PathRule, WorkspaceSpec
from athena.state.database import Database
from athena.state.mutations import (
    COMPLETED,
    RECOVERY_REQUIRED,
    MutationStore,
)

_TASK = (
    "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
    "VALUES ('t1', 'RUNNING', 'supervised', 'o', '2020-01-01T00:00:00Z', "
    "'2020-01-01T00:00:00Z')"
)


def _req(**args) -> CapabilityRequest:
    r = CapabilityRequest(capability_id="fs", arguments=args, task_id="t1")
    object.__setattr__(r, "call_id", new_id("call"))
    return r


def _ws(tmp_path) -> WorkspaceSpec:
    return WorkspaceSpec(id="w", root=str(tmp_path),
                         writable=(PathRule(str(tmp_path)),))


class _RecordingStore(MutationStore):
    """Wraps the real store and records the order of phase transitions."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.events: list[str] = []
        self.fail_complete = False

    async def record_intent(self, *a, **k) -> str:
        self.events.append(("intent", k.get("before_ref")))
        return await super().record_intent(*a, **k)

    async def mark_started(self, mid) -> None:
        self.events.append(("started", mid))
        await super().mark_started(mid)

    async def complete(self, *a, **k) -> None:
        self.events.append(("complete", k.get("after_hash")))
        if self.fail_complete:
            raise RuntimeError("simulated completion failure")
        return await super().complete(*a, **k)

    async def mark_recovery_required(self, mid) -> None:
        self.events.append(("recovery", mid))
        return await super().mark_recovery_required(mid)


@pytest.fixture
async def env(tmp_path):
    db = Database(":memory:")
    await db.execute(_TASK)
    yield tmp_path, db
    await db.close()


async def _fs(tmp_path, db, *, artifact=True, recording=False):
    as_store = ArtifactStore(tmp_path / "art") if artifact else None
    underlying = _RecordingStore(db) if recording else MutationStore(db)
    fs = FilesystemCapability(
        workspace=_ws(tmp_path),
        artifact_store=as_store,
        mutation_store=underlying,
    )
    return fs, underlying


async def test_write_overwrite_snapshots_before_as_artifact(env):
    tmp_path, db = env
    target = tmp_path / "a.txt"
    target.write_text("old")
    fs, store = await _fs(tmp_path, db)
    await fs.invoke(_req(operation="write", path=str(target), content="new"))
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["status"] == COMPLETED
    assert row["before_ref"] is not None
    assert row["before_ref"].startswith("artifact://sha256/")
    assert row["after_state"] is not None
    assert row["reversible"] == 1
    rev = await store.get(row["id"])
    assert rev["inverse"]["op"] == "restore_from_ref"
    assert rev["inverse"]["ref"] == row["before_ref"]


async def test_write_create_has_no_before_ref_but_is_reversible(env):
    tmp_path, db = env
    target = tmp_path / "new.txt"
    fs, store = await _fs(tmp_path, db)
    await fs.invoke(_req(operation="write", path=str(target), content="hello"))
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["before_ref"] is None
    assert row["reversible"] == 1
    rev = await store.get(row["id"])
    assert rev["inverse"]["op"] == "delete"


async def test_patch_captures_before_artifact(env):
    tmp_path, db = env
    target = tmp_path / "p.txt"
    target.write_text("v1")
    fs, _ = await _fs(tmp_path, db)
    await fs.invoke(_req(operation="patch", path=str(target), new_content="v2"))
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["status"] == COMPLETED
    assert row["before_ref"] is not None
    assert row["reversible"] == 1


async def test_delete_reversible_only_when_snapshot_succeeded(env):
    tmp_path, db = env
    target = tmp_path / "d.txt"
    target.write_text("data")
    fs, store = await _fs(tmp_path, db, artifact=True)
    r = await fs.invoke(_req(operation="delete", path=str(target)))
    assert r.status == CapabilityResultStatus.OK
    row = await store.get((await store.list_for_task("t1"))[0]["id"])
    assert row["before_ref"] is not None
    assert row["reversible"] is True
    assert row["inverse"]["op"] == "create_from_ref"


async def test_delete_snapshot_failure_is_not_reversible(env):
    tmp_path, db = env
    target = tmp_path / "d.txt"
    target.write_text("data")
    fs, _ = await _fs(tmp_path, db, artifact=False)
    r = await fs.invoke(_req(operation="delete", path=str(target)))
    assert r.status == CapabilityResultStatus.OK
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["before_ref"] is None
    assert row["reversible"] == 0


async def test_delete_refuses_directory(env):
    tmp_path, db = env
    d = tmp_path / "adir"
    d.mkdir()
    fs, _ = await _fs(tmp_path, db)
    r = await fs.invoke(_req(operation="delete", path=str(d)))
    assert r.status == CapabilityResultStatus.FAILED
    rows = await db.fetch_all("SELECT * FROM mutations")
    assert rows == []


async def test_write_ahead_intent_precedes_side_effect(env):
    tmp_path, db = env
    target = tmp_path / "wal.txt"
    target.write_text("old")
    seen = {}
    fs, store = await _fs(tmp_path, db, recording=True)
    original = store.record_intent
    async def wrap(*a, **k):
        seen["at_intent"] = target.read_text()
        return await original(*a, **k)
    store.record_intent = wrap
    await fs.invoke(_req(operation="write", path=str(target), content="new"))
    assert seen["at_intent"] == "old"
    assert target.read_text() == "new"
    (recorded,) = await db.fetch_all("SELECT status, after_state FROM mutations")
    assert recorded["status"] == COMPLETED
    assert recorded["after_state"] is not None


async def test_completion_failure_marks_recovery_required_and_raises(env):
    tmp_path, db = env
    target = tmp_path / "r.txt"
    target.write_text("before")
    fs, store = await _fs(tmp_path, db, recording=True)
    store.fail_complete = True
    with pytest.raises(RuntimeError):
        await fs.invoke(_req(operation="write", path=str(target), content="after"))
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["status"] == RECOVERY_REQUIRED
    assert target.exists() and target.read_text() == "after"


async def test_move_snapshots_source_and_destination(env):
    tmp_path, db = env
    src = tmp_path / "src.txt"
    src.write_text("mv")
    dest = tmp_path / "dest.txt"
    fs, _ = await _fs(tmp_path, db)
    r = await fs.invoke(_req(operation="move", path=str(src),
                             destination=str(dest)))
    assert r.status == CapabilityResultStatus.OK
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["resource"] == str(src)
    assert row["before_ref"] is not None
    assert not src.exists()
    assert dest.read_text() == "mv"


async def test_copy_captures_destination_before_state(env):
    tmp_path, db = env
    src = tmp_path / "src.txt"
    src.write_text("payload")
    dest = tmp_path / "dest.txt"
    dest.write_text("holder")
    fs, _ = await _fs(tmp_path, db)
    r = await fs.invoke(_req(operation="copy", path=str(src),
                             destination=str(dest)))
    assert r.status == CapabilityResultStatus.OK
    row = await db.fetch_one("SELECT * FROM mutations")
    assert row["resource"] == str(dest)
    assert row["before_ref"] is not None
    assert row["reversible"] == 1