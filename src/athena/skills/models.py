"""Skill data model.

Skills are instruction bundles injected into context (NOT executable
capabilities) per BUILDSPEC sections 65-68. A :class:`Skill` is the parsed,
validated representation of a portable ``SKILL.md``; a :class:`SkillCandidate`
is a self-improvement draft proposed from a task transcript (section 68).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from athena.protocol.messages import Provenance, TrustClass


@dataclass(frozen=True)
class Skill:
    """A validated, injectable skill.

    ``body`` is the markdown instruction text the model reads. ``metadata``
    carries backend-agnostic extras (scope, owner, tags) and is NOT part of the
    authoritative instruction content.
    """

    id: str
    name: str
    description: str
    body: str
    triggers: tuple[str, ...] = ()
    scope: str = "user"
    trust: TrustClass = TrustClass.AGENT_CURATED
    version: int = 1
    path: str | None = None
    source: Provenance | None = None
    enabled: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        """One-line progressive-disclosure summary (BUILDSPEC 67)."""
        if not self.description:
            return self.name
        return f"{self.name} · {self.description}"


@dataclass(frozen=True)
class SkillCandidate:
    """A self-improvement draft (SPEC 26, BUILDSPEC 68).

    Produced by the hot loop; must pass validation and an explicit promotion
    step before it becomes an active skill. Never silently applied.
    """

    draft: Skill
    source_task_id: str
    target_skill: str | None
    rationale: str = ""
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0
    promotion_hint: bool = True

    @property
    def propose_name(self) -> str:
        return self.draft.name

    @property
    def id(self) -> str:
        """Stable review identifier for this candidate.

        Candidate drafts are immutable values.  Deriving the identifier from
        their source task, name, and content lets the durable lifecycle
        upsert the same proposal after a retry or service restart without
        introducing a second mutable identity authority.
        """
        payload = {
            "source_task_id": self.source_task_id,
            "name": self.draft.name,
            "body": self.draft.body,
            "version": self.draft.version,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return f"skill_candidate_{digest}"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


__all__ = ["Skill", "SkillCandidate", "ValidationResult"]
