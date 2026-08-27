"""Task-scoped inspection of immutable execution artifacts.

Execution deliberately returns only a bounded preview for large output.  This
capability is the OI-style follow-up surface: the model can retrieve a slice or
search the full captured result later without re-running the computation or
placing the entire blob in the conversation.  Artifact ownership is checked
before every read; knowing an artifact URI is not sufficient authority.
"""

from __future__ import annotations

import json
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
            "type": "object",
            "required": ["operation"],
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "read", "slice", "search"],
                },
                "artifact_uri": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 65_536},
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
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
                return _result(request, output=_json({
                    "artifacts": [_ref_record(ref) for ref in refs],
                }))
            uri = str(args.get("artifact_uri") or "")
            if parse_artifact_uri(uri) is None:
                return _result(request, ok=False, error="artifact_uri is invalid")
            if not await self._owned(uri, request.task_id):
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

    async def _owned(self, uri: str, task_id: str) -> bool:
        refs = await self._store.list(task_id=task_id, limit=1000)
        return any(ref.uri == uri for ref in refs)

    async def _read(self, request, args: dict[str, Any], uri: str) -> CapabilityResult:
        offset = int(args.get("offset") or 0)
        limit = min(int(args.get("limit") or self._max_read_bytes), self._max_read_bytes)
        content = (await self._store.load(uri)).decode("utf-8", errors="replace")
        chunk = content[offset:offset + limit]
        next_offset = offset + len(chunk)
        return _result(request, output=_json({
            "artifact_uri": uri,
            "offset": offset,
            "content": chunk,
            "next_offset": next_offset,
            "eof": next_offset >= len(content),
            "size": len(content),
        }))

    async def _search(self, request, args: dict[str, Any], uri: str) -> CapabilityResult:
        query = str(args.get("query") or "")
        if not query:
            return _result(request, ok=False, error="search requires query")
        content = (await self._store.load(uri)).decode("utf-8", errors="replace")
        needle = query.casefold()
        limit = min(int(args.get("max_results") or 20), 100)
        matches: list[dict[str, Any]] = []
        start = 0
        while len(matches) < limit:
            index = content.casefold().find(needle, start)
            if index < 0:
                break
            line_start = content.rfind("\n", 0, index) + 1
            line_end = content.find("\n", index)
            if line_end < 0:
                line_end = len(content)
            matches.append({
                "offset": index,
                "line": content.count("\n", 0, index) + 1,
                "text": content[line_start:line_end][:self._max_read_bytes],
            })
            start = max(index + len(query), index + 1)
        return _result(request, output=_json({
            "artifact_uri": uri, "query": query, "matches": matches,
        }))


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


def _result(request, *, ok: bool = True, output: str = "", error: str | None = None):
    return CapabilityResult(
        request.call_id,
        request.capability_id,
        CapabilityResultStatus.OK if ok else CapabilityResultStatus.FAILED,
        output=output,
        error=error,
    )


__all__ = ["ArtifactCapability"]
