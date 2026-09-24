"""Held-out skill-selection benchmark and empirical reuse comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from athena.skills.models import Skill
from athena.skills.selector import SkillSelector


@dataclass(frozen=True)
class SkillSelectionCase:
    id: str
    objective: str
    relevant_skill_ids: frozenset[str]
    incompatible_skill_ids: frozenset[str] = frozenset()
    task_context: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.id or not self.objective:
            raise ValueError("skill selection case requires id and objective")
        if self.relevant_skill_ids & self.incompatible_skill_ids:
            raise ValueError("skill selection case cannot require and reject the same skill")


def _context(case: SkillSelectionCase) -> dict[str, Any]:
    context = dict(case.task_context or {})
    context["_skill_benchmark"] = True
    context.setdefault("objective", case.objective)
    context.setdefault("project_id", "benchmark-project")
    context.setdefault("incompatible_intents", set())
    return context


async def run_skill_selection_benchmark(
    cases: Sequence[SkillSelectionCase],
    available: Sequence[Skill],
    *,
    limit: int = 3,
) -> dict[str, Any]:
    selector = SkillSelector(min_score=0.0)
    reports: list[dict[str, Any]] = []
    for case in cases:
        records, selected = await selector.select_with_evidence(
            task_objective=case.objective,
            available=available,
            limit=limit,
            task_context=_context(case),
        )
        selected_ids = {skill.id for skill in selected}
        false_positive = selected_ids & case.incompatible_skill_ids
        recall = 1.0 if case.relevant_skill_ids <= selected_ids else 0.0
        reports.append(
            {
                "case_id": case.id,
                "selected": sorted(selected_ids),
                "selected_relevant": sorted(selected_ids & case.relevant_skill_ids),
                "missing_relevant": sorted(case.relevant_skill_ids - selected_ids),
                "false_positive": sorted(false_positive),
                "relevance_passed": not false_positive and recall == 1.0,
                "records": [record.to_record() for record in records],
            }
        )
    return {
        "case_count": len(reports),
        "systems": ["skill_selector"],
        "cases": reports,
        "by_case": {str(item["case_id"]): item for item in reports},
        "selection_passed": all(item["relevance_passed"] for item in reports),
    }


def compare_skill_reuse(
    without_skill: Mapping[str, Any],
    with_skill: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare held-out outcomes without inferring causality from one run."""
    without_status = str(without_skill.get("status") or "unknown")
    with_status = str(with_skill.get("status") or "unknown")
    without_complete = without_status == "complete"
    with_complete = with_status == "complete"
    return {
        "without_skill": dict(without_skill),
        "with_skill": dict(with_skill),
        "held_out_task": bool(without_skill.get("task_id") and with_skill.get("task_id")),
        "without_skill_complete": without_complete,
        "with_skill_complete": with_complete,
        "completion_delta": int(with_complete) - int(without_complete),
        "improved_completion": with_complete and not without_complete,
        "materially_contributed": None,
        "conclusion": "observed_completion_delta"
        if with_complete != without_complete
        else "no_completion_delta",
    }


__all__ = ["SkillSelectionCase", "compare_skill_reuse", "run_skill_selection_benchmark"]
