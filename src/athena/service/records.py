"""Typed reconstruction helpers for service-facing durable rows."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from athena.protocol.tasks import TERMINAL_STATUSES, TaskResult, TaskStatus, UsageSummary


def default_model_policy():
    from athena.protocol.tasks import ModelPolicy

    return ModelPolicy(require_tools=False)


def is_terminal_status(status: str | None) -> bool:
    return bool(status) and status in {item.value for item in TERMINAL_STATUSES}


def result_from_row(row: dict[str, Any]) -> TaskResult | None:
    status_raw = row.get("result_status") or row.get("status")
    if not status_raw or status_raw not in {item.value for item in TERMINAL_STATUSES}:
        return None
    usage = row.get("usage") or {}
    cost = usage.get("cost_usd")
    return TaskResult(
        task_id=row["id"],
        status=TaskStatus(status_raw),
        summary=row.get("summary") or "",
        evidence=decode_map_rows(row.get("evidence"), "ContextRef"),
        artifacts=decode_map_rows(row.get("artifacts"), "ArtifactRef"),
        mutations=decode_map_rows(row.get("mutations"), "MutationRef"),
        unresolved=tuple(row.get("unresolved") or []),
        usage=UsageSummary(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model_calls=int(usage.get("model_calls") or 0),
            cost_usd=Decimal(str(cost)) if cost is not None else Decimal(0),
            cost_known=bool(usage.get("cost_known", cost is not None)),
            duration_ms=int(usage.get("duration_ms") or 0),
            executions=int(usage.get("executions") or 0),
            mutations=int(usage.get("mutations") or 0),
        ),
    )


def decode_map_rows(raw: Any, kind: str):
    if not raw or not isinstance(raw, list):
        return ()
    from athena.protocol.artifacts import ArtifactRef
    from athena.protocol.tasks import ContextRef, MutationRef

    if kind == "ContextRef":
        return tuple(
            ContextRef(
                kind=item.get("kind", "session"),
                ref=item.get("ref", ""),
                source_id=item.get("source_id"),
                summary=item.get("summary"),
                mime_type=item.get("mime_type"),
            )
            for item in raw
            if isinstance(item, dict)
        )
    if kind == "ArtifactRef":
        return tuple(
            ArtifactRef(
                id=item.get("id", ""),
                uri=item.get("uri", ""),
                hash=item.get("hash"),
                mime_type=item.get("mime_type"),
                size=item.get("size"),
                producer=item.get("producer"),
                task_id=item.get("task_id"),
                metadata=item.get("metadata") or {},
            )
            for item in raw
            if isinstance(item, dict)
        )
    if kind == "MutationRef":
        return tuple(
            MutationRef(
                id=item.get("id", ""),
                resource=item.get("resource", ""),
                operation=item.get("operation", ""),
                reversible=bool(item.get("reversible", False)),
            )
            for item in raw
            if isinstance(item, dict)
        )
    return ()


__all__ = ["decode_map_rows", "default_model_policy", "is_terminal_status", "result_from_row"]
