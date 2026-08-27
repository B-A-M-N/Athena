"""Durable world-state store (review items 36+38)."""

import asyncio
import json

import pytest

from athena.state.database import Database
from athena.state.mutations import COMPLETED, MutationStore
from athena.worldstate import ClaimRegistry, ClaimStatus, WorldStateStore


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "worldstate.db")


async def _open(path):
    return Database(path)


async def test_persistence_across_restart(db_path):
    db1 = await _open(db_path)
    store1 = WorldStateStore(db1)
    cid = await store1.save_claim({
        "id": "claim-1",
        "task_id": "t1",
        "text": "all tests pass",
        "status": "VERIFIED",
        "evidence": {"exit_code": 0, "command": "pytest -q"},
        "depends_on_paths": ["src/"],
    })
    assert cid == "claim-1"
    rows = await store1.claims_for_task("t1")
    assert len(rows) == 1
    assert rows[0]["evidence"]["exit_code"] == 0
    assert rows[0]["status"] == "VERIFIED"
    await db1.close()

    # Restart: fresh Database + store on the same file.
    db2 = await _open(db_path)
    store2 = WorldStateStore(db2)
    rows = await store2.claims_for_task("t1")
    assert len(rows) == 1
    assert rows[0]["id"] == "claim-1"
    assert rows[0]["text"] == "all tests pass"
    assert tuple(rows[0]["depends_on_paths"]) == ("src/",)
    await db2.close()


async def test_invalidation_flip_persisted_and_scoped(db_path):
    db = await _open(db_path)
    store = WorldStateStore(db)
    scoped = await store.save_claim({
        "id": "c-scoped", "task_id": "t1", "text": "scoped claim",
        "status": "VERIFIED", "evidence": {},
        "depends_on_paths": ["src/athena"],
    })
    unscoped = await store.save_claim({
        "id": "c-unscoped", "task_id": "t1", "text": "unscoped claim",
        "status": "VERIFIED", "evidence": {}, "depends_on_paths": [],
    })
    other = await store.save_claim({
        "id": "c-other", "task_id": "t2", "text": "other task",
        "status": "VERIFIED", "evidence": {},
        "depends_on_paths": ["src/athena"],
    })

    flipped = await store.invalidate_for_paths(
        "t1", ["src/athena/store.py"],
        mutation_id="mut-1", mutation_sequence=4,
        mutation_event_sequence=11)
    # prefix semantics match core._paths_overlap; unscoped claims depend on all
    assert set(flipped) == {scoped, unscoped}
    assert other not in flipped

    rows = {r["id"]: r for r in await store.claims_for_task()}
    assert rows[scoped]["status"] == "STALE"
    assert rows[unscoped]["status"] == "STALE"
    assert rows[other]["status"] == "VERIFIED"
    assert rows[scoped]["invalidated_by"], "invalidation reason recorded"

    inv = await db.fetch_all(
        "SELECT * FROM claim_invalidations WHERE claim_id = ?", (scoped,))
    reason = json.loads(inv[0]["reason"])
    assert reason["paths"] == ["src/athena/store.py"]
    assert reason["mutation_id"] == "mut-1"
    assert reason["mutation_sequence"] == 4
    assert reason["mutation_event_sequence"] == 11

    # Already-STALE claims are not re-flipped.
    assert await store.invalidate_for_paths("t1", ["src/"]) == []
    await db.close()


async def test_mark_contradicted(db_path):
    db = await _open(db_path)
    store = WorldStateStore(db)
    cid = await store.save_claim({
        "id": "c-1", "task_id": "t1", "text": "x", "status": "VERIFIED",
        "evidence": {}, "depends_on_paths": [],
    })
    assert await store.mark_contradicted(cid, "test failed on rerun") is True
    rows = await store.claims_for_task("t1")
    assert rows[0]["status"] == "CONTRADICTED"
    assert await store.mark_contradicted("missing-id", "nope") is False
    await db.close()


async def test_registry_with_store_records_and_reloads(db_path):
    db1 = await _open(db_path)
    reg1 = ClaimRegistry(store=WorldStateStore(db1))
    a = reg1.record(text="tests pass", evidence={"exit_code": 0},
                    task_id="t9", depends_on_paths=("tests/",))
    b = reg1.record(text="lint clean", evidence={"rc": 0}, task_id="t9",
                    depends_on_paths=("docs/",))
    flipped = reg1.invalidate_for_paths(["tests/test_x.py"])
    assert [c.id for c in flipped] == [a.id]
    reg1.contradict(b.id, "contradicted later")
    await reg1.flush()  # drain background persistence writes
    await db1.close()

    # Restart: new registry over the same file hydrates from the store.
    db2 = await _open(db_path)
    reg2 = ClaimRegistry(store=WorldStateStore(db2))
    n = await reg2.load_from_store("t9")
    assert n == 2
    got_a = reg2.get(a.id)
    assert got_a is not None
    assert got_a.status == ClaimStatus.STALE
    assert got_a.evidence == {"exit_code": 0}
    assert got_a.invalidated_by and "paths" in got_a.invalidated_by[0]
    assert reg2.get(b.id).status == ClaimStatus.CONTRADICTED

    # New records still persist through the reloaded registry.
    c = reg2.record(text="post-restart", evidence={}, task_id="t9")
    await reg2.flush()
    ids = {r["id"] for r in await reg2._store.claims_for_task("t9")}
    assert {a.id, b.id, c.id} <= ids
    await db2.close()


async def test_registry_without_store_is_in_memory():
    """Backward compat: no store -> pure in-memory, sync-only usage."""
    reg = ClaimRegistry()
    claim = reg.record(text="x", evidence={})  # no loop needed
    assert reg.get(claim.id) is claim
    assert reg.invalidate_for_paths(["anything"]) == [claim]
    await asyncio.sleep(0)  # nothing scheduled to persist


async def test_mutations_get_durable_task_sequences(db_path):
    db = await _open(db_path)
    # The mutations table enforces FK on task_id; create the parent row the
    # same way tests/unit/capabilities/test_fs_mutations.py does.
    await db.execute(
        "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
        "VALUES ('t-seq', 'RUNNING', 'supervised', 'sequences', "
        "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
    )
    mutations = MutationStore(db)
    first = await mutations.record(
        "t-seq", "src/a.py", "write", after_state="a")
    second = await mutations.record(
        "t-seq", "src/b.py", "write", after_state="b")
    assert await mutations.sequence_for(first) == 1
    assert await mutations.sequence_for(second) == 2
    rows = await mutations.list_for_task("t-seq")
    assert [row["sequence"] for row in rows] == [1, 2]
    assert all(row["status"] == COMPLETED for row in rows)
    await db.close()


async def test_invariant_definitions_and_results_survive_store_reload(db_path):
    db = await _open(db_path)
    store = WorldStateStore(db)
    from athena.worldstate import InvariantSet

    invariants = InvariantSet(task_id="t-invariant", store=store)

    async def probe():
        return True

    invariant_id = invariants.add(
        "workspace remains valid", probe,
        definition={"type": "command", "command": "test -d /workspace"},
    )
    report = await invariants.check_all()
    assert report["ok"] is True
    definitions = await store.invariants_for_task("t-invariant")
    results = await store.invariant_results_for_task("t-invariant")
    assert definitions[0]["id"] == invariant_id
    assert definitions[0]["definition"]["type"] == "command"
    assert results[0]["invariant_id"] == invariant_id
    assert results[0]["passed"] is True
    await db.close()
