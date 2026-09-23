"""Skill selection (BUILDSPEC 67 progressive disclosure, BHV-105).

Selects a bounded set of relevant skills for a given task objective by ranking
truth / description relevance. Ensures only the selected SKILL.md bodies (not
every installed skill) are injected. This is the raised-relevance step between
"all skill metadata" and "selected SKILL.md".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Sequence

from athena.skills.models import Skill

_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")
_SKIPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "with",
        "using",
        "please",
        "help",
        "that",
        "this",
        "is",
        "are",
        "be",
    }
)


class SkillSelector:
    """Ranks skills by trigger/keyword overlap with the task objective."""

    def __init__(self, *, min_score: float = 0.0) -> None:
        self.min_score = min_score

    async def select(
        self,
        *,
        task_objective: str,
        available: Sequence[Skill],
        limit: int,
        task_context: Mapping[str, Any] | None = None,
    ) -> list[Skill]:
        if limit <= 0 or not task_objective:
            return []
        objective_tokens = set(self._tokens(task_objective))
        scored: list[tuple[float, Skill]] = []
        for skill in available:
            if not self._applicable(skill, task_context or {}):
                continue
            score = self._score(skill, objective_tokens)
            if score >= self.min_score:
                scored.append((score, skill))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [s for _, s in scored[:limit]]

    def _score(self, skill: Skill, objective_tokens: set[str]) -> float:
        score = 0.0
        skill_tokens = set(self._tokens(skill.description))
        for trigger in skill.triggers:
            score += self._trigger_score(trigger, objective_tokens)
        overlap = len(skill_tokens & objective_tokens)
        if overlap:
            score += 1.0 * overlap
        evidence = self._athena_metadata(skill).get("evidence") or {}
        score += min(float(evidence.get("verified_reuses") or 0), 3.0) * 0.25
        return score

    @staticmethod
    def _athena_metadata(skill: Skill) -> Mapping[str, Any]:
        raw = skill.metadata.get("athena") if isinstance(skill.metadata, Mapping) else None
        return raw if isinstance(raw, Mapping) else {}

    @classmethod
    def _applicable(cls, skill: Skill, context: Mapping[str, Any]) -> bool:
        """Apply explicit scope/prerequisite gates after keyword discovery."""
        meta = cls._athena_metadata(skill)
        if skill.scope == "project":
            project_id = context.get("project_id")
            owner = meta.get("project_id") or meta.get("owner")
            if not project_id:
                return False
            if owner and project_id and str(owner) != str(project_id):
                return False
            if owner and not project_id:
                return False
        owner = meta.get("user_id") or meta.get("principal_id")
        principal = context.get("principal_id")
        if owner and principal and str(owner) != str(principal):
            return False
        if owner and not principal:
            return False
        applicability = meta.get("applicability")
        if not isinstance(applicability, Mapping):
            applicability = {}
        required = applicability.get("required_capabilities") or meta.get("required_capabilities")
        available = set(str(value) for value in (context.get("available_capabilities") or ()))
        if required and not set(str(value) for value in required).issubset(available):
            return False
        required_dependencies = applicability.get("required_dependencies") or meta.get(
            "required_dependencies"
        )
        dependencies = set(str(value) for value in (context.get("dependencies") or ()))
        if required_dependencies and not set(
            str(value) for value in required_dependencies
        ).issubset(dependencies):
            return False
        environment = applicability.get("environment")
        if environment:
            actual = context.get("environment")
            allowed = (
                {str(value) for value in environment}
                if isinstance(environment, (list, tuple, set))
                else {str(environment)}
            )
            if str(actual) not in allowed:
                return False
        failure_terms = set(
            cls._tokens(
                " ".join(str(value) for value in (meta.get("known_failure_conditions") or ()))
            )
        )
        objective_terms = set(cls._tokens(str(context.get("objective") or "")))
        if failure_terms & objective_terms:
            return False
        return True

    @staticmethod
    def _trigger_score(trigger: str, objective_tokens: set[str]) -> float:
        trigger_l = trigger.lower().strip()
        if trigger_l in objective_tokens:
            return 3.0
        parts = set(SkillSelector._tokens(trigger_l))
        if not parts:
            return 0.0
        intersection = parts & objective_tokens
        return 1.5 * (len(intersection) / len(parts))

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [w.lower() for w in _TOKEN_RE.findall(text or "") if w.lower() not in _SKIPWORDS]


__all__ = ["SkillSelector"]
