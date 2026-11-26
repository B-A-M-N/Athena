"""Research service result and provenance codecs.

These helpers translate between durable research records and the canonical
capability-result envelope.  They own no policy and do not decide completion;
callers retain task/project visibility decisions at the service boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
)
from athena.research.models import EvidenceObject, SourceRecord


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def strings(value: Any, *, limit: int) -> list[str]:
    """Return bounded, non-empty strings from a schema-validated list."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value[:limit] if str(item).strip()]


def unique_strings(values: list[str]) -> list[str]:
    """Deduplicate bounded workflow queries without changing their order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def decode_object(value: str) -> dict[str, Any]:
    """Decode an internal capability response without trusting its shape."""
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def unique_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate local search hits while preserving query/rank order."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        source = candidate.get("source") if isinstance(candidate, Mapping) else None
        source_id = str(source.get("id") or "") if isinstance(source, Mapping) else ""
        key = source_id or json.dumps(candidate, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


async def index_snapshot(
    store,
    source: SourceRecord,
    snapshot: bytes | str | None,
    *,
    mime_type: str,
) -> None:
    """Populate the durable lexical projection when the store supports it."""
    index = getattr(store, "index_content", None)
    if index is None or snapshot is None:
        return
    if isinstance(snapshot, bytes):
        media = str(mime_type or "").lower()
        if not (
            media.startswith("text/")
            or media.endswith(("+json", "+xml"))
            or media
            in {
                "application/json",
                "application/xml",
                "application/javascript",
            }
        ):
            return
        text = snapshot.decode("utf-8", errors="replace")
        content_hash = hashlib.sha256(snapshot).hexdigest()
    else:
        text = str(snapshot)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    await index(
        source.id,
        text,
        content_hash=content_hash,
        mime_type=str(mime_type or ""),
    )


def result(
    request,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapabilityResult:
    """Build the canonical research result without inventing success."""
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


def source_visible(source: SourceRecord, request: CapabilityRequest, context: Any) -> bool:
    """Return whether a source belongs to this task or its project overlay."""
    if source.task_id == request.task_id:
        return True
    project_id = getattr(getattr(context, "workspace", None), "id", None)
    return source.task_id is None and bool(project_id) and source.project_id == project_id


async def evidence_visible(
    evidence: EvidenceObject,
    request: CapabilityRequest,
    context: Any,
    source_lookup,
) -> bool:
    """Apply task/project visibility to an evidence object and its source."""
    if evidence.task_id not in (None, request.task_id):
        return False
    source = await source_lookup(evidence.source_id)
    return source is not None and source_visible(source, request, context)


async def artifact_visible(artifacts: Any, uri: str, task_id: str) -> bool:
    """Artifact URIs are references, not bearer credentials."""
    refs = await artifacts.list(task_id=task_id, limit=1000)
    return any(getattr(ref, "uri", None) == uri for ref in refs)


__all__ = [
    "artifact_visible",
    "decode_object",
    "evidence_visible",
    "index_snapshot",
    "json_text",
    "result",
    "source_visible",
    "strings",
    "unique_candidates",
    "unique_strings",
]
