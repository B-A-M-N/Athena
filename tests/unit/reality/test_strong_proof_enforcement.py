"""Strong verification cannot implicitly downgrade to standard."""

from __future__ import annotations


from athena.reality.candidate_verification import CandidateVerificationService


class _Verifier:
    async def verify_against(self, task, criteria, workspace):
        return [{"id": criterion.id, "passed": True} for criterion in criteria]


def _task():
    from athena.protocol.tasks import Criterion

    return (Criterion(id="explicit", description="explicit", required=True),)


async def test_strong_with_single_category_fails_closed(tmp_path):
    svc = CandidateVerificationService(candidate_verifier=_Verifier())
    svc._plans["t"] = {
        "required_strength": "strong",
        "criterion_categories": {"project_default:1": "test"},
    }
    from athena.protocol.tasks import TaskSpec, WorkspaceSpec

    task = TaskSpec(id="t", objective="x", acceptance_criteria=_task())
    results = await svc.verify_or_fail(task, _task(), WorkspaceSpec(id="w", root="/tmp"))
    ids = [item["id"] for item in results]
    assert "verification_strength_unavailable" in ids
    strength = svc.strength_for("t")
    assert strength["achieved_strength"] == "none"
