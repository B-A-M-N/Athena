"""Build bounded, deterministic acceptance plans from project facts.

The planner is deliberately not a second reasoner.  It turns the inspected
project profile into executable criteria using a small, stable policy.  A
task with explicit acceptance criteria remains authoritative; this module is
only used when the caller omitted them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from athena.protocol.tasks import Criterion, TaskSpec, VerificationSpec, VerificationType

_CATEGORY_ORDER = ("test", "lint", "typecheck", "build", "check")
_CATEGORY_TOKENS = {
    "test": ("test", "pytest", "unittest", "spec"),
    "lint": ("lint", "ruff", "eslint", "fmt", "format"),
    "typecheck": ("typecheck", "mypy", "pyright", "tsc", "vet"),
    "build": ("build", "compile", "package"),
    "check": ("check", "verify", "validate"),
}
_BARE_LAUNCHERS = frozenset({"python", "python3", "node", "go", "cargo", "npm"})


@dataclass(frozen=True)
class VerificationPlan:
    """The deterministic criteria selected for one task."""

    criteria: tuple[Criterion, ...]
    source: str
    skipped_commands: tuple[str, ...] = ()
    plan_id: str = ""
    impacted_resources: tuple[str, ...] = ()
    impacted_tests: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    required_strength: str = "standard"
    rationale: tuple[str, ...] = ()
    index_revision: str | None = None


class VerificationPlanner:
    """Select safe project probes without inventing arbitrary commands."""

    def __init__(self, *, max_criteria: int = 8) -> None:
        if max_criteria < 1:
            raise ValueError("max_criteria must be positive")
        self._max_criteria = max_criteria

    def plan(
        self,
        task: TaskSpec,
        profile: Any,
        *,
        changed_resources: tuple[str, ...] | list[str] | None = None,
        impact: Mapping[str, Any] | None = None,
        invariants: tuple[str, ...] | list[str] | None = None,
    ) -> VerificationPlan:
        """Return a bounded plan derived only from an inspected profile.

        Commands are taken from the profile's command catalog, not from the
        task objective.  This prevents objective text from becoming an
        implicit shell execution channel.  Selection is stable: one command
        per verification category first, then additional distinct commands in
        catalog order until the bound is reached.
        """
        record = self._record(profile)
        raw_commands = record.get("commands") if record else None
        catalog = self._catalog(raw_commands)
        metadata = dict(task.metadata or {})
        resources = tuple(
            str(path)
            for path in (
                changed_resources
                or metadata.get("changed_resources")
                or metadata.get("modified_paths")
                or ()
            )
        )
        impact_record = dict(
            impact or metadata.get("impact") or metadata.get("impact_summary") or {}
        )
        index_revision = str(impact_record.get("index_revision") or "") or None
        invariant_values = tuple(
            str(value)
            for value in (
                invariants or metadata.get("invariants") or metadata.get("world_invariants") or ()
            )
        )
        focused_categories = self._focused_categories(resources, impact_record)
        category_order = tuple(
            category for category in _CATEGORY_ORDER if category in focused_categories
        ) + tuple(category for category in _CATEGORY_ORDER if category not in focused_categories)
        selected: list[tuple[str, str]] = []
        skipped: list[str] = []
        seen_commands: set[str] = set()

        # Prefer stronger, independent probes over a list of synonymous
        # commands.  A build-only project still gets its available build
        # probe; projects with tests get tests first.
        for category in category_order:
            for command in catalog:
                if command in seen_commands:
                    continue
                command_category = self._category(command)
                if command_category != category:
                    continue
                if not self._usable(command):
                    skipped.append(command)
                    seen_commands.add(command)
                    continue
                selected.append((category, command))
                seen_commands.add(command)
                break

        # Preserve useful, profile-provided variants after the representative
        # probe for each category.  This matters for multi-package projects.
        for command in catalog:
            if len(selected) >= self._max_criteria:
                break
            if command in seen_commands:
                continue
            seen_commands.add(command)
            category = self._category(command) or "project"
            if not self._usable(command):
                skipped.append(command)
                continue
            selected.append((category, command))

        criteria = tuple(
            Criterion(
                id=f"project_default:{index}",
                description=f"project default verification: {command}",
                verification=VerificationSpec(
                    type=VerificationType.COMMAND,
                    command=command,
                ),
                required=True,
            )
            for index, (_category, command) in enumerate(selected, start=1)
        )
        # This field identifies test resources from the project dependency
        # graph, not generated criterion labels or command strings.  A command
        # is an acceptance probe; an impacted test is a concrete resource that
        # explains why that probe was selected.
        raw_impacted_tests = impact_record.get("affected_tests") or ()
        if not isinstance(raw_impacted_tests, (list, tuple, set)):
            raw_impacted_tests = ()
        impacted_tests = tuple(
            sorted({str(path) for path in raw_impacted_tests if str(path).strip()})
        )
        rationale = []
        if resources:
            rationale.append("selection includes probes relevant to changed resources")
        if focused_categories:
            rationale.append("focused categories: " + ", ".join(sorted(focused_categories)))
        if invariant_values:
            rationale.append("task supplied explicit invariants")
        if not rationale:
            rationale.append("bounded independent probes from project command catalog")
        required_strength = (
            "strong"
            if resources and (len(resources) > 1 or focused_categories & {"build", "check"})
            else "standard"
        )
        plan_payload = {
            "commands": [command for _category, command in selected],
            "resources": resources,
            "invariants": invariant_values,
            "strength": required_strength,
            "index_revision": index_revision,
        }
        plan_id = (
            "plan_"
            + hashlib.sha256(
                json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:20]
        )
        return VerificationPlan(
            criteria=criteria,
            source="project_profile_command_catalog",
            skipped_commands=tuple(skipped),
            plan_id=plan_id,
            impacted_resources=resources,
            impacted_tests=impacted_tests,
            invariants=invariant_values,
            required_strength=required_strength,
            rationale=tuple(rationale),
            index_revision=index_revision,
        )

    @staticmethod
    def _focused_categories(
        resources: tuple[str, ...],
        impact: Mapping[str, Any],
    ) -> set[str]:
        focused: set[str] = set()
        for raw in resources:
            suffix = raw.rsplit("/", 1)[-1].casefold()
            if suffix.endswith((".py", ".js", ".ts", ".rs", ".go", ".java")):
                focused.update({"test", "lint", "typecheck"})
            if (
                suffix
                in {
                    "pyproject.toml",
                    "package.json",
                    "package-lock.json",
                    "cargo.toml",
                    "cargo.lock",
                    "go.mod",
                    "pom.xml",
                    "build.gradle",
                    "dockerfile",
                    "makefile",
                }
                or "lock" in suffix
            ):
                focused.update({"test", "build", "check"})
        for key, value in impact.items():
            if not value:
                continue
            category = str(key).casefold()
            if category in _CATEGORY_ORDER:
                focused.add(category)
        return focused

    @staticmethod
    def _record(profile: Any) -> Mapping[str, Any]:
        if isinstance(profile, Mapping):
            return profile
        to_dict = getattr(profile, "to_dict", None)
        value = to_dict() if callable(to_dict) else {}
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _catalog(raw_commands: Any) -> list[str]:
        values = raw_commands.values() if isinstance(raw_commands, Mapping) else ()
        output: list[str] = []
        for raw in values:
            candidates = (raw,) if isinstance(raw, str) else raw
            if not isinstance(candidates, (list, tuple)):
                continue
            for value in candidates:
                command = str(value).strip()
                if command and command not in output:
                    output.append(command)
        return output

    @staticmethod
    def _category(command: str) -> str | None:
        lowered = command.casefold()
        for category in _CATEGORY_ORDER:
            if any(token in lowered for token in _CATEGORY_TOKENS[category]):
                return category
        return None

    @staticmethod
    def _usable(command: str) -> bool:
        """Reject only obviously interactive or empty catalog entries."""
        if not command or command.casefold() in _BARE_LAUNCHERS:
            return False
        # The profile currently emits bounded command strings.  These
        # operators would make a catalog entry execute an unrelated command,
        # so don't turn them into completion authority without an explicit
        # criterion supplied by the caller.
        return not any(token in command for token in ("\n", "\r", "&&", ";", "|", ">", "<"))


__all__ = ["VerificationPlan", "VerificationPlanner"]
