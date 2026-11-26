from athena.evaluation.skill_selection import (
    SkillSelectionCase,
    compare_skill_reuse,
    run_skill_selection_benchmark,
)
from athena.skills.models import Skill


def _skill(skill_id, description, triggers, **overrides):
    metadata = overrides.pop("metadata", {})
    return Skill(
        id=skill_id,
        name=skill_id,
        description=description,
        body="body",
        triggers=tuple(triggers),
        scope="project",
        version=1,
        metadata=metadata,
        **overrides,
    )


async def test_held_out_paraphrases_retrieve_relevant_skills_without_lexical_keywords():
    deployment = _skill(
        "deploy",
        "Apply the service rollout procedure",
        ["rollout"],
        metadata={"athena": {"applicability": {"supported_intents": ["deployment"]}}},
    )
    database = _skill("db", "Manage postgres migrations", ["postgres", "migration"])
    cases = (
        SkillSelectionCase(
            id="deploy-paraphrase",
            objective="ship the application to the cluster",
            relevant_skill_ids=frozenset({"deploy"}),
        ),
        SkillSelectionCase(
            id="database-paraphrase",
            objective="upgrade the relational data store schema",
            relevant_skill_ids=frozenset({"db"}),
        ),
    )
    report = await run_skill_selection_benchmark(cases, [deployment, database], limit=1)
    assert report["selection_passed"] is True
    assert report["by_case"]["deploy-paraphrase"]["selected"] == ["deploy"]
    assert report["by_case"]["database-paraphrase"]["selected"] == ["db"]


async def test_adversarial_incompatible_skill_is_rejected():
    production = _skill(
        "production",
        "Deploy the production service",
        ["deploy", "service"],
        metadata={"athena": {"applicability": {"incompatible_intents": ["debugging"]}}},
    )
    report = await run_skill_selection_benchmark(
        [
            SkillSelectionCase(
                id="debug-not-deploy",
                objective="fix the broken service",
                relevant_skill_ids=frozenset(),
                incompatible_skill_ids=frozenset({"production"}),
            )
        ],
        [production],
    )
    assert report["selection_passed"] is True
    assert report["by_case"]["debug-not-deploy"]["false_positive"] == []


def test_reuse_comparison_keeps_material_contribution_unknown():
    result = compare_skill_reuse(
        {"task_id": "held-out-1", "status": "failed", "evidence": []},
        {"task_id": "held-out-1", "status": "complete", "evidence": ["proof"]},
    )
    assert result["held_out_task"] is True
    assert result["completion_delta"] == 1
    assert result["improved_completion"] is True
    assert result["materially_contributed"] is None
