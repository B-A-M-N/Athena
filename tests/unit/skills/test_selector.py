from athena.skills.models import Skill
from athena.skills.selector import SkillSelector
from athena.protocol.messages import TrustClass


def _skill(name, description, triggers, **overrides):
    return Skill(
        id=name,
        name=name,
        description=description,
        body="body",
        triggers=tuple(triggers),
        trust=TrustClass.AGENT_CURATED,
        scope="project",
        version=1,
        **overrides,
    )


async def test_selector_ranks_by_keyword_match():
    kubernetes = _skill(
        "k8s",
        "Deploy pods to the kubernetes cluster",
        ["kubernetes", "deploy"],
    )
    database = _skill(
        "db",
        "Manage postgres databases and migrations",
        ["database", "postgres"],
    )
    selected = await SkillSelector().select(
        task_objective="deploy pods to the kubernetes cluster",
        available=[database, kubernetes],
        limit=5,
        task_context={"project_id": "repo"},
    )
    assert selected[0].name == "k8s"
    assert set(s.name for s in selected) == {"k8s", "db"}


async def test_selector_empty_skills_list_returns_empty():
    selected = await SkillSelector().select(
        task_objective="deploy kubernetes",
        available=[],
        limit=5,
    )
    assert selected == []


async def test_selector_filters_missing_explicit_prerequisites():
    skill = _skill(
        "release",
        "Verify a release artifact",
        ["release"],
        metadata={
            "athena": {
                "applicability": {"required_dependencies": ["cosign"]},
            }
        },
    )
    selected = await SkillSelector().select(
        task_objective="verify the release artifact",
        available=[skill],
        limit=3,
        task_context={"project_id": "repo", "dependencies": []},
    )
    assert selected == []


async def test_selector_prefers_verified_reuse_when_applicability_matches():
    verified = _skill(
        "verified",
        "Inspect deployment output",
        ["deployment"],
        metadata={"athena": {"evidence": {"verified_reuses": 3}}},
    )
    unverified = _skill("unverified", "Inspect deployment output", ["deployment"])
    selected = await SkillSelector().select(
        task_objective="inspect deployment output",
        available=[unverified, verified],
        limit=2,
        task_context={"project_id": "repo"},
    )
    assert selected[0].name == "verified"


async def test_selector_emits_task_and_environment_selection_evidence():
    skill = _skill(
        "release",
        "Verify a release artifact",
        ["release"],
    )
    records, selected = await SkillSelector().select_with_evidence(
        task_objective="publish a release artifact",
        available=[skill],
        limit=1,
        task_context={
            "project_id": "repo-a",
            "environment": "linux",
            "available_capabilities": ["execute"],
        },
    )

    assert [item.id for item in selected] == ["release"]
    assert len(records) == 1
    record = records[0].to_record()
    assert record["skill_id"] == "release"
    assert record["version"] == 1
    assert record["task_class"] == "release"
    assert record["selected"] is True
    assert record["applicable"] is True
    assert record["environment_fingerprint"].startswith("env_")
    assert "trigger_matches:release" in record["evidence"]


async def test_observed_failures_demote_a_plausible_skill():
    failing = _skill(
        "failing",
        "Repair a failing service",
        ["failing", "service"],
        metadata={"athena": {"evidence": {"failed_reuses": 4}}},
    )
    neutral = _skill("neutral", "Repair a failing service", ["failing", "service"])
    records, selected = await SkillSelector().select_with_evidence(
        task_objective="repair a failing service",
        available=[failing, neutral],
        limit=1,
        task_context={"project_id": "repo-a"},
    )

    assert [item.id for item in selected] == ["neutral"]
    assert records[0].evidence
    assert "failed_reuses:4" in records[0].evidence


async def test_applicable_but_unselected_skill_remains_opportunity_evidence():
    skill = _skill("release", "Release artifact", ["release"])
    records, selected = await SkillSelector().select_with_evidence(
        task_objective="release artifact",
        available=[skill],
        limit=0,
        task_context={"project_id": "repo"},
    )
    assert selected == []
    assert records[0].applicable is True
    assert records[0].selected is False
