"""Exact candidate selection must resist fallback, failure, and tampering."""

from __future__ import annotations

import pytest

from athena.fusion.selection import CandidateSelectionStore


def _store(tmp_path):
    return CandidateSelectionStore(str(tmp_path))


def test_selected_identity_survives_restart_with_certificate_hash(tmp_path):
    store = _store(tmp_path)
    record = store.create(
        task_id="task-integrity",
        candidate_branch_ids=["older", "newer"],
        verified_branch_ids=["older", "newer"],
        verification_certificates={
            "older": {"certificate_hash": "old-hash", "candidate_fingerprint": "old-fp"},
            "newer": {"certificate_hash": "new-hash", "candidate_fingerprint": "new-fp"},
        },
    )
    store.select(record.comparison_id, "older")

    restored = CandidateSelectionStore(str(tmp_path))
    identity = restored.selected_identity_for_task("task-integrity")
    assert identity == {
        "comparison_id": record.comparison_id,
        "branch_id": "older",
        "candidate_fingerprint": "old-fp",
        "certificate_hash": "old-hash",
    }


def test_select_rejects_failed_attempted_branch(tmp_path):
    store = _store(tmp_path)
    record = store.create(
        task_id="task-failed",
        candidate_branch_ids=["good", "bad"],
        verified_branch_ids=["good"],
        verification_certificates={"good": {"certificate_hash": "hash"}},
    )
    with pytest.raises(ValueError, match="not a verified candidate"):
        store.select(record.comparison_id, "bad")
    assert record.lifecycle == "COMPARING"


def test_select_captures_exact_fingerprint_and_hash(tmp_path):
    store = _store(tmp_path)
    record = store.create(
        task_id="task-exact",
        candidate_branch_ids=["a", "b"],
        verified_branch_ids=["a", "b"],
        verification_certificates={
            "a": {"certificate_hash": "hash-a", "candidate_fingerprint": "fp-a"},
            "b": {"certificate_hash": "hash-b", "candidate_fingerprint": "fp-b"},
        },
    )
    result = store.select(record.comparison_id, "b")
    assert result.selected_branch_id == "b"
    assert result.selected_branch_fingerprint == "fp-b"
    assert result.selected_certificate_hash == "hash-b"
