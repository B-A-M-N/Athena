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
