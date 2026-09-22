"""Durable candidate-selection lifecycle (review item 7)."""

from __future__ import annotations

from athena.fusion.selection import CandidateSelectionStore


def test_create_select_discard_lifecycle(tmp_path):
    store = CandidateSelectionStore(str(tmp_path))
    record = store.create(
        task_id="task-a",
        candidate_branch_ids=["branch-1", "branch-2", "branch-3"],
        verified_branch_ids=["branch-1", "branch-2"],
        verification_certificates={
            "branch-2": {
                "certificate_hash": "hash-2",
                "candidate_fingerprint": "fp-2",
            }
        },
    )
    assert record.lifecycle == "COMPARING"
    assert record.selected_branch_id is None

    result = store.select(record.comparison_id, "branch-2")
    assert result.selected_branch_id == "branch-2"
    assert result.rejected_branch_ids == ["branch-1"]
    assert result.lifecycle == "SELECTED"
    assert result.selected_at is not None

    assert store.selected_branch_for_task("task-a") == "branch-2"


def test_restart_restores_selected_identity(tmp_path):
    store = CandidateSelectionStore(str(tmp_path))
    record = store.create(
        task_id="task-b",
        candidate_branch_ids=["branch-a", "branch-b"],
        verified_branch_ids=["branch-a", "branch-b"],
        verification_certificates={
            "branch-a": {
                "certificate_hash": "hash-a",
                "candidate_fingerprint": "fp-a",
            }
        },
    )
    store.select(record.comparison_id, "branch-a")

    # Simulate process restart: fresh store, same state file.
    restored = CandidateSelectionStore(str(tmp_path))
    assert restored.selected_branch_for_task("task-b") == "branch-a"
    found = restored.find_by_branch("branch-a")
    assert found is not None
    assert found.lifecycle == "SELECTED"


def test_reselect_conflict_is_rejected(tmp_path):
    store = CandidateSelectionStore(str(tmp_path))
    record = store.create(
        task_id="task-c",
        candidate_branch_ids=["x", "y"],
        verified_branch_ids=["x", "y"],
    )
    store.select(record.comparison_id, "x")
    import pytest

    with pytest.raises(ValueError, match="already selected"):
        store.select(record.comparison_id, "y")


def test_invalid_branch_rejected(tmp_path):
    store = CandidateSelectionStore(str(tmp_path))
    record = store.create(
        task_id="task-d",
        candidate_branch_ids=["x", "y"],
        verified_branch_ids=["x"],
        verification_certificates={"x": {}},
    )
    import pytest

    # y was attempted but failed; z was never attempted.
    with pytest.raises(ValueError, match="not a verified candidate"):
        store.select(record.comparison_id, "y")
    with pytest.raises(ValueError, match="not a verified candidate"):
        store.select(record.comparison_id, "z")
