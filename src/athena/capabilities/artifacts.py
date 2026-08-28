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


class ArtifactCapability:
    descriptor = CapabilityDescriptor(
        id="artifacts",
        description=(
            "Inspect task-owned immutable output artifacts without rerunning "
            "the operation. Operations: list/read/slice/search. Reads are "
            "bounded; use offset/limit or search for large results."
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
            ],
        },
        effects=frozenset({EffectClass.READ_LOCAL}),
        origin=CapabilityOrigin.NATIVE,
    )

    def __init__(self, store, *, max_read_bytes: int = 65_536) -> None:
        self._store = store
        self._max_read_bytes = max(1, min(max_read_bytes, 65_536))

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
            return _result(request, ok=False, error=f"unknown operation: {operation}")
        except (OSError, ValueError) as exc:
            return _result(request, ok=False, error=str(exc))
        except Exception as exc:
            return _result(request, ok=False, error=f"artifact operation failed: {exc}")

    async def _owned_ref(self, uri: str, task_id: str):
        refs = await self._store.list(task_id=task_id, limit=1000)
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
        "created_at": ref.created_at.isoformat() if ref.created_at else None,
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _valid_artifact_uri(uri: str) -> bool:
    parsed = parse_artifact_uri(uri)
    return bool(parsed and parsed[0] == "sha256" and re.fullmatch(r"[0-9a-f]{64}", parsed[1]))


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["ArtifactCapability"]
