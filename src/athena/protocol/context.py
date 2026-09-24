"""Neutral contracts for explicitly attached context blocks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow


@dataclass(frozen=True)
class ContextBlock:
    """One version of an explicitly attached context block."""

    id: str
    label: str
    content: str
    scope: str
    scope_id: str
    trust: TrustClass = TrustClass.AGENT_CURATED
    max_tokens: int = 2_500
    attached: bool = True
    version: int = 1
    provenance: Provenance | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime | None = None

    @property
    def effective_provenance(self) -> Provenance:
        if self.provenance is not None:
            return self.provenance
        return Provenance(
            source_type=SourceType.TASK if self.scope == "task" else SourceType.MEMORY,
            source_id=self.id,
            trust=self.trust,
            scope=self.scope,
            created_at=self.created_at,
        )

    def bounded_content(self) -> str:
        return self.content[: max(1, self.max_tokens) * 4]

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "content": self.content,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "trust": self.trust.value,
            "max_tokens": self.max_tokens,
            "attached": self.attached,
            "version": self.version,
            "provenance": _provenance_record(self.effective_provenance),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


_DIGEST_FIELD_NAMES = (
    "objective",
    "constraints",
    "decisions",
    "completed_work",
    "pending_work",
    "files_resources",
    "runtime_state",
    "child_tasks",
    "evidence",
    "artifacts",
    "failures",
    "open_questions",
    "acceptance_state",
)
_MAX_DIGEST_FIELD_ITEMS = 8
_MAX_DIGEST_FIELD_TEXT = 600


@dataclass(frozen=True)
class StrategySelectionRecord:
    """Evidence for the kernel's advisory strategy choice.

    The record compares speculative and sequential routes against the same
    bounded task evidence. It is a measurement record, not authority to run
    either route or promote a candidate.
    """

    selected_route: str
    selected_by: str
    uncertainty: str
    viable_alternatives: tuple[str, ...]
    estimated_validation_cost: float
    available_budget: Mapping[str, Any]
    rollback_feasible: bool
    critical_path_effect: str
    workspace_baseline: Mapping[str, Any]
    acceptance_criteria: tuple[str, ...]
    evidence: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "selected_route": self.selected_route,
            "selected_by": self.selected_by,
            "uncertainty": self.uncertainty,
            "viable_alternatives": list(self.viable_alternatives),
            "estimated_validation_cost": round(float(self.estimated_validation_cost), 6),
            "available_budget": dict(self.available_budget),
            "rollback_feasible": self.rollback_feasible,
            "critical_path_effect": self.critical_path_effect,
            "workspace_baseline": dict(self.workspace_baseline),
            "acceptance_criteria": list(self.acceptance_criteria),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ContextDigest:
    """Bounded, durable context state shared by compilers and repositories."""

    task_id: str
    session_id: str | None
    principal_id: str
    level: int
    parent_digest_id: str | None = None
    source_digest_ids: tuple[str, ...] = ()
    range_start_message_id: str | None = None
    range_end_message_id: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    transcript_anchors: tuple[str, ...] = ()
    recovery_queries: tuple[str, ...] = ()
    id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def normalized_fields(self) -> dict[str, Any]:
        result = {
            name: _bound_digest_field(self.fields.get(name, []), name=name)
            for name in _DIGEST_FIELD_NAMES
        }
        result["objective"] = str(result["objective"] or "")
        return result

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "principal_id": self.principal_id,
            "level": self.level,
            "parent_digest_id": self.parent_digest_id,
            "source_digest_ids": list(self.source_digest_ids),
            "range_start_message_id": self.range_start_message_id,
            "range_end_message_id": self.range_end_message_id,
            "fields": self.normalized_fields(),
            "transcript_anchors": list(self.transcript_anchors),
            "recovery_queries": list(self.recovery_queries),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _digest_json_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    return tuple(str(item) for item in value if item)


def row_to_digest(row: Mapping[str, Any]) -> ContextDigest:
    try:
        fields = json.loads(row.get("fields") or "{}")
    except (TypeError, ValueError):
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    return ContextDigest(
        id=str(row.get("id") or ""),
        task_id=str(row.get("task_id") or ""),
        session_id=str(row.get("session_id")) if row.get("session_id") else None,
        principal_id=str(row.get("principal_id") or ""),
        level=int(row.get("level") or 0),
        parent_digest_id=(
            str(row.get("parent_digest_id")) if row.get("parent_digest_id") else None
        ),
        source_digest_ids=_digest_json_list(row.get("source_digest_ids")),
        range_start_message_id=(
            str(row.get("range_start_message_id")) if row.get("range_start_message_id") else None
        ),
        range_end_message_id=(
            str(row.get("range_end_message_id")) if row.get("range_end_message_id") else None
        ),
        fields=fields,
        transcript_anchors=_digest_json_list(row.get("transcript_anchors")),
        recovery_queries=_digest_json_list(row.get("recovery_queries")),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _bound_digest_field(value: Any, *, name: str) -> Any:
    if name == "objective":
        return str(value or "")[:1200]
    if isinstance(value, Mapping):
        return {
            str(key): _bound_digest_field(item, name="item")
            for key, item in list(value.items())[:_MAX_DIGEST_FIELD_ITEMS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bound_digest_field(item, name="item") for item in list(value)[:_MAX_DIGEST_FIELD_ITEMS]
        ]
    return str(value)[:_MAX_DIGEST_FIELD_TEXT] if value is not None else ""


class ContextDigestStore(Protocol):
    """Durable digest port injected into context compilation."""

    async def save(self, digest: Any) -> Any:
        """Persist one compiled-context digest."""

    async def latest_for_task(self, task_id: str, principal_id: str) -> Any:
        """Return the latest task-scoped digest, when present."""

    async def latest_for_session(self, session_id: str, principal_id: str) -> Any:
        """Return the latest session-scoped digest, when present."""


def _provenance_record(value: Provenance | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "source_type": value.source_type.value,
        "source_id": value.source_id,
        "trust": value.trust.value,
        "scope": value.scope,
        "created_at": value.created_at.isoformat() if value.created_at else None,
    }


def provenance_from_mapping(data: Mapping[str, Any]) -> Provenance:
    source_type = SourceType(data.get("source_type", "runtime"))
    raw_trust = data.get("trust", TrustClass.AGENT_CURATED.value)
    try:
        trust = TrustClass(raw_trust)
    except ValueError:
        trust = TrustClass.AGENT_CURATED
    created = data.get("created_at")
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    return Provenance(
        source_type=source_type,
        source_id=data.get("source_id"),
        trust=trust,
        scope=data.get("scope"),
        created_at=created,
    )


__all__ = [
    "ContextBlock",
    "ContextDigest",
    "StrategySelectionRecord",
    "ContextDigestStore",
    "provenance_from_mapping",
    "row_to_digest",
]
