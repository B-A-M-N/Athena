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


async def test_skill_refinement_target_and_version_survive_restart(tmp_path):
    db_path = tmp_path / "athena.db"
    first_db = Database(str(db_path))
    first = SkillLifecycle(first_db)
    skill_id = await first.install(
        Skill(
            id="",
            name="release-check-helper",
            description="Run release checks",
            body="Check the release and retain its receipt.",
            triggers=("release",),
        )
    )
    current = await first.get(skill_id)
    assert current is not None
    candidate = SkillCandidate(
        draft=Skill(
            id="",
            name="release-check-helper-v2",
            description="Run release checks with verification",
            body="Run the checks and verify the receipts.",
            triggers=("release", "verification"),
        ),
        source_task_id="task-refine",
        target_skill=skill_id,
        target_skill_version=current.version,
        evidence=("receipt-2",),
        confidence=0.9,
    )
    await first.record_candidate(candidate)
    await first_db.close()

    second_db = Database(str(db_path))
    second = SkillLifecycle(second_db)
    try:
        restored = await second.get_candidate(candidate.id)
        assert restored is not None
        assert restored["target_skill"] == skill_id
        assert restored["target_skill_version"] == current.version
        assert await second.promote_candidate(candidate.id)
        updated = await second.get(skill_id)
        assert updated is not None
        assert updated.version == current.version + 1
        assert updated.body == candidate.draft.body
    finally:
        await second_db.close()


async def test_skill_refinement_rejects_intervening_target_update(tmp_path):
    db = Database(str(tmp_path / "athena.db"))
    lifecycle = SkillLifecycle(db)
    try:
        skill_id = await lifecycle.install(
            Skill(id="", name="target-skill", description="Target", body="v1")
        )
        current = await lifecycle.get(skill_id)
        assert current is not None
        candidate = SkillCandidate(
            draft=Skill(id="", name="target-skill-v2", description="Target", body="v2"),
            source_task_id="task-refine",
            target_skill=skill_id,
            target_skill_version=current.version,
            confidence=0.8,
        )
        await lifecycle.record_candidate(candidate)
        await lifecycle.update(
            skill_id, Skill(id="", name="target-skill", description="Target", body="intervening")
        )

        assert await lifecycle.promote_candidate(candidate.id) is None
        unchanged = await lifecycle.get(skill_id)
        assert unchanged is not None
        assert unchanged.body == "intervening"
        assert unchanged.version == current.version + 1
    finally:
        await db.close()


async def test_skill_outcome_is_attributed_to_exact_version(tmp_path):
    db = Database(str(tmp_path / "athena.db"))
    lifecycle = SkillLifecycle(db)
    try:
        skill_id = await lifecycle.install(
            Skill(id="", name="outcome-skill", description="Outcome", body="v1")
        )
        current = await lifecycle.get(skill_id)
        assert current is not None
        recorded = await lifecycle.record_outcome(
            skill_id,
            version=current.version,
            passed=False,
            task_id="task-failed",
            failure={"kind": "selection", "message": "wrong scope"},
        )
        assert recorded["refinement_required"] is True
        stale = await lifecycle.record_outcome(
            skill_id,
            version=current.version - 1,
            passed=True,
            task_id="task-stale",
        )
        assert stale["status"] == "revision_mismatch"
        refreshed = await lifecycle.get(skill_id)
        assert refreshed is not None
        evidence = refreshed.metadata["athena"]["evidence"]
        assert evidence["failed_reuses"] == 1
        assert evidence["outcomes"][0]["task_id"] == "task-failed"
    finally:
        await db.close()
