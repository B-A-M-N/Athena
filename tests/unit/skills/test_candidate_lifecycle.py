"""Durable review lifecycle for learned skill drafts."""

from athena.protocol.messages import TrustClass
from athena.skills.lifecycle import SkillLifecycle
from athena.skills.models import Skill, SkillCandidate
from athena.state.database import Database


def _candidate() -> SkillCandidate:
    return SkillCandidate(
        draft=Skill(
            id="",
            name="release-check-helper",
            description="Run the bounded release checks",
            body="# Release checks\n\nRun the checks and verify the receipts.",
            triggers=("release", "checks"),
            trust=TrustClass.AGENT_CURATED,
        ),
        source_task_id="task-source",
        target_skill=None,
        rationale="The task used a repeatable verified procedure.",
        evidence=("event-1", "receipt-1"),
        confidence=0.86,
    )


async def test_skill_candidate_survives_restart_and_requires_explicit_promotion(tmp_path):
    db_path = tmp_path / "athena.db"
    first_db = Database(str(db_path))
    first = SkillLifecycle(first_db)
    candidate = _candidate()

    assert await first.promote(candidate, task_id="task-source", authorized=False) is None
    pending = await first.list_candidates()
    assert [row["id"] for row in pending] == [candidate.id]
    assert pending[0]["lifecycle_state"] == "PENDING_REVIEW"
    assert pending[0]["evidence"] == ["event-1", "receipt-1"]
    await first_db.close()

    second_db = Database(str(db_path))
    second = SkillLifecycle(second_db)
    try:
        restored = await second.get_candidate(candidate.id)
        assert restored is not None
        assert restored["source_task"] == "task-source"
        assert await second.list(active_only=True) == []

        skill_id = await second.promote_candidate(candidate.id, task_id="operator-task")
        assert skill_id
        active = await second.get(skill_id)
        assert active is not None
        assert active.name == "release-check-helper"
        reviewed = await second.list_candidates(include_reviewed=True)
        assert reviewed[0]["lifecycle_state"] == "PROMOTED"
        assert reviewed[0]["promoted_skill_id"] == skill_id
        assert await second.promote_candidate(candidate.id) is None
    finally:
        await second_db.close()
