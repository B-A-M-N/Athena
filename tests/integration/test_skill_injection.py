"""Skill selection/injection through the real SkillLifecycle + ContextCompiler."""
from __future__ import annotations
import pytest

from athena.protocol.ids import new_id
from athena.protocol.messages import Provenance, SourceType, TrustClass
from athena.protocol.tasks import ResourceBudget, TaskSpec
from athena.skills.models import Skill

_MARKER = "APISUMMARIZER_RULEBOOK_SENTINEL"


async def _install_summarizer_skill(svc) -> None:
    lifecycle = svc._skills._lifecycle
    await lifecycle.install(Skill(
        id="skill-api-summarizer",
        name="api-summarizer",
        description="Produce API docs in the project's summarizer style.",
        body=(
            f"{_MARKER}\n"
            "When documenting endpoints always follow the local conventions."
        ),
        triggers=("summarize", "doxy", "docs"),
        trust=TrustClass.AGENT_CURATED,
        source=Provenance(
            source_type=SourceType.SKILL,
            source_id="skill-api-summarizer",
            trust=TrustClass.AGENT_CURATED,
        ),
    ))


async def _compile_objective(svc, objective: str) -> str:
    spec = TaskSpec(id=new_id("task"), objective=objective,
                    session_id=new_id("session"), resource_budget=ResourceBudget(),
                    metadata={"autonomy": "supervised"})
    compiled = await svc._compiler.compile(spec)
    return "\n".join(m.text() for m in compiled.messages)


@pytest.mark.athena_claim("BHV-105")
@pytest.mark.athena_evidence("test", "e2e")
async def test_skill_injected_when_trigger_in_objective(make_service):
    svc = await make_service()
    await _install_summarizer_skill(svc)

    with_trigger = await _compile_objective(svc, "please summarize the api docs")
    without_trigger = await _compile_objective(svc, "compute the answer to 2+2")

    assert _MARKER in with_trigger, "skill body must be injected when triggered"
    assert _MARKER not in without_trigger, "skill must not inject without the trigger"