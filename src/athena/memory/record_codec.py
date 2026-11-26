"""Canonical memory-row serialization beneath :mod:`athena.memory.store`."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from athena.protocol.memory import MemoryKind, MemoryRecord, MemoryScope, RetrievalMode
from athena.protocol.messages import Provenance, SourceType, TrustClass, utcnow

_NSP = "_athena"
TRUST_KEY = f"{_NSP}:trust"
SCOPE_ID_KEY = f"{_NSP}:scope_id"
SUMMARY_KEY = f"{_NSP}:summary"
PROVENANCE_KEY = f"{_NSP}:provenance"
KIND_KEY = f"{_NSP}:kind"
UPDATED_AT_KEY = f"{_NSP}:updated_at"
RETRIEVAL_MODE_KEY = f"{_NSP}:retrieval_mode"
SUBJECT_KEY = f"{_NSP}:subject"
TAGS_KEY = f"{_NSP}:tags"
SOURCE_REFS_KEY = f"{_NSP}:source_refs"
CONFIDENCE_KEY = f"{_NSP}:confidence"
VALID_FROM_KEY = f"{_NSP}:valid_from"
VALID_UNTIL_KEY = f"{_NSP}:valid_until"
SUPERSEDES_KEY = f"{_NSP}:supersedes"
CONTRADICTED_BY_KEY = f"{_NSP}:contradicted_by"
RETRIEVAL_SCORE_KEY = f"{_NSP}:retrieval_score"

_NS_KEYS = frozenset(
    {
        TRUST_KEY,
        SCOPE_ID_KEY,
        SUMMARY_KEY,
        PROVENANCE_KEY,
        KIND_KEY,
        UPDATED_AT_KEY,
        RETRIEVAL_MODE_KEY,
        SUBJECT_KEY,
        TAGS_KEY,
        SOURCE_REFS_KEY,
        CONFIDENCE_KEY,
        VALID_FROM_KEY,
        VALID_UNTIL_KEY,
        SUPERSEDES_KEY,
        CONTRADICTED_BY_KEY,
        RETRIEVAL_SCORE_KEY,
    }
)


def _prov_to_dict(provenance: Provenance) -> dict[str, Any]:
    return {
        "source_type": provenance.source_type.value,
        "source_id": provenance.source_id,
        "trust": provenance.trust.value,
        "scope": provenance.scope,
        "created_at": provenance.created_at.isoformat() if provenance.created_at else None,
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _prov_from_dict(data: Mapping[str, Any]) -> Provenance:
    return Provenance(
        source_type=SourceType(data.get("source_type", SourceType.RUNTIME.value)),
        source_id=data.get("source_id"),
        trust=TrustClass(data.get("trust", TrustClass.AGENT_CURATED.value)),
        scope=data.get("scope"),
        created_at=_parse_dt(data.get("created_at")),
    )


def _tuple_of(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item)
    return ()


def scope_id(record: MemoryRecord) -> str | None:
    if "scope_id" in record.metadata:
        return str(record.metadata["scope_id"])
    if record.scope in (
        MemoryScope.TASK,
        MemoryScope.SESSION,
        MemoryScope.JOB,
        MemoryScope.USER,
    ):
        return record.source.source_id if record.source else None
    return None


def row_to_record(row: Mapping[str, Any]) -> MemoryRecord:
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except Exception:
        metadata = {}
    trust_value = metadata.get(TRUST_KEY, TrustClass.AGENT_CURATED.value)
    trust = (
        TrustClass(trust_value)
        if trust_value and trust_value in TrustClass._value2member_map_
        else TrustClass.AGENT_CURATED
    )
    provenance_raw = metadata.get(PROVENANCE_KEY)
    if isinstance(provenance_raw, dict):
        provenance = _prov_from_dict(provenance_raw)
    else:
        provenance = Provenance(
            source_type=SourceType.RUNTIME,
            source_id=row.get("source_task_id"),
            trust=trust,
        )
    kind_raw = metadata.get(KIND_KEY, row.get("kind"))
    kind = (
        MemoryKind(kind_raw)
        if kind_raw and kind_raw in MemoryKind._value2member_map_
        else MemoryKind.WORKING
    )
    created = _parse_dt(row.get("created_at")) or utcnow()
    updated = _parse_dt(metadata.get(UPDATED_AT_KEY)) or _parse_dt(row.get("updated_at"))
    retrieval_raw = metadata.get(RETRIEVAL_MODE_KEY)
    retrieval_mode = (
        RetrievalMode(retrieval_raw)
        if retrieval_raw and retrieval_raw in RetrievalMode._value2member_map_
        else None
    )
    valid_from = _parse_dt(metadata.get(VALID_FROM_KEY))
    valid_until = _parse_dt(metadata.get(VALID_UNTIL_KEY))
    confidence = metadata.get(CONFIDENCE_KEY)
    return MemoryRecord(
        id=row["id"],
        kind=kind,
        scope=_scope_from_row(row),
        content=row.get("content") or "",
        summary=metadata.get(SUMMARY_KEY),
        source=provenance,
        trust=trust,
        created_at=created,
        updated_at=updated,
        metadata={key: value for key, value in metadata.items() if key not in _NS_KEYS},
        retrieval_mode=retrieval_mode,
        subject=metadata.get(SUBJECT_KEY),
        tags=_tuple_of(metadata.get(TAGS_KEY)),
        source_refs=_tuple_of(metadata.get(SOURCE_REFS_KEY)),
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        valid_from=valid_from,
        valid_until=valid_until,
        supersedes=_tuple_of(metadata.get(SUPERSEDES_KEY)),
        contradicted_by=_tuple_of(metadata.get(CONTRADICTED_BY_KEY)),
    )


def _scope_from_row(row: Mapping[str, Any]) -> MemoryScope:
    raw = row.get("scope")
    if raw and raw in MemoryScope._value2member_map_:
        return MemoryScope(raw)
    return MemoryScope.PROJECT


def record_metadata(
    record: MemoryRecord,
    provenance: Provenance,
    now: str,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Encode the durable namespaced metadata for one memory record."""
    trust = record.trust if record.source is None else provenance.trust
    metadata = dict(record.metadata)
    metadata[TRUST_KEY] = trust.value
    metadata[KIND_KEY] = record.kind.value
    metadata[SCOPE_ID_KEY] = scope_id(record)
    if record.summary:
        metadata[SUMMARY_KEY] = record.summary
    metadata[PROVENANCE_KEY] = _prov_to_dict(provenance)
    metadata[UPDATED_AT_KEY] = now
    if record.retrieval_mode is not None:
        metadata[RETRIEVAL_MODE_KEY] = record.retrieval_mode.value
    if record.subject:
        metadata[SUBJECT_KEY] = record.subject
    if record.tags:
        metadata[TAGS_KEY] = list(record.tags)
    if record.source_refs:
        metadata[SOURCE_REFS_KEY] = list(record.source_refs)
    if record.confidence is not None:
        metadata[CONFIDENCE_KEY] = record.confidence
    if record.valid_from is not None:
        metadata[VALID_FROM_KEY] = record.valid_from.isoformat()
    if record.valid_until is not None:
        metadata[VALID_UNTIL_KEY] = record.valid_until.isoformat()
    if record.supersedes:
        metadata[SUPERSEDES_KEY] = list(record.supersedes)
    if record.contradicted_by:
        metadata[CONTRADICTED_BY_KEY] = list(record.contradicted_by)
    if extra:
        metadata.update(extra)
    return metadata


__all__ = [
    "CONTRADICTED_BY_KEY",
    "MemoryRecord",
    "record_metadata",
    "row_to_record",
    "scope_id",
]
