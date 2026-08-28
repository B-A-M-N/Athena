"""Opaque identifier generation.

Identifiers are opaque to consumers; production uses monotonic time-sortable
IDs. Consumers MUST NOT depend on identifier internals.
"""

from __future__ import annotations

import random
import time

# Identity prefixes per the implementation spec.
_PREFIXES = {
    "task": "task",
    "sess": "sess",
    "session": "session",
    "msg": "msg",
    "evt": "evt",
    "event": "event",
    "call": "call",
    "exec": "exec",
    "execution": "execution",
    "run": "run",
    "mem": "mem",
    "skill": "skill",
    "art": "art",
    "artifact": "artifact",
    "mut": "mut",
    "mutation": "mutation",
    "apr": "apr",
    "approval": "approval",
    "sched": "sched",
    "schedule": "schedule",
    "job": "job",
    "cred": "cred",
}


def _make_sortable_id() -> str:
    # Monotonic time-sortable: 12 hex digits of ms timestamp + random suffix.
    ms = int(time.time() * 1000)
    return f"{ms:012x}{random.getrandbits(48):012x}"


def new_id(kind: str) -> str:
    """Create a new opaque identifier with a typed prefix."""
    prefix = _PREFIXES.get(kind, kind)
    return f"{prefix}_{_make_sortable_id()}"


def fake_id(kind: str, n: int = 1) -> str:
    """Deterministic identifier for tests and fixtures."""
    return f"{kind}_{n:010d}"


__all__ = ["new_id", "fake_id"]
