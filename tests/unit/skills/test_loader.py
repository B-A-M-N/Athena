from pathlib import Path

import pytest

from athena.skills.loader import SkillLoader


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    return root


async def test_load_valid_skill_md_parses_fields(skills_dir: Path):
    skill_dir = skills_dir / "git_usage"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: git_usage\n"
        "description: A guide for using git workflows\n"
        "version: 3\n"
        "triggers: commit, rebase, merge\n"
        "---\n"
        "Use git to stage, commit, and push changes.\n"
    )

    loader = SkillLoader(search_paths=[skills_dir])
    skills = await loader.load()

    assert len(skills) == 1
    skill = skills[0]
    assert skill.name == "git_usage"
    assert skill.description == "A guide for using git workflows"
    assert skill.version == 3
    assert skill.body == "Use git to stage, commit, and push changes."
    assert "commit" in skill.triggers


async def test_malformed_skill_md_is_skipped_gracefully(skills_dir: Path):
    valid_dir = skills_dir / "valid"
    valid_dir.mkdir()
    (valid_dir / "SKILL.md").write_text(
        "---\nname: valid_helper\ndescription: a valid helper\n---\nDo the thing safely.\n"
    )
    malformed_dir = skills_dir / "malformed"
    malformed_dir.mkdir()
    (malformed_dir / "SKILL.md").write_text("---\nname: 99invalid\nbody:   \n---\n")

    loader = SkillLoader(search_paths=[skills_dir])
    skills = await loader.load()

    names = [s.name for s in skills]
    assert "valid_helper" in names
    assert "99invalid" not in names
    assert loader.errors


async def test_repeated_loads_replace_state_and_use_revision_cache(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    skill_dir = skills_dir / "cached"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: cached\ndescription: cached skill\n---\nDo the thing.\n")
    loader = SkillLoader(search_paths=[skills_dir])
    original = loader.parse_skill_file
    parses = 0

    def counted(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "parse_skill_file", counted)

    for _ in range(100):
        assert len(await loader.load_active()) == 1

    assert len(loader._skills) == 1  # noqa: SLF001 - cache invariant
    assert parses == 1
    assert loader.generation == 1

    skill_path.write_text("---\nname: cached\ndescription: changed skill\n---\nDo the new thing.\n")
    assert (await loader.load_active())[0].description == "changed skill"
    assert parses == 2
    assert loader.generation == 2
