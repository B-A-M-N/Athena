"""Selected candidate identity is exact; missing branches refuse to fall back."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.service.candidates import CandidateService


class _Shadow:
    def __init__(self, branches):
        self._branches = {branch.id: branch for branch in branches}

    def list_branches(self):
        return list(self._branches.values())

    def get_branch(self, branch_id):
        return self._branches.get(branch_id)


def _service_with(branches, identity):
    fusion = SimpleNamespace(
        selection_store=SimpleNamespace(selected_identity_for_task=lambda task_id: identity)
    )
    svc = SimpleNamespace(shadow_engine=lambda: _Shadow(branches), _fusion=fusion)
    return CandidateService(svc)


def test_exact_selected_branch_wins_over_latest():
    older = SimpleNamespace(id="older", task_id="t", status="VERIFIED")
    newer = SimpleNamespace(id="newer", task_id="t", status="VERIFIED")
    service = _service_with(
        [older, newer],
        {
            "comparison_id": "cmp",
            "branch_id": "older",
            "candidate_fingerprint": "fp",
            "certificate_hash": "hash",
        },
    )
    assert service._candidate_branch("t") is older


def test_missing_selected_branch_raises_instead_of_falling_back():
    later = SimpleNamespace(id="later", task_id="t", status="VERIFIED")
    service = _service_with(
        [later],
        {
            "comparison_id": "cmp",
            "branch_id": "selected",
            "candidate_fingerprint": "fp",
            "certificate_hash": "hash",
        },
    )
    with pytest.raises(RuntimeError, match="selection_integrity_error"):
        service._candidate_branch("t")


def test_non_verified_selected_branch_raises():
    branch = SimpleNamespace(id="selected", task_id="t", status="CONFLICTED")
    service = _service_with(
        [branch],
        {
            "comparison_id": "cmp",
            "branch_id": "selected",
            "candidate_fingerprint": None,
            "certificate_hash": None,
        },
    )
    with pytest.raises(RuntimeError, match="selection_integrity_error"):
        service._candidate_branch("t")
