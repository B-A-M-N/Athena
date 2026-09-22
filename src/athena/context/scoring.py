"""Shared scoring and classification helpers for context compilation.

Owned by neither the compiler nor retrieval; these are pure functions.
Extracted from the compiler to remove dynamic owner-backimports (review
item 15).
"""

from __future__ import annotations

import re
from typing import Any

MEMORY_SCOPE_WEIGHTS = {
    "SESSION": 1.0,
    "JOB": 0.9,
    "PROJECT": 0.6,
    "USER": 0.45,
    "GLOBAL": 0.3,
}

# WORK-mode floor: a memory must overlap at least this fraction of the
# objective's tokens to earn context space on an ordinary work turn.
WORK_MATCH_FLOOR = 0.34

_DEFAULT_FALLBACK_BUNDLE: tuple[str, ...] = ("fs", "git", "capabilities")


def objective_tokens(objective: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(objective or "").casefold()))


def strong_matches(objective: str, records: list[Any]) -> list[Any]:
    """Keep records whose token overlap clears the WORK-mode floor."""
    qset = objective_tokens(objective)
    if not qset:
        return []
    kept: list[Any] = []
    for rec in records:
        text = str(
            getattr(rec, "content", None)
            or (rec.get("content") if isinstance(rec, dict) else None)
            or getattr(rec, "summary", None)
            or (rec.get("summary") if isinstance(rec, dict) else None)
            or ""
        )
        tset = objective_tokens(text)
        if not tset:
            continue
        overlap = len(qset & tset) / len(qset)
        if overlap >= WORK_MATCH_FLOOR:
            kept.append(rec)
    return kept


def fallback_bundle_ids(objective: str) -> tuple[str, ...]:
    """Map an action-shaped miss to the smallest foundational capability set."""
    tokens = objective_tokens(objective)
    if tokens & {
        "debug",
        "fix",
        "failing",
        "failure",
        "broken",
        "crash",
        "crashed",
        "error",
        "errors",
        "bug",
        "regression",
        "test",
        "tests",
        "pytest",
        "lint",
        "traceback",
        "stack",
        "trace",
    }:
        return ("fs", "execute", "diagnostics", "git", "capabilities")
    if tokens & {
        "commit",
        "branch",
        "merge",
        "rebase",
        "push",
        "pull",
        "git",
        "changelog",
        "blame",
        "revert",
        "tag",
        "stash",
    }:
        return ("git", "fs", "capabilities")
    if tokens & {
        "remember",
        "recall",
        "earlier",
        "previous",
        "before",
        "preference",
        "favorite",
        "memory",
        "last",
        "decided",
        "chose",
        "said",
        "discussed",
    }:
        return ("memory", "session_search", "capabilities")
    if tokens & {
        "research",
        "investigate",
        "evidence",
        "source",
        "sources",
        "latest",
        "release",
        "protocol",
        "study",
        "compare",
        "survey",
    }:
        return ("research", "capabilities")
    if tokens & {
        "terminal",
        "session",
        "pty",
        "watch",
        "stream",
        "long-running",
        "background",
        "process",
        "daemon",
        "server",
        "serve",
    }:
        return ("execute", "terminal_session", "process", "capabilities")
    return _DEFAULT_FALLBACK_BUNDLE


DEFAULT_FALLBACK_BUNDLE = _DEFAULT_FALLBACK_BUNDLE
