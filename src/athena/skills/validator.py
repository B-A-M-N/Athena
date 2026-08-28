"""Skill validation (BUILDSPEC 66, spec 25).

A skill is instructions only. Validation enforces the required portable
front-matter contract and basic injection hygiene so untrusted ``SKILL.md``
content cannot escalate beyond its declared trust level (BHV-106).
"""

from __future__ import annotations

import re

from athena.protocol.messages import TrustClass
from athena.skills.models import Skill, ValidationResult

_REQUIRED_FIELDS = ("name", "description", "body")

# Skills are guidance, not executable payloads. Reject deception about
# executable authority: claims that running the skill executes code, installs
# packages, or bypasses policy. Regular legitimate prose may mention these in
# an illustrative way, so these trigger warnings (hard) only on explicit
# command-like or coercion patterns; forbidden patterns are hard errors.
_FORBIDDEN_RE = re.compile(
    r"(?i)\beval\s*\(|\bexec\s*\(|\bos\.system\s*\(|"
    r"\b__import__\s*\(|\bpickle\.loads?\s*\(|"
    r"\bsubprocess\.(?:run|call|Popen|check_call|check_output)\s*\(|"
    r"\b(?:curl|wget|git\s+clone)\s+[^\s]+\s*\|?\s*(?:bash|sh)\b"
)

_WARNING_RE = re.compile(
    r"(?i)\b(?:shell|bash|execute|run|fetch|download|install|"
    r"sudo|root|curl|wget)\b"
)

_VALID_SCOPES = {"user", "project", "bundled", "global"}
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}$")
_TRIGGER_RE = re.compile(r"^[^\n\r]{1,80}$")


class SkillValidator:
    """Validates parsed skills and skill candidates."""

    def validate(self, skill: Skill) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        for field_ in _REQUIRED_FIELDS:
            value = getattr(skill, field_, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"missing required field: {field_}")

        if not _NAME_RE.match(skill.name or ""):
            errors.append("invalid name: must match [a-zA-Z0-9][a-zA-Z0-9._-]{1,63}")

        if not isinstance(skill.trust, TrustClass):
            errors.append(f"invalid trust class: {skill.trust!r}")

        if skill.scope not in _VALID_SCOPES:
            warnings.append(f"unknown scope {skill.scope!r}; expected {sorted(_VALID_SCOPES)}")

        if not isinstance(skill.version, int) or skill.version < 1:
            errors.append("version must be a positive integer")

        bad_triggers = [t for t in skill.triggers if not _TRIGGER_RE.match(t or "")]
        if bad_triggers:
            errors.append(
                f"invalid trigger value(s): {bad_triggers[:3]!r} (1-80 chars, single line)"
            )

        if skill.body:
            if len(skill.body) > 200_000:
                errors.append("body exceeds maximum size (200k chars)")
            if _FORBIDDEN_RE.search(skill.body):
                errors.append(
                    "body contains executable-claim patterns; skills are "
                    "instructions-only and must not claim eval/exec/subprocess authority"
                )
            if _WARNING_RE.search(skill.body):
                warnings.append(
                    "body references execution/shell concepts; ensure instructions-only framing"
                )

        if skill.description and len(skill.description) > 2000:
            warnings.append("description is unusually long")

        return ValidationResult(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))

    def validate_candidate(self, candidate: "object") -> ValidationResult:
        from athena.skills.models import SkillCandidate

        if not isinstance(candidate, SkillCandidate):
            return ValidationResult(
                ok=False,
                errors=("candidate is not a SkillCandidate instance",),
            )
        result = self.validate(candidate.draft)
        errors = list(result.errors)
        if not (0.0 <= candidate.confidence <= 1.0):
            errors.append("candidate confidence must be within [0, 1]")
        return ValidationResult(
            ok=not errors,
            errors=tuple(errors),
            warnings=result.warnings,
        )


__all__ = ["SkillValidator", "ValidationResult"]
