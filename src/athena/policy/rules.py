"""Declarative policy rules.

A Rule matches a concrete policy request on capability id pattern, resolved
effect class, and/or path glob, and yields a verdict. Rules are prioritized so
specific deny rules (e.g. a concrete resolved resource) beat broad allow rules
(BHV-041 resolved-effect policy; BHV-043 denial means no effect).

A RuleSet is an ordered, priority-ranked collection that the PolicyEngine loads
and evaluates in order. First matching rule wins (highest priority first); the
default verdict applies when nothing matches.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from athena.protocol.capabilities import EffectClass


@dataclass(frozen=True)
class Rule:
    """A single policy rule.

    At least one matcher must be provided to be useful; all provided matchers
    must match for the rule to fire. ``path`` is a glob matched against the
    resolved absolute path when the request carries one in its arguments.
    """

    verdict: str
    capability_id: str | None = None
    effect: EffectClass | str | None = None
    path: str | None = None
    resource: str | None = None
    priority: int = 100
    reason: str | None = None

    def matches(
        self,
        capability_id: str,
        effects: frozenset[EffectClass],
        arguments: dict[str, Any],
    ) -> bool:
        if self.capability_id is not None and not _glob(self.capability_id, capability_id):
            return False
        if self.effect is not None and not _effect_matches(self.effect, effects):
            return False
        if self.path is not None:
            path = arguments.get("path") or arguments.get("resource") or ""
            if not path or not _glob(self.path, str(path)):
                return False
        if self.resource is not None:
            resource = arguments.get("resource") or arguments.get("path") or ""
            if not resource or not _glob(self.resource, str(resource)):
                return False
        return True

    @property
    def name(self) -> str:
        bits = [self.capability_id or "*", self.effect or "*"]
        if self.path is not None:
            bits.append(self.path)
        return ".".join(bits)


@dataclass(frozen=True)
class RuleSet:
    """An ordered set of rules evaluated highest-priority first."""

    rules: tuple[Rule, ...] = field(default_factory=tuple)
    default: str = "ask"

    def ordered(self) -> list[Rule]:
        return sorted(self.rules, key=lambda r: r.priority, reverse=True)

    def evaluate(
        self,
        capability_id: str,
        effects: frozenset[EffectClass],
        arguments: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Return (verdict, matched_rule_name) for the first matching rule."""
        for rule in self.ordered():
            if rule.matches(capability_id, effects, arguments):
                return rule.verdict, rule.name
        return None

    def verdict(
        self,
        capability_id: str,
        effects: frozenset[EffectClass],
        arguments: dict[str, Any],
    ) -> str:
        hit = self.evaluate(capability_id, effects, arguments)
        if hit is None:
            return self.default
        return hit[0]


def rule(
    verdict: str,
    capability_id: str | None = None,
    effect: EffectClass | str | None = None,
    path: str | None = None,
    resource: str | None = None,
    priority: int = 100,
    reason: str | None = None,
) -> Rule:
    return Rule(
        capability_id=capability_id,
        effect=effect
        if isinstance(effect, EffectClass)
        else EffectClass(effect)
        if effect
        else None,
        path=path,
        resource=resource,
        priority=priority,
        reason=reason,
        verdict=verdict,
    )


def _glob(pattern: str, value: str) -> bool:
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return value == base or value.startswith(base + "/")
    return fnmatch.fnmatch(value, pattern)


def _effect_matches(effect: EffectClass | str, effects: frozenset[EffectClass]) -> bool:
    target = effect if isinstance(effect, EffectClass) else EffectClass(effect)
    return target in effects


__all__ = ["Rule", "RuleSet", "rule"]
