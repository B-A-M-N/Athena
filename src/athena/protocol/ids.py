"""Opaque identifier generation.

Identifiers are opaque to consumers; production uses time-sortable IDs with a
random suffix. They are not a causal append sequence, so durable stores must
use their own insertion/sequence ordering when order matters.
"""

from __future__ import annotations

import random
import time
import hashlib

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
    # Time-sortable: 12 hex digits of ms timestamp + random suffix. The random
    # suffix is intentionally not used as a causal tie-breaker.
    ms = int(time.time() * 1000)
    return f"{ms:012x}{random.getrandbits(48):012x}"


def new_id(kind: str) -> str:
    """Create a new opaque identifier with a typed prefix."""
    prefix = _PREFIXES.get(kind, kind)
    return f"{prefix}_{_make_sortable_id()}"


def fake_id(kind: str, n: int = 1) -> str:
    """Deterministic identifier for tests and fixtures."""
    return f"{kind}_{n:010d}"


def stable_id(kind: str, *parts: object) -> str:
    """Create a deterministic opaque identifier from stable identity parts."""
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[
        :32
    ]
    prefix = _PREFIXES.get(kind, kind)
    return f"{prefix}_{digest}"


__all__ = ["new_id", "fake_id", "stable_id"]
