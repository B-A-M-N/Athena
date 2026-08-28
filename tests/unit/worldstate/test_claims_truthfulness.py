"""Claim truthfulness: evidence binding, staleness, projection-not-truth.

Pins contracts of ``athena.worldstate`` that the registered WORLD/CLAIM
scenario tests do not cover:

* a CONTRADICTED or STALE claim is terminal in the registry -- later mutations
  never flip it back to VERIFIED, and re-verification produces a NEW claim
  rather than resurrecting the old one;
* claim flips carry the mutation identity (id/sequence) that caused them and
  survive a full restart through the durable store;
* ``TaskWorldState.snapshot`` is a PROJECTION: its
  ``mutations_since_verified_claim`` counter reads the durable mutation
  ledger, not model assertions, and counts only mutations after the latest
  verified claim's evidence boundary.
"""

from __future__ import annotations

import pytest

from athena.state.database import Database
from athena.state.mutations import COMPLETED, MutationStore
from athena.worldstate import ClaimRegistry, ClaimStatus, WorldStateStore


# ---------------------------------------------------------------------------
# Registry-level truthfulness invariants (in-memory + durable)
# ---------------------------------------------------------------------------


async def test_contradicted_and_stale_claims_are_terminal():
    """Once disproven or invalidated, a claim never returns to VERIFIED."""
    reg = ClaimRegistry()
    contradicted = reg.record(
        text="v1 is correct", evidence={"exit_code": 0}, depends_on_paths=("src/v1.py",)
    )
    stale = reg.record(
        text="docs are current", evidence={"exit_code": 0}, depends_on_paths=("docs/",)
    )
    other = reg.record(text="unrelated", evidence={"exit_code": 0}, depends_on_paths=("other.txt",))

    assert reg.contradict(contradicted.id, "falsified by rerun") is not None
    # Contradicting an unknown claim is a no-op, not an error.
    assert reg.contradict("claim_does_not_exist", "nope") is None

    flipped = reg.invalidate_for_paths(["docs/guide.md"])
    assert [c.id for c in flipped] == [stale.id]
    assert stale.status == ClaimStatus.STALE
    assert contradicted.status == ClaimStatus.CONTRADICTED

    # A second, overlapping mutation must not re-flip non-VERIFIED claims
    # (no duplicate invalidation reasons, no status churn).
    again = reg.invalidate_for_paths(["docs/guide.md", "src/v1.py"])
    assert again == []
    assert len(stale.invalidated_by) == 1
    assert reg.get(other.id).status == ClaimStatus.VERIFIED


async def test_reverification_creates_new_claim_and_does_not_resurrect_old():
    reg = ClaimRegistry()
    old = reg.record(
        text="tests pass", evidence={"exit_code": 0}, depends_on_paths=("src/auth.py",)
    )
    reg.invalidate_for_paths(["src/auth.py"])

    fresh = reg.record(
        text="tests pass (re-verified)",
        evidence={"exit_code": 0},
        depends_on_paths=("src/auth.py",),
    )

    assert fresh.status == ClaimStatus.VERIFIED
    assert old.status == ClaimStatus.STALE, (
        "recording new evidence must not restore trust in the stale claim"
    )


async def test_unscoped_claim_goes_stale_on_any_path_and_reason_carries_mutation():
    reg = ClaimRegistry()
    claim = reg.record(text="workspace is healthy", evidence={})
    flipped = reg.invalidate_for_paths(
        ["somewhere/else.txt"],
        mutation_id="mut-42",
        mutation_sequence=7,
        mutation_event_sequence=99,
    )
    assert [c.id for c in flipped] == [claim.id]
    reason = claim.invalidated_by[0]
    assert reason["paths"] == ["somewhere/else.txt"]
    assert reason["mutation_id"] == "mut-42"
    assert reason["mutation_sequence"] == 7
    assert reason["mutation_event_sequence"] == 99


# ---------------------------------------------------------------------------
# Durability: flips and their reasons survive a restart (file-backed DB)
# ---------------------------------------------------------------------------


async def test_flip_reasons_survive_restart(durable_db_path):
    db1 = Database(durable_db_path)
    reg1 = ClaimRegistry(store=WorldStateStore(db1))
    # Disjoint scopes keep the backgrounded persistence writes disjoint:
    # the invalidation write touches only `a`, the contradiction only `b`
    # (their relative scheduling is otherwise nondeterministic).
    a = reg1.record(
        text="scoped", evidence={"rc": 0}, task_id="t-claims", depends_on_paths=("src/",)
    )
    b = reg1.record(
        text="wrong", evidence={"rc": 0}, task_id="t-claims", depends_on_paths=("docs/",)
    )
    reg1.invalidate_for_paths(
        ["src/a.py"], mutation_id="mut-flip", mutation_sequence=3, mutation_event_sequence=12
    )
    reg1.contradict(b.id, "falsified on rerun")
    await reg1.flush()
    await db1.close()

    db2 = Database(durable_db_path)
    reg2 = ClaimRegistry(store=WorldStateStore(db2))
    assert await reg2.load_from_store("t-claims") == 2
    got_a = reg2.get(a.id)
    got_b = reg2.get(b.id)
    assert got_a.status == ClaimStatus.STALE
    assert got_b.status == ClaimStatus.CONTRADICTED
    reason = got_a.invalidated_by[0]
    assert reason["paths"] == ["src/a.py"]
    assert reason["mutation_id"] == "mut-flip"
    assert reason["mutation_sequence"] == 3
    assert reason["mutation_event_sequence"] == 12
    assert len(got_a.invalidated_by) == 1
    assert "falsified on rerun" in got_b.invalidated_by[0]["because"]
    await db2.close()


# ---------------------------------------------------------------------------
# TaskWorldState is a projection of the durable ledger, not of assertions
# ---------------------------------------------------------------------------

_TASK_ROW = (
    "INSERT INTO tasks(id, status, autonomy, objective, created_at, updated_at) "
    "VALUES ('{tid}', 'RUNNING', 'supervised', 'projection', "
    "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
)


@pytest.fixture
async def svc():
    from athena.service.service import AthenaService

    service = AthenaService.in_memory()
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


async def test_snapshot_counts_only_mutations_after_verified_boundary(svc):
    db = svc._world_state_store._db
    await db.execute(_TASK_ROW.format(tid="t-proj"))
    ledger = MutationStore(db)
    await ledger.record("t-proj", "src/one.py", "write", after_state="1")
    await ledger.record("t-proj", "src/two.py", "write", after_state="2")

    wstate = svc.world_state("t-proj")
    # Claim whose evidence pins the ledger at mutation sequence 2.
    wstate.claims.record(
        text="state verified at seq 2", evidence={"mutation_sequence": 2}, task_id="t-proj"
    )
    await ledger.record("t-proj", "src/three.py", "write", after_state="3")

    snap = await wstate.snapshot()
    # Only the mutation AFTER the verified boundary counts as pressure.
    assert snap["mutations_since_verified_claim"] == 1
    assert snap["last_mutation_sequence"] == 3
    assert [m["operation"] for m in snap["recent_mutations"]] == ["write"] * 3
    assert all(m["status"] == COMPLETED for m in snap["recent_mutations"])


async def test_snapshot_legacy_claim_without_boundary_counts_all_mutations(svc):
    db = svc._world_state_store._db
    await db.execute(_TASK_ROW.format(tid="t-legacy"))
    ledger = MutationStore(db)
    await ledger.record("t-legacy", "src/one.py", "write", after_state="1")
    await ledger.record("t-legacy", "src/two.py", "write", after_state="2")

    wstate = svc.world_state("t-legacy")
    # Legacy claim: no durable verification boundary in its evidence.
    wstate.claims.record(
        text="asserted without boundary", evidence={"exit_code": 0}, task_id="t-legacy"
    )

    snap = await wstate.snapshot()
    assert snap["mutations_since_verified_claim"] == 2


async def test_snapshot_reflects_ledger_not_assertions(svc):
    """A claim the model makes must not change the projected mutation count."""
    db = svc._world_state_store._db
    await db.execute(_TASK_ROW.format(tid="t-ground"))
    ledger = MutationStore(db)
    await ledger.record("t-ground", "src/a.py", "write", after_state="a")

    wstate = svc.world_state("t-ground")
    before = await wstate.snapshot()
    assert before["mutations_since_verified_claim"] == 1

    # The model asserts "everything is verified" -- projection must not care.
    wstate.claims.record(text="all mutations accounted for", evidence={}, task_id="t-ground")
    after_assertion = await wstate.snapshot()
    assert after_assertion["mutations_since_verified_claim"] == 1
    assert len(after_assertion["claims"]) == 1

    # The durable ledger, not the assertion, moves the projection.
    await ledger.record("t-ground", "src/b.py", "write", after_state="b")
    after_mutation = await wstate.snapshot()
    assert after_mutation["mutations_since_verified_claim"] == 2


async def test_snapshot_separates_unknowns_from_contradictions(svc):
    wstate = svc.world_state("t-view")
    # Disjoint scopes: the path mutation can only stale `stale`, never `wrong`.
    stale = wstate.claims.record(
        text="maybe still true", evidence={}, task_id="t-view", depends_on_paths=("src/",)
    )
    wrong = wstate.claims.record(
        text="definitely false", evidence={}, task_id="t-view", depends_on_paths=("docs/",)
    )
    wstate.claims.invalidate_for_paths(["src/x.py"])
    wstate.claims.contradict(wrong.id, "contradicted by evidence")

    snap = await wstate.snapshot()
    assert snap["unknown"] == ["maybe still true"]
    assert [c["id"] for c in snap["contradictions"]] == [wrong.id]
    statuses = {c["id"]: c["status"] for c in snap["claims"]}
    assert statuses[stale.id] == ClaimStatus.STALE
    assert statuses[wrong.id] == ClaimStatus.CONTRADICTED
