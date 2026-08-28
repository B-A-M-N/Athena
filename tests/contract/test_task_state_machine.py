"""Contract: TaskStore.transition enforces LEGAL_TRANSITIONS and lifecycle rules.

Verifies the PROTOCOL state machine, not the store's internals:
  * every legal transition is accepted,
  * every illegal transition is rejected,
  * final statuses have no outgoing transitions,
  * paused statuses can resume to RUNNING,
  * TERMINAL_STATUSES == FINAL_STATUSES,
  * completed_at is written only for final statuses.
"""

from __future__ import annotations

import pytest

from athena.protocol.tasks import (
    FINAL_STATUSES,
    LEGAL_TRANSITIONS,
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
    TaskStatus,
)
from athena.state.tasks import TaskStore

ALL_STATUSES = set(TaskStatus)


@pytest.mark.athena_claim("BHV-014", "BHV-015", "BHV-016", "BHV-024")
@pytest.mark.athena_evidence("test", "invariant")
class TestLegalTransitions:
    async def test_every_legal_transition_is_accepted(self, db):
        store = TaskStore(db)
        idx = 0
        for status, targets in LEGAL_TRANSITIONS.items():
            for new_status in targets:
                tid = f"t-{status.value}-{idx}"
                idx += 1
                await store.insert_task(tid, None, None, "objective", status=status)
                await store.transition(tid, new_status)
                row = await store.get(tid)
                assert row["status"] == new_status

    async def test_every_illegal_transition_is_rejected(self, db):
        store = TaskStore(db)
        for status in ALL_STATUSES:
            allowed = LEGAL_TRANSITIONS.get(status, frozenset())
            for new_status in ALL_STATUSES:
                if new_status in allowed:
                    continue
                tid = f"illegal-{status.value}-{new_status.value}"
                await store.insert_task(tid, None, None, "x", status=status)
                with pytest.raises(ValueError):
                    await store.transition(tid, new_status)
                # state unchanged
                row = await store.get(tid)
                assert row["status"] == status.value

    async def test_unknown_task_raises_keyerror(self, db):
        store = TaskStore(db)
        with pytest.raises(KeyError):
            await store.transition("nope", TaskStatus.RUNNING)


class TestFinalStatuses:
    def test_final_statuses_have_no_outgoing_transitions(self):
        for status in FINAL_STATUSES:
            assert LEGAL_TRANSITIONS.get(status, frozenset()) == frozenset()

    def test_terminal_equals_final(self):
        assert set(TERMINAL_STATUSES) == set(FINAL_STATUSES)


class TestPausedStatuses:
    def test_all_paused_can_resume_to_running(self):
        for status in PAUSED_STATUSES:
            allowed = LEGAL_TRANSITIONS.get(status, frozenset())
            assert TaskStatus.RUNNING in allowed, f"{status} must resume to RUNNING"

    def test_paused_are_not_final(self):
        assert PAUSED_STATUSES.isdisjoint(FINAL_STATUSES)


class TestCompletedAt:
    async def test_not_written_for_paused_transition(self, db):
        store = TaskStore(db)
        await store.insert_task("t-p", None, None, "x", status=TaskStatus.RUNNING)
        await store.transition("t-p", TaskStatus.INTERRUPTED)
        row = await store.get("t-p")
        assert row["status"] == TaskStatus.INTERRUPTED.value
        assert row.get("completed_at") is None

    async def test_written_for_final_transition(self, db):
        store = TaskStore(db)
        await store.insert_task("t-c", None, None, "x", status=TaskStatus.RUNNING)
        await store.transition("t-c", TaskStatus.COMPLETE)
        row = await store.get("t-c")
        assert row["status"] == TaskStatus.COMPLETE.value
        assert row.get("completed_at") is not None
