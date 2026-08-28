"""Athena memory subsystem.

Implements the durable, curated knowledge layer defined in BUILDSPEC section 61
and SPEC section 23. Distinct from session history and task state (BUILDSPEC 64,
BHV-099). Exposes persistence, retrieval, conflict handling, and candidate
generation.
"""

from athena.memory.store import MemoryStore, new_memory_id
from athena.memory.retrieval import MemoryRetriever
from athena.memory.conflicts import (
    MemoryConflictResolver,
    ConflictReport,
    ConflictResult,
    ConflictResolution,
)
from athena.memory.candidates import candidates_from_task

__all__ = [
    "MemoryStore",
    "MemoryRetriever",
    "MemoryConflictResolver",
    "ConflictReport",
    "ConflictResult",
    "ConflictResolution",
    "candidates_from_task",
    "new_memory_id",
]
