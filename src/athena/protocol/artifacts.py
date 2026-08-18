"""Artifact references and metadata.

Artifacts are content-addressed immutable blobs stored outside conversation
messages. Large outputs MUST move out of messages and into artifacts
(BUILDSPEC sections 53-54).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    uri: str            # artifact://sha256/<digest>
    hash: str | None = None
    mime_type: str | None = None
    size: int | None = None
    storage_path: str | None = None
    created_at: datetime | None = None
    producer: str | None = None
    task_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def parse_artifact_uri(uri: str) -> tuple[str, str] | None:
    """Parse an artifact uri into (scheme-tail, digest/path)."""
    if uri.startswith("artifact://"):
        rest = uri[len("artifact://"):]
        if "/" in rest:
            scheme, key = rest.split("/", 1)
            return scheme, key
        return None
    return None


__all__ = ["ArtifactRef", "parse_artifact_uri"]