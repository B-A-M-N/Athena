"""Neutral scope and dependency contracts used by affordance consumers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AffordanceScope(str, Enum):
    SCRATCH = "scratch"
    TASK = "task"
    CANDIDATE = "candidate"
    PROJECT = "project"
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class DependencyRequirement:
    """A dependency request that can be inspected/resolved by policy."""

    name: str
    manager: str = "python"
    version: str | None = None
    reason: str = ""
    required_for: str | None = None

    def key(self) -> str:
        return f"{self.manager}:{self.name}:{self.version or '*'}"


__all__ = ["AffordanceScope", "DependencyRequirement"]
