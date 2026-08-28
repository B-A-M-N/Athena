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
