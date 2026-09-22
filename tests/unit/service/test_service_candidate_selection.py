"""Service owns comparison selection; candidates consume exact branch identity."""

from __future__ import annotations


from athena.service.candidates import CandidateService


async def test_service_candidate_selection_delegates_to_fusion(monkeypatch):
    calls = []

    class _Fusion:
        async def select_candidate(self, comparison_id, branch_id):
            calls.append((comparison_id, branch_id))
            return {"selected_branch_id": branch_id}

    class _Service:
        def fusion_orchestrator(self):
            return _Fusion()

    service = CandidateService(_Service())
    result = await service.select_candidate("cmp-1", "branch-older")
    assert result == {"selected_branch_id": "branch-older"}
    assert calls == [("cmp-1", "branch-older")]


async def test_service_candidate_comparison_delegates_to_fusion():
    class _Fusion:
        async def compare(self, *, task_id, proposals, **kwargs):
            return {
                "task_id": task_id,
                "proposals": proposals,
                "kwargs": kwargs,
            }

    class _Service:
        def fusion_orchestrator(self):
            return _Fusion()

    service = CandidateService(_Service())
    result = await service.compare_candidates(
        task_id="task-1",
        proposals=[["a"], ["b"]],
        profile="autonomous",
    )
    assert result["task_id"] == "task-1"
    assert result["kwargs"]["profile"] == "autonomous"


async def test_service_selection_blocks_when_newer_comparison_is_comparing():
    from types import SimpleNamespace

    from athena.service.candidates import CandidateService

    class _Store:
        def __init__(self):
            self.records = {}

        def get(self, comparison_id):
            return self.records.get(comparison_id)

        def latest_for_task(self, task_id):
            values = [r for r in self.records.values() if r.task_id == task_id]
            return values[-1] if values else None

    class _Fusion:
        def __init__(self):
            self.selection_store = _Store()
            self.calls = []

        async def select_candidate(self, comparison_id, branch_id):
            self.calls.append((comparison_id, branch_id))
            return {"ok": True}

    fusion = _Fusion()
    fusion.selection_store.records["cmp-old"] = SimpleNamespace(
        comparison_id="cmp-old", task_id="task-1", lifecycle="SELECTED"
    )
    fusion.selection_store.records["cmp-new"] = SimpleNamespace(
        comparison_id="cmp-new", task_id="task-1", lifecycle="COMPARING"
    )

    class _Service:
        def fusion_orchestrator(self):
            return fusion

    service = CandidateService(_Service())
    import pytest

    with pytest.raises(RuntimeError, match="still COMPARING"):
        await service.select_candidate("cmp-old", "branch-old")
    assert fusion.calls == []
