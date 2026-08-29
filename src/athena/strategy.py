"""Deterministic affordance guidance for the single Athena model loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable, Mapping
import re
from typing import Any


@dataclass(frozen=True)
class StrategyAffordance:
    """Facts about one affordance visible to the advisory selector."""

    id: str
    description: str = ""
    available: bool = True
    scope: str = "system"
    dependency_ready: bool = True
    environment_compatible: bool = True
    proof: Mapping[str, Any] = field(default_factory=dict)
    effects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "description": self.description,
            "available": self.available,
            "scope": self.scope,
            "dependency_ready": self.dependency_ready,
            "environment_compatible": self.environment_compatible,
            "proof": dict(self.proof),
            "effects": self.effects,
            "tags": self.tags,
            "output_schema": dict(self.output_schema),
        }


@dataclass(frozen=True)
class StrategyGuidance:
    """A small, model-visible hint—not a second planner or execution path."""

    route: str
    rationale: str
    candidates: tuple[str, ...] = ()
    missing_affordance: str | None = None
    gap_kind: str | None = None
    route_kind: str = "existing_primitive"
    affordances: tuple[StrategyAffordance, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "rationale": self.rationale,
            "candidates": self.candidates,
            "missing_affordance": self.missing_affordance,
            "gap_kind": self.gap_kind,
            "route_kind": self.route_kind,
            "affordances": tuple(item.to_dict() for item in self.affordances),
        }


def select_strategy(
    objective: str,
    capability_ids: Iterable[str | Mapping[str, Any] | Any],
) -> StrategyGuidance:
    """Select bounded advisory guidance from visible affordance evidence.

    The model still chooses the actual calls. This function only makes the
    existing architecture explicit and observable. Callers may provide legacy
    ids, descriptors, or fabric search records; all are normalized into the
    same typed evidence before selection.
    """
    text = str(objective or "").casefold()
    affordances = tuple(_coerce_affordance(value) for value in capability_ids)
    available = {
        item.id
        for item in affordances
        if item.available and item.dependency_ready and item.environment_compatible
    }

    # An empty inventory is different from a known missing affordance.  Small
    # compiler/unit fixtures and restricted deployments may intentionally
    # expose no capabilities; do not turn that absence into a model-facing
    # claim that a preferred capability should be built.
    if not affordances:
        return StrategyGuidance(
            route="direct",
            rationale="No capability inventory is available; follow the task with the currently exposed interface.",
            gap_kind="empty_inventory",
            affordances=affordances,
        )

    # Route is advisory evidence ranking. Objective terms are only signals in
    # the score; the actual route is selected from the visible descriptor
    # inventory, declared effects/tags, and readiness proof. This keeps a
    # missing capability observable without making a keyword switch the
    # execution planner.
    profiles: tuple[Mapping[str, Any], ...] = (
        {
            "route": "fusion",
            "preferred": ("fusion", "workflow", "fs", "execute"),
            "signals": {"experiment", "shadow", "speculative", "fork", "branch"},
            "tags": {"fusion", "shadow", "experiment"},
            "effects": {"execute", "write_local"},
            "rationale": "Bounded speculative work should be proven in a shadow before commit.",
        },
        {
            "route": "evidence_acquisition",
            "preferred": ("research", "workflow", "artifacts"),
            "signals": {"research", "compare", "sources", "evidence", "investigate"},
            "tags": {"research", "evidence", "sources"},
            "effects": {"read_local", "network_read"},
            "rationale": "Sourced work needs bounded acquisition and explicit gap handling.",
        },
        {
            "route": "synthesize",
            "preferred": ("synthesis", "scratch", "workflow"),
            "signals": {"tool", "automate", "generate", "capability", "helper"},
            "tags": {"synthesis", "generated", "tool"},
            "effects": {"execute", "write_local"},
            "rationale": "Reusable behavior should be validated task-locally before promotion.",
        },
        {
            "route": "compose",
            "preferred": ("workflow", "execute", "fs"),
            "signals": {"workflow", "pipeline", "release", "deploy", "steps"},
            "tags": {"workflow", "pipeline", "compose"},
            "effects": {"execute", "write_local"},
            "rationale": "Ordered work should use a bounded workflow when one exists.",
        },
        {
            "route": "direct",
            "preferred": ("capabilities", "execute", "fs", "workflow", "scratch", "synthesis"),
            "signals": set(),
            "tags": {"primitive", "native"},
            "effects": {"execute", "read_local"},
            "priority": 1,
            "rationale": "Start with the smallest visible primitive; compose or build only when it is insufficient.",
        },
    )
    terms = set(_tokens(text))

    def profile_score(profile: Mapping[str, Any]) -> tuple[int, int, int, str]:
        preferred_ids = tuple(profile["preferred"])
        evidence_score = 0
        for item in affordances:
            if not item.available:
                continue
            id_match = next(
                (
                    index
                    for index, preferred in enumerate(preferred_ids)
                    if _matches(item.id, preferred)
                ),
                None,
            )
            tag_match = bool(set(item.tags) & set(profile["tags"]))
            effect_match = bool(set(item.effects) & set(profile["effects"]))
            description_match = bool(terms & set(_tokens(item.description)))
            if id_match is not None:
                # Identity is useful evidence, but only a weak prior.  A
                # route must earn its score from the declared affordance
                # surface, effects, tags, and proof rather than winning by a
                # longer list of familiar substrings.
                evidence_score += 2 if id_match == 0 else 1
            if tag_match:
                evidence_score += 3
            if effect_match:
                evidence_score += 1
            if description_match and (id_match is not None or tag_match):
                evidence_score += 1
            if item.proof.get("all_passed") is True or item.proof.get("validation_state") in {
                "VALIDATED",
                "PROMOTED",
            }:
                evidence_score += 1
        signal_score = len(terms & set(profile["signals"]))
        # Objective vocabulary is a bounded prior, not a route switch. A
        # specialized signal can expose a missing route, while structured
        # affordance evidence can win when the wording is novel.
        return (
            signal_score * 2 + evidence_score,
            evidence_score,
            int(profile.get("priority", 0)),
            profile["route"],
        )

    selected_profile = max(profiles, key=profile_score)
    preferred = tuple(selected_profile["preferred"])
    route = str(selected_profile["route"])
    rationale = str(selected_profile["rationale"])

    candidates = tuple(
        capability
        for capability in preferred
        if any(_matches(item_id, capability) for item_id in available)
    )
    # The first candidate names the selected route's primary affordance.  A
    # convenient fallback must not make a materially different route look
    # equivalent to the requested one.
    preferred_record = (
        next((item for item in affordances if _matches(item.id, preferred[0])), None)
        if preferred
        else None
    )
    # Direct work can use any visible primitive in its ordered preference
    # list; the absence of the optional ``capabilities`` umbrella must not
    # turn an otherwise usable ``execute`` or ``fs`` primitive into a gap.
    primary_missing = not any(_matches(item_id, preferred[0]) for item_id in available)
    missing = (
        preferred[0]
        if preferred and primary_missing and (route != "direct" or not candidates)
        else None
    )
    if missing is not None:
        gap_kind = "missing_affordance"
        if preferred_record is not None and not preferred_record.dependency_ready:
            gap_kind = "dependency_unready"
        elif preferred_record is not None and not preferred_record.environment_compatible:
            gap_kind = "environment_incompatible"
        elif preferred_record is not None and not preferred_record.available:
            gap_kind = "unavailable"
        return StrategyGuidance(
            route="affordance_gap",
            rationale=f"Preferred route {missing!r} is not currently available; inspect or build a bounded replacement.",
            candidates=(),
            missing_affordance=missing,
            gap_kind=gap_kind,
            route_kind=route_kind_for(route),
            affordances=affordances,
        )
    return StrategyGuidance(
        route,
        rationale,
        candidates,
        route_kind=route_kind_for(route),
        affordances=affordances,
    )


def _coerce_affordance(value: str | Mapping[str, Any] | Any) -> StrategyAffordance:
    if isinstance(value, StrategyAffordance):
        return value
    if isinstance(value, str):
        return StrategyAffordance(id=value)
    if isinstance(value, Mapping):
        optimizer = value.get("optimizer")
        optimizer = optimizer if isinstance(optimizer, Mapping) else {}
        return StrategyAffordance(
            id=str(value.get("id") or ""),
            description=str(value.get("description") or ""),
            available=str(value.get("availability") or "available") == "available"
            and bool(value.get("available", True)),
            scope=str(value.get("scope") or "system"),
            dependency_ready=bool(
                value.get("dependency_ready", optimizer.get("dependency_available", True))
            ),
            environment_compatible=bool(
                value.get(
                    "environment_compatible",
                    optimizer.get("environment_compatible", True),
                )
            ),
            proof=dict(value.get("proof") or optimizer),
            effects=tuple(sorted(_string_values(value.get("effects")))),
            tags=tuple(sorted(_string_values(value.get("tags")))),
            output_schema=dict(value.get("output_schema") or {}),
        )
    identifier = str(getattr(value, "id", ""))
    availability = getattr(getattr(value, "availability", None), "value", "available")
    origin = getattr(getattr(value, "origin", None), "value", "system")
    return StrategyAffordance(
        id=identifier,
        description=str(getattr(value, "description", "") or ""),
        available=availability == "available",
        scope=origin,
        effects=tuple(sorted(_string_values(getattr(value, "effects", ())))),
        tags=tuple(sorted(_string_values(getattr(value, "tags", ())))),
        output_schema=dict(getattr(value, "output_schema", None) or {}),
    )


def _string_values(values: Any) -> set[str]:
    if isinstance(values, str):
        values = (values,)
    return {
        str(getattr(value, "value", value)).casefold()
        for value in (values or ())
        if str(getattr(value, "value", value)).strip()
    }


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9_.-]+", value.casefold()))


def _matches(identifier: str, preferred: str) -> bool:
    return identifier == preferred or identifier.startswith(preferred + ".")


def route_kind_for(route: str) -> str:
    return {
        "direct": "existing_primitive",
        "compose": "workflow_composition",
        "synthesize": "generated_capability",
        "evidence_acquisition": "research_evidence",
        "fusion": "fusion_shadow",
    }.get(route, "affordance_gap")


__all__ = ["StrategyAffordance", "StrategyGuidance", "select_strategy"]
