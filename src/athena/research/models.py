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
from typing import Any, Sequence
from urllib.parse import urlsplit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def schema_hash(value: Mapping[str, Any]) -> str:
    """Hash a JSON-shaped record for provenance and replay boundaries."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_family_metadata(
    canonical_uri: str, metadata: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Derive bounded source-family identity without claiming independence."""
    values = dict(metadata or {})
    domain = (urlsplit(canonical_uri).hostname or "").casefold()
    publisher = str(values.get("publisher") or domain)
    family = str(values.get("source_family") or publisher or domain or "unknown")
    return {
        "publisher": publisher,
        "canonical_domain": domain,
        "source_family": family,
        "upstream_source": str(values.get("upstream_source") or ""),
        "primary_secondary": str(values.get("primary_secondary") or "secondary"),
        "independence_group": str(values.get("independence_group") or family),
    }


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
    revision: str | None = None
    acquisition_method: str = "operator_or_fetch"
    acquisition_receipt: Mapping[str, Any] = field(default_factory=dict)
    confidence_calibration: Mapping[str, Any] = field(default_factory=dict)
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
        revision: str | None = None,
        acquisition_method: str = "operator_or_fetch",
        acquisition_receipt: Mapping[str, Any] | None = None,
        confidence_calibration: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceRecord:
        # A changed content hash creates a new source version.  Uncaptured
        # sources still deduplicate by canonical URI until a snapshot exists.
        merged_metadata = source_family_metadata(canonical_uri, metadata)
        merged_metadata.update(dict(metadata or {}))
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
            revision=revision,
            acquisition_method=acquisition_method,
            acquisition_receipt=dict(acquisition_receipt or {}),
            confidence_calibration=dict(confidence_calibration or {}),
            metadata=merged_metadata,
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
            "revision": self.revision,
            "acquisition_method": self.acquisition_method,
            "acquisition_receipt": dict(self.acquisition_receipt),
            "confidence_calibration": dict(self.confidence_calibration),
            "metadata": {
                **dict(self.metadata),
                "_athena:revision": self.revision,
                "_athena:acquisition_method": self.acquisition_method,
                "_athena:acquisition_receipt": dict(self.acquisition_receipt),
                "_athena:confidence_calibration": dict(self.confidence_calibration),
            },
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
            revision=record.get("revision")
            or (record.get("metadata") or {}).get("_athena:revision"),
            acquisition_method=str(
                record.get("acquisition_method")
                or (record.get("metadata") or {}).get("_athena:acquisition_method")
                or "operator_or_fetch"
            ),
            acquisition_receipt=dict(
                record.get("acquisition_receipt")
                or (record.get("metadata") or {}).get("_athena:acquisition_receipt")
                or {}
            ),
            confidence_calibration=dict(
                record.get("confidence_calibration")
                or (record.get("metadata") or {}).get("_athena:confidence_calibration")
                or {}
            ),
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
    source_revision: str | None = None
    source_content_hash: str | None = None
    acquired_at: str = field(default_factory=_now)
    confidence_calibration: Mapping[str, Any] = field(default_factory=dict)
    contradiction_status: str = "unreviewed"
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
        source_revision: str | None = None,
        source_content_hash: str | None = None,
        acquired_at: str | None = None,
        confidence_calibration: Mapping[str, Any] | None = None,
        contradiction_status: str = "unreviewed",
        metadata: Mapping[str, Any] | None = None,
    ) -> EvidenceObject:
        return cls(
            id=_stable_id(
                "evidence",
                source_id,
                extracted_claim,
                exact_supporting_excerpt,
                json.dumps(dict(locator or {}), sort_keys=True),
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
            source_revision=source_revision,
            source_content_hash=source_content_hash,
            acquired_at=acquired_at or _now(),
            confidence_calibration=dict(confidence_calibration or {}),
            contradiction_status=contradiction_status,
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
            "source_revision": self.source_revision,
            "source_content_hash": self.source_content_hash,
            "acquired_at": self.acquired_at,
            "confidence_calibration": dict(self.confidence_calibration),
            "contradiction_status": self.contradiction_status,
            "metadata": {
                **dict(self.metadata),
                "_athena:source_revision": self.source_revision,
                "_athena:source_content_hash": self.source_content_hash,
                "_athena:acquired_at": self.acquired_at,
                "_athena:confidence_calibration": dict(self.confidence_calibration),
                "_athena:contradiction_status": self.contradiction_status,
            },
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
            source_revision=record.get("source_revision")
            or (record.get("metadata") or {}).get("_athena:source_revision"),
            source_content_hash=record.get("source_content_hash")
            or (record.get("metadata") or {}).get("_athena:source_content_hash"),
            acquired_at=str(
                record.get("acquired_at")
                or (record.get("metadata") or {}).get("_athena:acquired_at")
                or _now()
            ),
            confidence_calibration=dict(
                record.get("confidence_calibration")
                or (record.get("metadata") or {}).get("_athena:confidence_calibration")
                or {}
            ),
            contradiction_status=str(
                record.get("contradiction_status")
                or (record.get("metadata") or {}).get("_athena:contradiction_status")
                or "unreviewed"
            ),
            metadata=dict(record.get("metadata") or {}),
        )


def classify_evidence_quality(
    evidence: EvidenceObject | None,
    source: SourceRecord | None = None,
) -> str:
    """Classify evidence for completion without treating metadata as truth."""
    if evidence is None:
        return "no_evidence"
    if evidence.contradiction_status.casefold() in {"contradicted", "conflicted"}:
        return "contradicted"
    if source is None or not source.artifact_uri:
        return "no_evidence"
    if (
        evidence.source_revision and source.revision and evidence.source_revision != source.revision
    ) or (
        evidence.source_content_hash
        and source.content_hash
        and evidence.source_content_hash != source.content_hash
    ):
        return "stale"
    if evidence.confidence is not None and float(evidence.confidence) < 0.5:
        return "weak"
    if str(evidence.confidence_calibration.get("status") or "").casefold() in {
        "weak",
        "uncalibrated",
    }:
        return "weak"
    return "supported"


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


@dataclass(frozen=True)
class EvidenceBundle:
    """Bounded, deterministic packet presented to synthesis or a reviewer."""

    id: str
    task_id: str
    ready: bool
    sources: tuple[Mapping[str, Any], ...] = ()
    evidence: tuple[Mapping[str, Any], ...] = ()
    gaps: tuple[Mapping[str, Any], ...] = ()
    required_open_gaps: tuple[str, ...] = ()
    unverified_closed_gaps: tuple[str, ...] = ()
    independence_groups: tuple[str, ...] = ()
    contradiction_evidence_ids: tuple[str, ...] = ()
    evidence_quality_counts: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        task_id: str,
        *,
        ready: bool,
        sources: Sequence[Mapping[str, Any]] = (),
        evidence: Sequence[Mapping[str, Any]] = (),
        gaps: Sequence[Mapping[str, Any]] = (),
        required_open_gaps: Sequence[str] = (),
        unverified_closed_gaps: Sequence[str] = (),
        independence_groups: Sequence[str] = (),
        contradiction_evidence_ids: Sequence[str] = (),
        evidence_quality_counts: Mapping[str, int] | None = None,
    ) -> "EvidenceBundle":
        normalized_sources = tuple(dict(item) for item in sources)
        normalized_evidence = tuple(dict(item) for item in evidence)
        normalized_gaps = tuple(dict(item) for item in gaps)
        identity = (
            task_id,
            [item.get("id") for item in normalized_sources],
            [item.get("id") for item in normalized_evidence],
            [item.get("id") for item in normalized_gaps],
            list(required_open_gaps),
            list(unverified_closed_gaps),
            dict(evidence_quality_counts or {}),
        )
        return cls(
            id=_stable_id("bundle", json.dumps(identity, sort_keys=True, default=str)),
            task_id=task_id,
            ready=bool(ready),
            sources=normalized_sources,
            evidence=normalized_evidence,
            gaps=normalized_gaps,
            required_open_gaps=tuple(str(item) for item in required_open_gaps),
            unverified_closed_gaps=tuple(str(item) for item in unverified_closed_gaps),
            independence_groups=tuple(str(item) for item in independence_groups),
            contradiction_evidence_ids=tuple(str(item) for item in contradiction_evidence_ids),
            evidence_quality_counts={
                str(key): int(value) for key, value in (evidence_quality_counts or {}).items()
            },
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "bundle_id": self.id,
            "task_id": self.task_id,
            "ready": self.ready,
            "required_open_gaps": list(self.required_open_gaps),
            "unverified_closed_gaps": list(self.unverified_closed_gaps),
            "sources": [dict(item) for item in self.sources],
            "evidence": [dict(item) for item in self.evidence],
            "gaps": [dict(item) for item in self.gaps],
            "independence_groups": list(self.independence_groups),
            "contradiction_evidence_ids": list(self.contradiction_evidence_ids),
            "evidence_quality_counts": dict(self.evidence_quality_counts),
        }


__all__ = [
    "EvidenceBundle",
    "EvidenceObject",
    "ResearchGap",
    "SourceRecord",
    "classify_evidence_quality",
    "schema_hash",
]
