"""Task-scoped inspection of immutable execution artifacts.

Execution deliberately returns only a bounded preview for large output.  This
capability is the OI-style follow-up surface: the model can retrieve a slice or
search the full captured result later without re-running the computation or
placing the entire blob in the conversation.  Artifact ownership is checked
before every read; knowing an artifact URI is not sufficient authority.
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from athena.protocol.artifacts import parse_artifact_uri
from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)


@dataclass(frozen=True)
class ExtractionResult:
    """Bounded, deterministic output from a registered MIME extractor."""

    text: str = ""
    mime_type: str = "text/plain"
    truncated: bool = False
    metadata: Mapping[str, Any] = dataclass_field(default_factory=dict)


Extractor = Callable[[bytes, bool], ExtractionResult]


class MimeExtractorRegistry:
    """Allow-list of pure, deterministic artifact extractors.

    The registry contains callables supplied by Athena code only. It never
    imports or executes a handler named by artifact metadata, so extraction
    cannot become a plugin execution path.
    """

    def __init__(self) -> None:
        self._extractors: dict[str, Extractor] = {
            "application/json": _extract_json,
            "text/*": _extract_text,
        }

    def register(self, mime_type: str, extractor: Extractor) -> None:
        key = str(mime_type or "").strip().lower()
        if not key or "/" not in key:
            raise ValueError("extractor MIME type must be non-empty")
        self._extractors[key] = extractor

    def extract(
        self, mime_type: str | None, content: bytes, *, truncated: bool
    ) -> ExtractionResult:
        media = str(mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
        extractor = self._extractors.get(media)
        if extractor is None and media.startswith("text/"):
            extractor = self._extractors["text/*"]
        if extractor is None:
            return ExtractionResult(
                mime_type=media,
                truncated=truncated,
                metadata={"binary": True, "bytes": len(content)},
            )
        result = extractor(content, truncated)
        return ExtractionResult(
            text=result.text,
            mime_type=result.mime_type,
            truncated=result.truncated or truncated,
            metadata=dict(result.metadata),
        )


def _extract_text(content: bytes, truncated: bool) -> ExtractionResult:
    return ExtractionResult(
        text=content.decode("utf-8", errors="replace"),
        mime_type="text/plain",
        truncated=truncated,
        metadata={"encoding": "utf-8"},
    )


def _extract_json(content: bytes, truncated: bool) -> ExtractionResult:
    text = content.decode("utf-8", errors="replace")
    metadata: dict[str, Any] = {"encoding": "utf-8", "structured": True}
    if truncated:
        metadata["parse_status"] = "bounded_preview"
        return ExtractionResult(
            text=text, mime_type="application/json", truncated=True, metadata=metadata
        )
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        metadata["parse_status"] = "invalid_json"
        return ExtractionResult(text=text, mime_type="application/json", metadata=metadata)
    metadata["parse_status"] = "parsed"
    return ExtractionResult(
        text=json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        mime_type="application/json",
        metadata=metadata,
    )


class ArtifactCapability:
    descriptor = CapabilityDescriptor(
        id="artifacts",
        description=(
            "Inspect task-owned immutable output artifacts without rerunning "
            "the operation. Operations: list/read/slice/search/extract. Reads "
            "are bounded; use offset/limit or search for large results."
        ),
        input_schema={
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"operation": {"const": "list"}},
                    "required": ["operation"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "read"},
                        "artifact_uri": {"type": "string", "minLength": 1},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 65_536},
                    },
                    "required": ["operation", "artifact_uri"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "slice"},
                        "artifact_uri": {"type": "string", "minLength": 1},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 65_536},
                    },
                    "required": ["operation", "artifact_uri"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "search"},
                        "artifact_uri": {"type": "string", "minLength": 1},
                        "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["operation", "artifact_uri", "query"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"const": "extract"},
                        "artifact_uri": {"type": "string", "minLength": 1},
                        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
                    },
                    "required": ["operation", "artifact_uri"],
                    "additionalProperties": False,
                },
            ],
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(
        self,
        store,
        *,
        max_read_bytes: int = 65_536,
        extractor_registry: MimeExtractorRegistry | None = None,
    ) -> None:
        self._store = store
        self._max_read_bytes = max(1, min(max_read_bytes, 65_536))
        self._extractors = extractor_registry or MimeExtractorRegistry()

    async def invoke(self, request: CapabilityRequest, **kw) -> CapabilityResult:
        args = dict(request.arguments or {})
        operation = str(args.get("operation") or "")
        if request.task_id is None:
            return _result(request, ok=False, error="artifact access requires a task")
        try:
            if operation == "list":
                refs = await self._store.list(task_id=request.task_id, limit=100)
                return _result(
                    request,
                    output=_json(
                        {
                            "artifacts": [_ref_record(ref) for ref in refs],
                        }
                    ),
                )
            uri = str(args.get("artifact_uri") or "")
            if not _valid_artifact_uri(uri):
                return _result(request, ok=False, error="artifact_uri is invalid")
            owned = await self._owned_ref(uri, request.task_id)
            if owned is None:
                return _result(request, ok=False, error="artifact is not visible to this task")
            if operation in {"read", "slice"}:
                return await self._read(request, args, uri)
            if operation == "search":
                return await self._search(request, args, uri)
            if operation == "extract":
                return await self._extract(request, args, uri, owned)
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        except (OSError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))
        except Exception as exc:
            return _result(request, ok=False, error=f"artifact operation failed: {exc}")

    async def _owned_ref(self, uri: str, task_id: str):
        find_occurrence = getattr(self._store, "find_occurrence", None)
        if callable(find_occurrence):
            return await find_occurrence(task_id=task_id, uri=uri)
        # Legacy stores may not expose the digest-sidecar index. Exhaust that
        # store's ownership view rather than imposing a page-size authority
        # boundary that makes later artifacts invisible.
        refs = await self._store.list(task_id=task_id, limit=None)
        return next((ref for ref in refs if ref.uri == uri), None)

    async def _read(self, request, args: dict[str, Any], uri: str) -> CapabilityResult:
        offset = int(args.get("offset") or 0)
        limit = min(int(args.get("limit") or self._max_read_bytes), self._max_read_bytes)
        chunk, size, eof = await self._text_slice(uri, offset, limit)
        next_offset = offset + len(chunk)
        return _result(
            request,
            output=_json(
                {
                    "artifact_uri": uri,
                    "offset": offset,
                    "content": chunk,
                    "next_offset": next_offset,
                    "eof": eof,
                    "size": size,
                }
            ),
        )

    async def _search(self, request, args: dict[str, Any], uri: str) -> CapabilityResult:
        query = str(args.get("query") or "")
        if not query:
            return _result(request, ok=False, error="search requires query")
        needle = query.casefold()
        limit = min(int(args.get("max_results") or 20), 100)
        matches: list[dict[str, Any]] = []
        rolling = ""
        line_number = 1
        offset = 0
        line_preview: list[str] = []
        async for text in self._text_chunks(uri):
            for char in text:
                if char == "\n":
                    line_number += 1
                    line_preview = []
                elif len(line_preview) < self._max_read_bytes:
                    line_preview.append(char)
                rolling = (rolling + char)[-len(needle) :]
                if rolling.casefold().endswith(needle):
                    matches.append(
                        {
                            "offset": offset - len(query) + 1,
                            "line": line_number,
                            "text": "".join(line_preview),
                        }
                    )
                    if len(matches) >= limit:
                        break
                offset += 1
            if len(matches) >= limit:
                break
        return _result(
            request,
            output=_json(
                {
                    "artifact_uri": uri,
                    "query": query,
                    "matches": matches,
                }
            ),
        )

    async def _extract(self, request, args: dict[str, Any], uri: str, ref) -> CapabilityResult:
        limit = max(
            1, min(int(args.get("max_bytes") or self._max_read_bytes), self._max_read_bytes)
        )
        content, truncated = await self._bounded_bytes(uri, limit)
        result = self._extractors.extract(ref.mime_type, content, truncated=truncated)
        metadata = {
            "source_uri": uri,
            "source_hash": ref.hash,
            "source_mime_type": ref.mime_type,
            "extractor_mime_type": result.mime_type,
            **dict(result.metadata),
        }
        derived = None
        save = getattr(self._store, "save", None)
        if result.text and save is not None:
            derived_ref = await self._store.save(
                task_id=request.task_id,
                content=result.text,
                mime_type=result.mime_type,
                producer="artifact-extractor",
                metadata=metadata,
            )
            derived = _ref_record(derived_ref)
        return _result(
            request,
            output=_json(
                {
                    "artifact_uri": uri,
                    "mime_type": ref.mime_type,
                    "text": result.text,
                    "truncated": result.truncated,
                    "metadata": metadata,
                    "derived_artifact": derived,
                }
            ),
            metadata=metadata,
        )

    async def _bounded_bytes(self, uri: str, limit: int) -> tuple[bytes, bool]:
        """Read at most ``limit`` bytes plus one byte to detect truncation."""
        data = bytearray()
        async for chunk in self._byte_chunks(uri):
            remaining = limit + 1 - len(data)
            if remaining <= 0:
                break
            data.extend(chunk[:remaining])
            if len(data) > limit:
                return bytes(data[:limit]), True
        return bytes(data), False

    async def _text_slice(self, uri: str, offset: int, limit: int) -> tuple[str, int, bool]:
        """Read a bounded character slice while keeping blob memory bounded."""
        pieces: list[str] = []
        collected = 0
        position = 0
        total = 0
        async for text in self._text_chunks(uri):
            total += len(text)
            if position + len(text) <= offset:
                position += len(text)
                continue
            start = max(0, offset - position)
            remaining = limit - collected
            if remaining > 0:
                piece = text[start : start + remaining]
                pieces.append(piece)
                collected += len(piece)
            position += len(text)
        chunk = "".join(pieces)
        next_offset = offset + len(chunk)
        return chunk, total, next_offset >= total

    async def _text_chunks(self, uri: str):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        async for data in self._byte_chunks(uri):
            text = decoder.decode(data)
            if text:
                yield text
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail

    async def _byte_chunks(self, uri: str):
        open_stream = getattr(self._store, "open_stream", None)
        if open_stream is None:
            yield await self._store.load(uri)
            return
        async with open_stream(uri) as stream:
            async for chunk in stream:
                yield chunk


def _ref_record(ref) -> dict[str, Any]:
    return {
        "uri": ref.uri,
        "hash": ref.hash,
        "mime_type": ref.mime_type,
        "size": ref.size,
        "producer": ref.producer,
        "task_id": ref.task_id,
        "metadata": dict(ref.metadata or {}),
        "created_at": ref.created_at.isoformat() if ref.created_at else None,
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _valid_artifact_uri(uri: str) -> bool:
    parsed = parse_artifact_uri(uri)
    return bool(parsed and parsed[0] == "sha256" and re.fullmatch(r"[0-9a-f]{64}", parsed[1]))


def _result(
    request,
    *,
    ok: bool = True,
    output: str = "",
    error: str | None = None,
    metadata: Mapping[str, Any] | None = None,
):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
        metadata=dict(metadata or {}),
    )


__all__ = ["ArtifactCapability", "ExtractionResult", "MimeExtractorRegistry"]
