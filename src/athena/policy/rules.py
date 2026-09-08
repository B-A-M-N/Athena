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
    """An ordered set of rules evaluated highest-priority first.

    Two compiled views are built once at construction (the set is frozen,
    so they can never go stale):

    * ``_compiled`` — all rules in priority order (returned by ``ordered``).
    * ``_by_effect`` — per-effect sublists in priority order, so an
      evaluation for a concrete effect scans only rules that could match
      it, instead of the whole set.

    Semantics are unchanged: first matching rule in priority order wins;
    the default applies when nothing matches.
    """

    rules: tuple[Rule, ...] = field(default_factory=tuple)
    default: str = "ask"

    def __post_init__(self) -> None:
        compiled = tuple(sorted(self.rules, key=lambda r: r.priority, reverse=True))
        object.__setattr__(self, "_compiled", compiled)
        # Per-effect candidate lists: the effect-specific rules for that
        # effect MERGED with the effect-agnostic rules, in the compiled
        # priority order. Merging at build time (rather than appending
        # agnostic rules after the specific ones) preserves exact
        # first-match-in-priority-order semantics between the two kinds.
        specific: dict[EffectClass, list[Rule]] = {}
        agnostic: list[Rule] = []
        for rule in compiled:
            if rule.effect is not None:
                target = (
                    rule.effect
                    if isinstance(rule.effect, EffectClass)
                    else EffectClass(rule.effect)
                )
                specific.setdefault(target, []).append(rule)
            else:
                agnostic.append(rule)
        by_effect: dict[EffectClass, tuple[Rule, ...]] = {}
        for effect, rules_for in specific.items():
            merged = sorted(
                [*rules_for, *agnostic],
                key=lambda r: r.priority,
                reverse=True,
            )
            by_effect[effect] = tuple(merged)
        if agnostic:
            # Effect-agnostic rules must also fire for effects with no
            # specific rules; index the union so such scans find them.
            agnostic_tuple = tuple(agnostic)
            for effect in EffectClass:
                if effect not in by_effect:
                    by_effect[effect] = agnostic_tuple
        object.__setattr__(self, "_by_effect", by_effect)

    def ordered(self) -> tuple[Rule, ...]:
        return self._compiled  # type: ignore[attr-defined]

    def evaluate(
        self,
        capability_id: str,
        effects: frozenset[EffectClass],
        arguments: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Return (verdict, matched_rule_name) for the first matching rule."""
        if len(effects) == 1:
            effect = next(iter(effects))
            candidates = self._by_effect.get(effect)  # type: ignore[attr-defined]
            if candidates is not None:
                for rule in candidates:
                    if rule.matches(capability_id, effects, arguments):
                        return rule.verdict, rule.name
                return None
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
