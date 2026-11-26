"""Reflection/discovery vocabulary and ranking for the capability fabric.

Subordinate to :class:`athena.affordances.fabric.CapabilityFabric`. This module
owns progressive-disclosure tokenization, relevance scoring, and advisory
dependency fingerprints. It does not authorize invocation or execute effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from athena.affordances.models import GeneratedCapability
from athena.protocol.tasks import WorkspaceSpec

AUTOMATIC_DISCLOSURE = "automatic_disclosure"
EXPLICIT_REFLECTION_SEARCH = "explicit_reflection_search"

__all__ = [
    "AUTOMATIC_DISCLOSURE",
    "EXPLICIT_REFLECTION_SEARCH",
    "capability_search_tokens",
    "capability_synonyms",
    "dependency_state_fingerprint",
]


# Search is progressive disclosure, not a full-text index. These terms are
# intentionally small and operator-facing: a capability earns visibility from
# an identity/tag/synonym hit, while arbitrary prose in a long descriptor
# cannot make it appear relevant to a casual question.
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "be",
        "can",
        "do",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "the",
        "tell",
        "that",
        "this",
        "to",
        "what",
        "with",
        "you",
        "your",
    }
)
_SEARCH_WEAK_TERMS = frozenset(
    {
        "all",
        "bounded",
        "current",
        "local",
        "normal",
        "output",
        "project",
        "return",
        "short",
        "task",
        "use",
        "used",
        "using",
        "work",
    }
)

# Tags are the primary vocabulary. This tiny table exists only for legacy
# vocabulary that cannot be expressed by a canonical descriptor ID alone;
# native, MCP, pack, and generated descriptors should publish their own tags.
_CAPABILITY_SYNONYMS: dict[str, frozenset[str]] = {
    "fs": frozenset({"file_ops"}),
    "execute": frozenset({"command_runner"}),
    "research": frozenset({"web_lookup"}),
}


def capability_search_tokens(value: Any, *, include_weak: bool = True) -> frozenset[str]:
    """Tokenize human words without turning prose substrings into matches."""
    tokens = {
        token.casefold()
        for token in re.findall(r"[a-zA-Z0-9]+", str(value or ""))
        if len(token) >= 2 and token.casefold() not in _SEARCH_STOPWORDS
    }
    if not include_weak:
        tokens.difference_update(_SEARCH_WEAK_TERMS)
    return frozenset(tokens)


def capability_synonyms(part: str) -> frozenset[str]:
    """Return operator-facing synonyms for a canonical capability id part."""
    return _CAPABILITY_SYNONYMS.get(part, frozenset())


def dependency_state_fingerprint(
    workspace: WorkspaceSpec | None,
    record: GeneratedCapability | None,
) -> str:
    """Identify filesystem/runtime facts used by generated dependency checks."""
    if workspace is None or record is None or not record.required_dependencies:
        return ""
    root = Path(workspace.root).resolve()
    facts: list[tuple[str, Any]] = [("root", str(root))]
    for relative in (".athena/dependencies.lock.json", ".athena/dependencies"):
        path = root / relative
        try:
            stat = path.stat()
            facts.append((relative, (stat.st_mtime_ns, stat.st_size, stat.st_ino)))
            if path.is_file():
                facts.append((relative + ":sha256", hashlib.sha256(path.read_bytes()).hexdigest()))
        except OSError:
            facts.append((relative, None))
    facts.extend(
        (
            ("requirements", tuple(item.key() for item in record.required_dependencies)),
            ("python", os.path.realpath(sys.executable)),
            ("version", tuple(sys.version_info[:3])),
        )
    )
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
