"""Protocol records for durable evidence-backed research.

These records intentionally complement, rather than replace, Athena's
existing ``Claim`` and ``ArtifactRef`` types:

* ``SourceRecord`` identifies one retrieved source version;
* ``EvidenceObject`` records the exact support extracted from that source;
* ``ResearchGap`` records what is still not established.

The model does not assign truth merely because an object was written.  Source
authority and evidence verification remain explicit fields and operations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def schema_hash(value: Mapping[str, Any]) -> str:
    """Hash a JSON-shaped record for provenance and replay boundaries."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceRecord:
    """One immutable source version, normally backed by an artifact blob."""

    id: str
    canonical_uri: str
    title: str = ""
    source_type: str = "web"
    authority_class: str = "tertiary"
    retrieved_at: str = field(default_factory=_now)
    published_at: str | None = None
    content_hash: str | None = None
    artifact_uri: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def for_uri(
        cls,
        canonical_uri: str,
        *,
        title: str = "",
        source_type: str = "web",
        authority_class: str = "tertiary",
        content_hash: str | None = None,
        artifact_uri: str | None = None,
        published_at: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceRecord:
        # A changed content hash creates a new source version.  Uncaptured
        # sources still deduplicate by canonical URI until a snapshot exists.
        return cls(
            id=_stable_id("src", canonical_uri, content_hash or "uncaptured"),
            canonical_uri=canonical_uri,
            title=title,
            source_type=source_type,
            authority_class=authority_class,
            content_hash=content_hash,
            artifact_uri=artifact_uri,
            published_at=published_at,
            task_id=task_id,
            project_id=project_id,
            metadata=dict(metadata or {}),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "canonical_uri": self.canonical_uri,
            "title": self.title,
            "source_type": self.source_type,
            "authority_class": self.authority_class,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            "content_hash": self.content_hash,
            "artifact_uri": self.artifact_uri,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> SourceRecord:
        return cls(
            id=str(record["id"]),
            canonical_uri=str(record.get("canonical_uri") or ""),
            title=str(record.get("title") or ""),
            source_type=str(record.get("source_type") or "web"),
            authority_class=str(record.get("authority_class") or "tertiary"),
            retrieved_at=str(record.get("retrieved_at") or _now()),
            published_at=record.get("published_at"),
            content_hash=record.get("content_hash"),
            artifact_uri=record.get("artifact_uri"),
            task_id=record.get("task_id"),
            project_id=record.get("project_id"),
            metadata=dict(record.get("metadata") or {}),
        )


@dataclass(frozen=True)
class EvidenceObject:
    """A bounded, inspectable support object extracted from one source."""

    id: str
    source_id: str
    extracted_claim: str
    exact_supporting_excerpt: str
    locator: Mapping[str, Any] = field(default_factory=dict)
    evidence_type: str = "quote"
    authority_class: str = "tertiary"
    extraction_method: str = "manual"
    extraction_model: str | None = None
    confidence: float | None = None
    task_id: str | None = None
    claim_id: str | None = None
    corroborates: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def for_content(
        cls,
        *,
        source_id: str,
        extracted_claim: str,
        exact_supporting_excerpt: str,
        locator: Mapping[str, Any] | None = None,
        evidence_type: str = "quote",
        authority_class: str = "tertiary",
        extraction_method: str = "manual",
        extraction_model: str | None = None,
        confidence: float | None = None,
        task_id: str | None = None,
        claim_id: str | None = None,
        corroborates: tuple[str, ...] = (),
        contradicts: tuple[str, ...] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> EvidenceObject:
        return cls(
            id=_stable_id(
                "evidence", source_id, extracted_claim,
                exact_supporting_excerpt, json.dumps(dict(locator or {}), sort_keys=True),
            ),
            source_id=source_id,
            extracted_claim=extracted_claim,
            exact_supporting_excerpt=exact_supporting_excerpt,
            locator=dict(locator or {}),
            evidence_type=evidence_type,
            authority_class=authority_class,
            extraction_method=extraction_method,
            extraction_model=extraction_model,
            confidence=confidence,
            task_id=task_id,
            claim_id=claim_id,
            corroborates=tuple(corroborates),
            contradicts=tuple(contradicts),
            metadata=dict(metadata or {}),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "extracted_claim": self.extracted_claim,
            "exact_supporting_excerpt": self.exact_supporting_excerpt,
            "locator": dict(self.locator),
            "evidence_type": self.evidence_type,
            "authority_class": self.authority_class,
            "extraction_method": self.extraction_method,
            "extraction_model": self.extraction_model,
            "confidence": self.confidence,
            "task_id": self.task_id,
            "claim_id": self.claim_id,
            "corroborates": list(self.corroborates),
            "contradicts": list(self.contradicts),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> EvidenceObject:
        return cls(
            id=str(record["id"]),
            source_id=str(record.get("source_id") or ""),
            extracted_claim=str(record.get("extracted_claim") or ""),
            exact_supporting_excerpt=str(record.get("exact_supporting_excerpt") or ""),
            locator=dict(record.get("locator") or {}),
            evidence_type=str(record.get("evidence_type") or "quote"),
            authority_class=str(record.get("authority_class") or "tertiary"),
            extraction_method=str(record.get("extraction_method") or "manual"),
            extraction_model=record.get("extraction_model"),
            confidence=record.get("confidence"),
            task_id=record.get("task_id"),
            claim_id=record.get("claim_id"),
            corroborates=tuple(record.get("corroborates") or ()),
            contradicts=tuple(record.get("contradicts") or ()),
            created_at=str(record.get("created_at") or _now()),
            metadata=dict(record.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ResearchGap:
    """An unanswered or insufficiently evidenced research requirement."""

    id: str
    objective: str
    question: str
    kind: str = "unsupported_claim"
    required: bool = True
    status: str = "OPEN"
    task_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    resolved_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        objective: str,
        question: str,
        *,
        kind: str = "unsupported_claim",
        required: bool = True,
        task_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ResearchGap:
        gap_metadata = dict(metadata or {})
        identity = gap_metadata.get("requirement_id") or ""
        return cls(
            id=_stable_id("gap", objective, question, task_id or "", identity),
            objective=objective,
            question=question,
            kind=kind,
            required=required,
            task_id=task_id,
            metadata=gap_metadata,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "question": self.question,
            "kind": self.kind,
            "required": self.required,
            "status": self.status,
            "task_id": self.task_id,
            "evidence_ids": list(self.evidence_ids),
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ResearchGap:
        return cls(
            id=str(record["id"]),
            objective=str(record.get("objective") or ""),
            question=str(record.get("question") or ""),
            kind=str(record.get("kind") or "unsupported_claim"),
            required=bool(record.get("required", True)),
            status=str(record.get("status") or "OPEN"),
            task_id=record.get("task_id"),
            evidence_ids=tuple(record.get("evidence_ids") or ()),
            created_at=str(record.get("created_at") or _now()),
            resolved_at=record.get("resolved_at"),
            metadata=dict(record.get("metadata") or {}),
        )


__all__ = ["EvidenceObject", "ResearchGap", "SourceRecord", "schema_hash"]
