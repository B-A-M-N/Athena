"""Durable write-ahead records for task terminal finalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from athena.protocol.artifacts import ArtifactRef
from athena.protocol.messages import utcnow
from athena.protocol.tasks import ContextRef, MutationRef, TaskResult, TaskStatus, UsageSummary


PREPARED = "PREPARED"
QUIESCING = "QUIESCING"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
COMMITTED = "COMMITTED"
OBSERVER_PENDING = "OBSERVER_PENDING"
OBSERVER_RUNNING = "OBSERVER_RUNNING"
OBSERVER_DONE = "OBSERVER_DONE"
OBSERVER_FAILED = "OBSERVER_FAILED"

_RECOVERABLE_PHASES = (PREPARED, QUIESCING, RECOVERY_REQUIRED, COMMITTED)


@dataclass(frozen=True)
class PendingTaskFinalization:
    task_id: str
    intended_status: TaskStatus
    result: TaskResult
    phase: str
    observer_state: dict[str, str]
    created_at: str
    updated_at: str
    last_error: str | None = None


class TaskFinalizationStore:
    """Persist the exact terminal claim until recovery observers finish."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def prepare(self, result: TaskResult) -> PendingTaskFinalization:
        now = utcnow().isoformat()
        encoded = json.dumps(encode_result(result), sort_keys=True, default=str)
        existing = await self.get(result.task_id)
        if existing is not None:
            return existing
        await self._db.execute(
            "INSERT INTO pending_task_finalizations("
            "task_id, intended_status, result_json, phase, observer_state, "
            "created_at, updated_at, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.task_id,
                result.status.value,
                encoded,
                PREPARED,
                "{}",
                now,
                now,
                None,
            ),
        )
        return await self.get(result.task_id)  # type: ignore[return-value]

    async def get(self, task_id: str) -> PendingTaskFinalization | None:
        row = await self._db.fetch_one(
            "SELECT * FROM pending_task_finalizations WHERE task_id = ?", (str(task_id),)
        )
        return _decode(row) if row is not None else None

    async def list_recoverable(self) -> list[PendingTaskFinalization]:
        placeholders = ",".join("?" for _ in _RECOVERABLE_PHASES)
        rows = await self._db.fetch_all(
            "SELECT * FROM pending_task_finalizations "
            f"WHERE phase IN ({placeholders}) ORDER BY created_at, task_id",
            _RECOVERABLE_PHASES,
        )
        return [_decode(row) for row in rows]

    async def list_observer_pending(self) -> list[PendingTaskFinalization]:
        rows = await self._db.fetch_all(
            "SELECT * FROM pending_task_finalizations WHERE phase = ? ORDER BY created_at, task_id",
            (COMMITTED,),
        )
        return [_decode(row) for row in rows]

    async def set_phase(
        self,
        task_id: str,
        phase: str,
        *,
        error: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE pending_task_finalizations SET phase = ?, last_error = ?, "
            "updated_at = ? WHERE task_id = ?",
            (phase, error, utcnow().isoformat(), str(task_id)),
        )

    async def set_observer_state(
        self,
        task_id: str,
        observer: str,
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        pending = await self.get(task_id)
        if pending is None:
            return
        state_map = dict(pending.observer_state)
        state_map[str(observer)] = str(state)
        await self._db.execute(
            "UPDATE pending_task_finalizations SET phase = ?, observer_state = ?, "
            "last_error = ?, updated_at = ? WHERE task_id = ?",
            (
                pending.phase,
                json.dumps(state_map, sort_keys=True),
                error,
                utcnow().isoformat(),
                str(task_id),
            ),
        )

    async def delete(self, task_id: str) -> None:
        await self._db.execute(
            "DELETE FROM pending_task_finalizations WHERE task_id = ?", (str(task_id),)
        )


def encode_result(result: TaskResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "summary": result.summary,
        "evidence": [_encode_context(item) for item in result.evidence],
        "artifacts": [_encode_artifact(item) for item in result.artifacts],
        "mutations": [_encode_mutation(item) for item in result.mutations],
        "unresolved": list(result.unresolved),
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "model_calls": result.usage.model_calls,
            "cost_usd": str(result.usage.cost_usd),
            "cost_known": result.usage.cost_known,
            "duration_ms": result.usage.duration_ms,
            "executions": result.usage.executions,
            "mutations": result.usage.mutations,
        },
        "created_at": result.created_at.isoformat(),
    }


def decode_result(raw: dict[str, Any]) -> TaskResult:
    usage = raw.get("usage") or {}
    return TaskResult(
        task_id=str(raw["task_id"]),
        status=TaskStatus(str(raw["status"])),
        summary=str(raw.get("summary") or ""),
        evidence=tuple(_decode_context(item) for item in raw.get("evidence") or ()),
        artifacts=tuple(_decode_artifact(item) for item in raw.get("artifacts") or ()),
        mutations=tuple(_decode_mutation(item) for item in raw.get("mutations") or ()),
        unresolved=tuple(str(item) for item in raw.get("unresolved") or ()),
        usage=UsageSummary(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            model_calls=int(usage.get("model_calls", 0)),
            cost_usd=Decimal(str(usage.get("cost_usd", "0"))),
            cost_known=bool(usage.get("cost_known", True)),
            duration_ms=int(usage.get("duration_ms", 0)),
            executions=int(usage.get("executions", 0)),
            mutations=int(usage.get("mutations", 0)),
        ),
        created_at=_parse_datetime(raw.get("created_at")) or utcnow(),
    )


def _decode(row: dict[str, Any]) -> PendingTaskFinalization:
    try:
        raw = json.loads(row.get("result_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    try:
        observer_state = json.loads(row.get("observer_state") or "{}")
    except (TypeError, ValueError):
        observer_state = {}
    result = decode_result(raw)
    return PendingTaskFinalization(
        task_id=str(row["task_id"]),
        intended_status=TaskStatus(str(row["intended_status"])),
        result=result,
        phase=str(row["phase"]),
        observer_state={str(key): str(value) for key, value in observer_state.items()},
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_error=row.get("last_error"),
    )


def _encode_context(value: ContextRef) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "ref": value.ref,
        "source_id": value.source_id,
        "summary": value.summary,
        "mime_type": value.mime_type,
    }


def _decode_context(value: dict[str, Any]) -> ContextRef:
    return ContextRef(
        kind=str(value.get("kind") or "session"),
        ref=str(value.get("ref") or ""),
        source_id=value.get("source_id"),
        summary=value.get("summary"),
        mime_type=value.get("mime_type"),
    )


def _encode_artifact(value: ArtifactRef) -> dict[str, Any]:
    return {
        "id": value.id,
        "uri": value.uri,
        "hash": value.hash,
        "mime_type": value.mime_type,
        "size": value.size,
        "storage_path": value.storage_path,
        "created_at": value.created_at.isoformat() if value.created_at else None,
        "producer": value.producer,
        "task_id": value.task_id,
        "metadata": dict(value.metadata or {}),
    }


def _decode_artifact(value: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        id=str(value.get("id") or ""),
        uri=str(value.get("uri") or ""),
        hash=value.get("hash"),
        mime_type=value.get("mime_type"),
        size=value.get("size"),
        storage_path=value.get("storage_path"),
        created_at=_parse_datetime(value.get("created_at"), allow_none=True),
        producer=value.get("producer"),
        task_id=value.get("task_id"),
        metadata=value.get("metadata") or {},
    )


def _encode_mutation(value: MutationRef) -> dict[str, Any]:
    return {
        "id": value.id,
        "resource": value.resource,
        "operation": value.operation,
        "reversible": value.reversible,
    }


def _decode_mutation(value: dict[str, Any]) -> MutationRef:
    return MutationRef(
        id=str(value.get("id") or ""),
        resource=str(value.get("resource") or ""),
        operation=str(value.get("operation") or ""),
        reversible=bool(value.get("reversible", False)),
    )


def _parse_datetime(value: Any, *, allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if value is None:
        return utcnow()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


__all__ = [
    "COMMITTED",
    "OBSERVER_DONE",
    "OBSERVER_PENDING",
    "OBSERVER_RUNNING",
    "PREPARED",
    "QUIESCING",
    "RECOVERY_REQUIRED",
    "PendingTaskFinalization",
    "TaskFinalizationStore",
    "decode_result",
    "encode_result",
]
