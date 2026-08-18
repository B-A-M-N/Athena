"""Skills subsystem (BUILDSPEC sections 65-68, SPEC sections 25-26).

Skills are reusable, self-contained instruction bundles (``SKILL.md`` with YAML
front-matter) injected into model context — they are guidance, NOT executable
capabilities. This package provides discovery/parsing (``loader``), relevance
selection (``selector``), self-improvement candidate drafts (``candidates``),
validation (``validator``), and the persistent lifecycle (``lifecycle``).
"""

from __future__ import annotations

from athena.skills.candidates import SkillCandidate, candidates_from_task
from athena.skills.lifecycle import SkillLifecycle, SkillStore
from athena.skills.loader import SkillLoader
from athena.skills.models import Skill, ValidationResult
from athena.skills.selector import SkillSelector
from athena.skills.validator import SkillValidator

__all__ = [
    "Skill",
    "SkillCandidate",
    "ValidationResult",
    "SkillLoader",
    "SkillSelector",
    "SkillValidator",
    "SkillLifecycle",
    "SkillStore",
    "candidates_from_task",
]