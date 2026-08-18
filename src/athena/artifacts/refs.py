"""Artifact reference helpers and result artifactization (§54, BHV-065).

Large outputs MUST NOT be injected wholesale into model context. When a value
exceeds the configured threshold it is artifactized: the full blob is written
to the ArtifactStore out-of-band, and only a bounded excerpt plus an immutable
ArtifactRef reaches the model.
"""

from __future__ import annotations

import hashlib
from typing import Mapping

from athena.artifacts.store import ArtifactStore
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import Provenance

DEFAULT_ARTIFACT_THRESHOLD = 50 * 1024  # 50 KiB
EXCERPT_LIMIT = 1000


def build_uri(content: bytes | str) -> str:
    """Build the content-addressed ``artifact://sha256/<digest>`` uri."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(data).hexdigest()
    return f"artifact://sha256/{digest}"


def built_from(content: bytes | str) -> str:
    """Sha256 of ``content``; identity for the content-addressed artifact."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def size_bytes(content: bytes | str) -> int:
    return len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))


async def artifactize_output(
    store: ArtifactStore,
    content: bytes | str,
    *,
    mime_type: str = "application/octet-stream",
    task_id: str | None = None,
    producer: Provenance | str | None = None,
    metadata: Mapping | None = None,
) -> ArtifactRef:
    """Write ``content`` to the store and return its immutable ref."""
    return await store.save(
        task_id=task_id,
        content=content,
        mime_type=mime_type,
        producer=producer,
        metadata=metadata,
    )


async def maybe_artifactize(
    store: ArtifactStore,
    content: bytes | str,
    *,
    threshold: int = DEFAULT_ARTIFACT_THRESHOLD,
    mime_type: str = "application/octet-stream",
    task_id: str | None = None,
    producer: Provenance | str | None = None,
    metadata: Mapping | None = None,
) -> ArtifactRef | str:
    """Return an ArtifactRef for large content, else the inline content string.

    Out-of-band (≤ threshold): content is returned inline.  Above threshold the
    full blob is written to ``store`` (BHV-065) and only the ref is returned.
    """
    size = size_bytes(content)
    if size < threshold:
        return content.decode("utf-8") if isinstance(content, bytes) else content
    return await artifactize_output(
        store,
        content,
        mime_type=mime_type,
        task_id=task_id,
        producer=producer,
        metadata=metadata,
    )


def legitimate_excerpt(content: bytes | str, limit: int = EXCERPT_LIMIT) -> str:
    """Return a bounded excerpt; callers use this instead of the full blob."""
    text = (
        content.decode("utf-8", errors="replace")
        if isinstance(content, bytes)
        else content
    )
    return text[:limit]


__all__ = [
    "maybe_artifactize",
    "artifactize_output",
    "build_uri",
    "built_from",
    "size_bytes",
    "legitimate_excerpt",
    "DEFAULT_ARTIFACT_THRESHOLD",
    "EXCERPT_LIMIT",
]
