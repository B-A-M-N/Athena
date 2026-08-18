from athena.skills.models import Skill
from athena.skills.validator import SkillValidator
from athena.protocol.messages import TrustClass


def _skill(**overrides):
    defaults = dict(
        id="skill-1",
        name="deploy_helper",
        description="helps deploy the service safely",
        body="Follow the deployment checklist and verify health before releasing.",
        scope="project",
        trust=TrustClass.CONFIGURED_INSTRUCTION,
        version=1,
    )
    defaults.update(overrides)
    return Skill(**defaults)


async def test_valid_skill_passes():
    result = SkillValidator().validate(_skill())
    assert result.ok is True
    assert result.errors == ()


async def test_skill_with_eval_fails_validation_instead_of_warning():
    result = SkillValidator().validate(
        _skill(body="def run(user_code):\n    return eval(user_code)")
    )
    assert result.ok is False
    assert any("eval" in error or "executable" in error for error in result.errors)


async def test_skill_missing_required_field_fails():
    result = SkillValidator().validate(_skill(body="   "))
    assert result.ok is False
    assert any("body" in error for error in result.errors)