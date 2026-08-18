"""Skill selection (BUILDSPEC 67 progressive disclosure, BHV-105).

Selects a bounded set of relevant skills for a given task objective by ranking
truth / description relevance. Ensures only the selected SKILL.md bodies (not
every installed skill) are injected. This is the raised-relevance step between
"all skill metadata" and "selected SKILL.md".
"""

from __future__ import annotations

import re
from typing import Sequence

from athena.skills.models import Skill

_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")
_SKIPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at",
        "with", "using", "please", "help", "that", "this", "is", "are", "be",
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
    ) -> list[Skill]:
        if limit <= 0 or not task_objective:
            return []
        objective_tokens = set(self._tokens(task_objective))
        scored: list[tuple[float, Skill]] = []
        for skill in available:
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
        return score

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
        return [
            w.lower()
            for w in _TOKEN_RE.findall(text or "")
            if w.lower() not in _SKIPWORDS
        ]


__all__ = ["SkillSelector"]