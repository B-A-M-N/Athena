"""Skill selection (BUILDSPEC 67 progressive disclosure, BHV-105).

Selects a bounded set of relevant skills for a given task objective by ranking
truth / description relevance. Ensures only the selected SKILL.md bodies (not
every installed skill) are injected. This is the raised-relevance step between
"all skill metadata" and "selected SKILL.md".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Sequence

from athena.skills.models import Skill, SkillSelectionRecord

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
        _records, selected = await self.select_with_evidence(
            task_objective=task_objective,
            available=available,
            limit=limit,
            task_context=task_context,
        )
        return selected

    async def select_with_evidence(
        self,
        *,
        task_objective: str,
        available: Sequence[Skill],
        limit: int,
        task_context: Mapping[str, Any] | None = None,
    ) -> tuple[tuple[SkillSelectionRecord, ...], list[Skill]]:
        """Return selection decisions plus the selected skills.

        Every applicable skill is recorded as an opportunity, including skills
        below the limit.  The context compiler persists these decisions as
        evidence so later reuse can distinguish selection from application.
        """
        context = task_context or {}
        if not task_objective:
            return (), []
        objective_tokens = set(self._tokens(task_objective))
        task_class = classify_task_class(task_objective)
        environment = environment_fingerprint(context)
        scored: list[tuple[float, Skill]] = []
        records: list[SkillSelectionRecord] = []
        for skill in available:
            applicable = self._applicable(skill, context)
            if not applicable:
                continue
            score = self._score(skill, objective_tokens)
            eligible = score >= self.min_score
            if eligible:
                scored.append((score, skill))
            records.append(
                SkillSelectionRecord(
                    skill_id=skill.id,
                    version=int(skill.version or 1),
                    task_class=task_class,
                    environment_fingerprint=environment,
                    score=score,
                    applicable=True,
                    selected=False,
                    reason=(
                        "deterministic applicability plus relevance score"
                        if eligible
                        else "applicable but below selection threshold"
                    ),
                    evidence=self._evidence(skill, objective_tokens, score),
                )
            )
        scored.sort(key=lambda t: t[0], reverse=True)
        selected_skills = [skill for _score, skill in scored[:limit]]
        selected_ids = {(skill.id, int(skill.version or 1)) for skill in selected_skills}
        records = [
            record.__class__(
                **{
                    **record.__dict__,
                    "selected": (record.skill_id, record.version) in selected_ids,
                }
            )
            for record in records
        ]
        return tuple(records), selected_skills

    def _evidence(self, skill: Skill, objective_tokens: set[str], score: float) -> tuple[str, ...]:
        trigger_hits = tuple(
            trigger
            for trigger in skill.triggers
            if self._trigger_score(trigger, objective_tokens) > 0
        )
        metadata = self._athena_metadata(skill).get("evidence") or {}
        evidence = [f"trigger_matches:{trigger}" for trigger in trigger_hits]
        if metadata.get("verified_reuses"):
            evidence.append(f"verified_reuses:{int(metadata['verified_reuses'])}")
        if metadata.get("failed_reuses"):
            evidence.append(f"failed_reuses:{int(metadata['failed_reuses'])}")
        evidence.append(f"score:{score:.6f}")
        return tuple(evidence)

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
        # Proposal confidence is not reliability.  Exact observed failures
        # must reduce preference, otherwise a plausible but repeatedly harmful
        # skill remains selected forever.
        score -= min(float(evidence.get("failed_reuses") or 0), 4.0) * 0.75
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


_TASK_CLASS_MARKERS = (
    ("deployment", ("deploy", "deployment", "rollout", "cluster", "kubernetes", "service")),
    ("release", ("release", "publish", "artifact", "package", "version")),
    ("debugging", ("debug", "bug", "fix", "repair", "error", "failing", "test")),
    ("research", ("research", "investigate", "source", "evidence", "compare")),
    ("maintenance", ("refactor", "maintain", "migrate", "cleanup", "document")),
)


def classify_task_class(objective: str) -> str:
    """Return a bounded, deterministic task class for reuse evidence."""
    tokens = set(SkillSelector._tokens(objective))
    for label, markers in _TASK_CLASS_MARKERS:
        if tokens.intersection(markers):
            return label
    return "general"


def environment_fingerprint(context: Mapping[str, Any]) -> str:
    """Hash non-secret operating conditions relevant to skill applicability."""
    workspace = context.get("workspace") or {}
    if not isinstance(workspace, Mapping):
        workspace = {"id": str(workspace)}
    payload = {
        "project_id": str(context.get("project_id") or workspace.get("id") or ""),
        "environment": str(context.get("environment") or ""),
        "available_capabilities": sorted(
            str(value) for value in (context.get("available_capabilities") or ())
        ),
        "dependencies": sorted(str(value) for value in (context.get("dependencies") or ())),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"env_{digest}"


__all__ = ["SkillSelector", "classify_task_class", "environment_fingerprint"]
