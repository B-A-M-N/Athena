from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.messages import (
    AudioBlock,
    ArtifactRefBlock,
    CapabilityCallBlock,
    CapabilityResultBlock,
    ContentBlock,
    FileRefBlock,
    ImageBlock,
    Message,
    Provenance,
    ReasoningBlock,
    Role,
    SourceType,
    TextBlock,
    utcnow,
)
from athena.protocol.artifacts import ArtifactRef
from athena.protocol.tasks import (
    CapabilityPolicy,
    Criterion,
    ModelPolicy,
    ResourceBudget,
    TaskSpec,
    TaskStatus,
    WorkspaceSpec,
)
from athena.protocol.events import Event
from athena.state.database import Database


@dataclass(frozen=True)
class SessionSpec:
    id: str
    parent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)


def _serialize_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {
            "type": "text",
            "text": block.text,
            "provenance": _serialize_provenance(block.provenance) if block.provenance else None,
        }
    if isinstance(block, ReasoningBlock):
        return {"type": "reasoning", "text": block.text}
    if isinstance(block, ImageBlock):
        return {"type": "image", "data_path": block.data_path, "mime_type": block.mime_type}
    if isinstance(block, AudioBlock):
        return {"type": "audio", "data_path": block.data_path, "mime_type": block.mime_type}
    if FileRefBlock is not None and isinstance(block, FileRefBlock):
        return {"type": "file_ref", "uri": block.uri, "mime_type": block.mime_type}
    if isinstance(block, ArtifactRefBlock):
        ref = block.ref
        return {
            "type": "artifact_ref",
            "uri": block.uri,
            "ref": {
                "id": ref.id, "uri": ref.uri, "hash": ref.hash,
                "mime_type": ref.mime_type, "size": ref.size,
                "producer": ref.producer, "task_id": ref.task_id,
                "metadata": dict(ref.metadata),
            } if ref is not None else None,
        }
    if isinstance(block, CapabilityCallBlock):
        return {
            "type": "capability_call",
            "call_id": block.call_id,
            "capability_id": block.capability_id,
            "arguments": dict(block.arguments),
        }
    if isinstance(block, CapabilityResultBlock):
        return {
            "type": "capability_result",
            "call_id": block.call_id,
            "capability_id": block.capability_id,
            "ok": block.ok,
            "output": block.output,
            "error": block.error,
            "metadata": dict(block.metadata),
            "ref_uri": block.ref_uri,
        }
    return {"type": getattr(block, "type", "unknown")}


def _deserialize_block(data: dict[str, Any]) -> ContentBlock:
    btype = data.get("type")
    if btype == "text":
        prov = data.get("provenance")
        return TextBlock(
            text=data.get("text", ""),
            provenance=_deserialize_provenance(prov) if prov else None,
        )
    if btype == "reasoning":
        return ReasoningBlock(text=data.get("text", ""))
    if btype == "image":
        return ImageBlock(data_path=data.get("data_path"), mime_type=data.get("mime_type"))
    if btype == "audio":
        return AudioBlock(data_path=data.get("data_path"), mime_type=data.get("mime_type"))
    if btype == "file_ref":
        return FileRefBlock(uri=data.get("uri", ""), mime_type=data.get("mime_type"))
    if btype == "artifact_ref":
        ref_data = data.get("ref")
        ref = None
        if ref_data:
            ref = ArtifactRef(
                id=ref_data.get("id", ""),
                uri=ref_data.get("uri", ""),
                hash=ref_data.get("hash"),
                mime_type=ref_data.get("mime_type"),
                size=ref_data.get("size"),
                producer=ref_data.get("producer"),
                task_id=ref_data.get("task_id"),
                metadata=ref_data.get("metadata") or {},
            )
        return ArtifactRefBlock(uri=data.get("uri", ""), ref=ref)
    if btype == "capability_call":
        return CapabilityCallBlock(
            call_id=data.get("call_id", ""),
            capability_id=data.get("capability_id", ""),
            arguments=data.get("arguments") or {},
        )
    if btype == "capability_result":
        return CapabilityResultBlock(
            call_id=data.get("call_id", ""),
            capability_id=data.get("capability_id", ""),
            ok=data.get("ok", True),
            output=data.get("output", ""),
            error=data.get("error"),
            metadata=data.get("metadata") or {},
            ref_uri=data.get("ref_uri"),
        )
    raise ValueError(f"Unknown block type: {btype!r}")


def _serialize_provenance(prov: Provenance) -> dict[str, Any]:
    return {
        "source_type": prov.source_type.value,
        "source_id": prov.source_id,
        "trust": prov.trust.value,
        "scope": prov.scope,
        "created_at": prov.created_at.isoformat() if prov.created_at else None,
    }


def _deserialize_provenance(data: dict[str, Any]) -> Provenance:
    return Provenance(
        source_type=SourceType(data.get("source_type", "runtime")),
        source_id=data.get("source_id"),
        trust=data.get("trust", "agent_curated"),
        scope=data.get("scope"),
        created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
    )


def _extract_text(blocks: tuple[ContentBlock, ...]) -> str:
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, (TextBlock, ReasoningBlock)):
            if b.text:
                parts.append(b.text)
        elif isinstance(b, CapabilityResultBlock):
            if b.output:
                parts.append(b.output)
        elif isinstance(b, CapabilityCallBlock):
            if b.capability_id:
                parts.append(b.capability_id)
    return "\n".join(parts)


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._current_session_id: str | None = None

    async def create(
        self,
        session_id: str,
        parent_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        meta = dict(metadata or {})
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO sessions(id, parent_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
            (session_id, parent_id, now, now, json.dumps(meta)),
        )
        self._current_session_id = session_id
        return session_id

    async def get(self, session_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        if row.get("metadata"):
            row["metadata"] = json.loads(row["metadata"])
        return row

    async def list_all(self) -> list[dict]:
        """Return every persisted session in creation order."""
        rows = await self._db.fetch_all(
            "SELECT * FROM sessions ORDER BY created_at ASC, rowid ASC"
        )
        for row in rows:
            if row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"])
        return rows

    async def append_message(self, message: Message) -> None:
        session_id: str | None = None
        if message.metadata:
            raw = message.metadata.get("session_id")
            if isinstance(raw, str):
                session_id = raw
        if session_id is None:
            session_id = self._current_session_id
        if session_id is None:
            raise ValueError("append_message requires a session_id in metadata or a current session")
        self._current_session_id = session_id
        blocks_json = json.dumps([_serialize_block(b) for b in message.blocks])
        prov_json = json.dumps(_serialize_provenance(message.provenance))
        meta_json = json.dumps(dict(message.metadata))
        text_content = _extract_text(message.blocks)
        await self._db.execute(
            "INSERT INTO messages(id, session_id, role, blocks, text_content, created_at, provenance, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                session_id,
                message.role.value,
                blocks_json,
                text_content,
                message.created_at.isoformat(),
                prov_json,
                meta_json,
            ),
        )

    async def list_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (session_id, limit),
        )
        result: list[Message] = []
        for row in rows:
            blocks_data = json.loads(row["blocks"]) if row.get("blocks") else []
            blocks = tuple(_deserialize_block(b) for b in blocks_data)
            prov_data = json.loads(row["provenance"]) if row.get("provenance") else None
            prov = _deserialize_provenance(prov_data) if prov_data else Provenance(source_type=SourceType.RUNTIME)
            meta = json.loads(row["metadata"]) if row.get("metadata") else {}
            result.append(
                Message(
                    id=row["id"],
                    role=Role(row["role"]),
                    blocks=blocks,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    provenance=prov,
                    metadata=meta,
                )
            )
        return result


def _serialize_workspace(ws: WorkspaceSpec | None) -> str | None:
    if ws is None:
        return None
    return json.dumps({
        "id": ws.id,
        "root": ws.root,
        "readable": [{"path": r.path, "allow": r.allow} for r in ws.readable],
        "writable": [{"path": r.path, "allow": r.allow} for r in ws.writable],
        "temp_root": ws.temp_root,
        "execution_backend": ws.execution_backend,
        "network_policy": ws.network_policy.value,
    })


def _serialize_criteria(crits: tuple[Criterion, ...]) -> str:
    out = []
    for c in crits:
        v = c.verification
        out.append({
            "id": c.id,
            "description": c.description,
            "required": c.required,
            "verification": {
                "type": v.type.value,
                "command": v.command,
                "path": v.path,
                "predicate": v.predicate,
                "capability": v.capability,
            } if v else None,
        })
    return json.dumps(out)


def _serialize_resource_budget(rb: ResourceBudget) -> str:
    return json.dumps({
        "max_agent_iterations": rb.max_agent_iterations,
        "max_input_tokens": rb.max_input_tokens,
        "max_output_tokens": rb.max_output_tokens,
        "max_cost_usd": str(rb.max_cost_usd) if rb.max_cost_usd is not None else None,
        "max_wall_time": rb.max_wall_time.total_seconds() if rb.max_wall_time is not None else None,
        "max_children": rb.max_children,
        "max_child_depth": rb.max_child_depth,
        "max_parallel_model_calls": rb.max_parallel_model_calls,
        "max_parallel_executions": rb.max_parallel_executions,
        "max_artifact_bytes": rb.max_artifact_bytes,
    })


def _serialize_model_policy(mp: ModelPolicy) -> str:
    return json.dumps({
        "role": mp.role,
        "allowed": list(mp.allowed),
        "require_tools": mp.require_tools,
        "privacy": mp.privacy,
        "max_cost_usd": str(mp.max_cost_usd) if mp.max_cost_usd is not None else None,
    })


def _serialize_capability_policy(cp: CapabilityPolicy) -> str:
    return json.dumps({
        "effects": sorted(cp.effects),
        "allow": list(cp.allow),
        "ask": list(cp.ask),
        "deny": list(cp.deny),
    })


def _serialize_context_refs(refs: tuple) -> str:
    return json.dumps([
        {"kind": r.kind, "ref": r.ref, "source_id": r.source_id, "summary": r.summary}
        for r in refs
    ])


def _serialize_delivery(d) -> str | None:
    if d is None:
        return None
    return json.dumps({"channel": d.channel, "destination": d.destination})


_JSON_FIELDS = (
    "acceptance_criteria",
    "context_refs",
    "workspace",
    "capability_policy",
    "model_policy",
    "resource_budget",
    "delivery",
    "evidence",
    "artifacts",
    "mutations",
    "unresolved",
    "usage",
    "metadata",
)


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, task: TaskSpec) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO tasks("
            "id, session_id, parent_task_id, status, autonomy, objective, "
            "acceptance_criteria, context_refs, workspace, capability_policy, "
            "model_policy, resource_budget, deadline, delivery, "
            "created_at, updated_at, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.session_id,
                task.parent_task_id,
                TaskStatus.CREATED.value,
                "supervised",
                task.objective,
                _serialize_criteria(task.acceptance_criteria),
                _serialize_context_refs(task.context_refs),
                _serialize_workspace(task.workspace),
                _serialize_capability_policy(task.capability_policy),
                _serialize_model_policy(task.model_policy),
                _serialize_resource_budget(task.resource_budget),
                task.deadline.isoformat() if task.deadline else None,
                _serialize_delivery(task.delivery),
                now,
                now,
                json.dumps(dict(task.metadata)),
            ),
        )

    async def transition(self, task_id: str, new_status: TaskStatus) -> None:
        async with self._db.transaction():
            row = await self._db.fetch_one_raw(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            )
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            current = TaskStatus(row["status"])
            allowed = current.legal_transitions()
            if new_status not in allowed:
                raise ValueError(
                    f"Illegal transition {current.value} -> {new_status.value}; "
                    f"allowed: {sorted(s.value for s in allowed)}"
                )
            now = utcnow().isoformat()
            await self._db.execute_raw(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status.value, now, task_id),
            )

    async def get(self, task_id: str) -> dict | None:
        row = await self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        return _decode_task_row(row)

    async def list_for_session(self, session_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        return [_decode_task_row(row) for row in rows]


def _decode_task_row(row: dict) -> dict:
    for key in _JSON_FIELDS:
        val = row.get(key)
        if val:
            try:
                row[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    return row


class EventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, event: Event) -> None:
        await self._db.execute(
            "INSERT INTO events("
            "id, task_id, session_id, type, sequence, timestamp, schema_version, payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.task_id,
                event.session_id,
                event.type,
                event.sequence,
                event.timestamp.isoformat(),
                event.schema_version,
                json.dumps(dict(event.payload)),
            ),
        )

    async def list_after(self, task_id: str, sequence: int) -> list[Event]:
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE task_id = ? AND sequence > ? ORDER BY sequence ASC",
            (task_id, sequence),
        )
        result: list[Event] = []
        for row in rows:
            payload = json.loads(row["payload"]) if row.get("payload") else {}
            result.append(
                Event(
                    id=row["id"],
                    type=row["type"],
                    sequence=row["sequence"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    task_id=row.get("task_id"),
                    session_id=row.get("session_id"),
                    schema_version=row.get("schema_version", 1),
                    payload=payload,
                )
            )
        return result


__all__ = [
    "SessionSpec",
    "SessionRepository",
    "TaskRepository",
    "EventRepository",
]
