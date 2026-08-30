"""Durable SQLite storage for sources, searchable snapshots, and evidence."""

from __future__ import annotations

import asyncio
import json
from datetime import timezone
from typing import Any

from athena.research.models import EvidenceObject, ResearchGap, SourceRecord
from athena.state.database import Database


class ResearchStore:
    """Persistence boundary for Athena's Evidence/Research Fabric.

    The store is deliberately not a research agent.  It records immutable-ish
    source versions and evidence relations; planning, retrieval, critique, and
    synthesis remain ordinary AgentKernel workflows/capabilities.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic revision for compiled-context cache invalidation."""
        return self._generation

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS research_sources ("
                "id TEXT PRIMARY KEY, canonical_uri TEXT NOT NULL, title TEXT NOT NULL, "
                "source_type TEXT NOT NULL, authority_class TEXT NOT NULL, "
                "retrieved_at TEXT NOT NULL, published_at TEXT, content_hash TEXT, "
                "artifact_uri TEXT, task_id TEXT, project_id TEXT, metadata TEXT NOT NULL)"
            )
            await self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_research_source_version "
                "ON research_sources(canonical_uri, content_hash)"
            )
            # Keep a bounded searchable text projection beside the immutable
            # ArtifactStore blob.  The blob remains the authority; this table
            # is only an index/projection so research.search can retrieve
            # content that is not present in a source title or URI without
            # requiring an optional vector database.
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS research_source_content ("
                "source_id TEXT PRIMARY KEY, content TEXT NOT NULL, "
                "content_hash TEXT NOT NULL, mime_type TEXT NOT NULL DEFAULT '', "
                "FOREIGN KEY(source_id) REFERENCES research_sources(id) ON DELETE CASCADE)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_source_content_hash "
                "ON research_source_content(content_hash)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS research_evidence ("
                "id TEXT PRIMARY KEY, source_id TEXT NOT NULL, claim_id TEXT, "
                "extracted_claim TEXT NOT NULL, exact_supporting_excerpt TEXT NOT NULL, "
                "locator TEXT NOT NULL, evidence_type TEXT NOT NULL, "
                "authority_class TEXT NOT NULL, extraction_method TEXT NOT NULL, "
                "extraction_model TEXT, confidence REAL, task_id TEXT, "
                "created_at TEXT NOT NULL, metadata TEXT NOT NULL, "
                "FOREIGN KEY(source_id) REFERENCES research_sources(id))"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_evidence_source "
                "ON research_evidence(source_id)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_evidence_claim "
                "ON research_evidence(claim_id)"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS research_evidence_links ("
                "evidence_id TEXT NOT NULL, related_evidence_id TEXT NOT NULL, "
                "relation TEXT NOT NULL, PRIMARY KEY(evidence_id, related_evidence_id, relation), "
                "FOREIGN KEY(evidence_id) REFERENCES research_evidence(id), "
                "FOREIGN KEY(related_evidence_id) REFERENCES research_evidence(id))"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS research_claim_evidence ("
                "claim_id TEXT NOT NULL, evidence_id TEXT NOT NULL, task_id TEXT, "
                "created_at TEXT NOT NULL, PRIMARY KEY(claim_id, evidence_id), "
                "FOREIGN KEY(evidence_id) REFERENCES research_evidence(id))"
            )
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS research_gaps ("
                "id TEXT PRIMARY KEY, objective TEXT NOT NULL, question TEXT NOT NULL, "
                "kind TEXT NOT NULL, required INTEGER NOT NULL, status TEXT NOT NULL, "
                "task_id TEXT, evidence_ids TEXT NOT NULL, created_at TEXT NOT NULL, "
                "resolved_at TEXT, metadata TEXT NOT NULL)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_gaps_task_status "
                "ON research_gaps(task_id, status)"
            )
        self._ready = True

    async def index_content(
        self,
        source_id: str,
        content: str,
        *,
        content_hash: str,
        mime_type: str = "",
    ) -> None:
        """Index a textual source projection for durable local retrieval.

        ``content_hash`` must be the hash of the captured ArtifactStore bytes.
        The index is therefore never treated as evidence by itself: callers
        still verify excerpts against the immutable artifact before making a
        claim.  Re-indexing the same source is idempotent and replaces only
        the derived projection.
        """
        await self._ensure()
        if await self.get_source(source_id) is None:
            raise KeyError(f"unknown source: {source_id}")
        await self._db.execute(
            "INSERT INTO research_source_content(source_id, content, content_hash, mime_type) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET content=excluded.content, "
            "content_hash=excluded.content_hash, mime_type=excluded.mime_type",
            (source_id, str(content), content_hash, str(mime_type or "")),
        )
        self._generation += 1

    async def search_content(
        self,
        query: str,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
        snippet_chars: int = 240,
    ) -> list[dict[str, Any]]:
        """Search indexed snapshots and return bounded evidence candidates.

        This is intentionally deterministic lexical retrieval.  It gives the
        kernel a useful local corpus primitive while leaving semantic
        retrieval/reranking as an explicit future implementation.  Every
        returned hit includes the source version and a bounded context window;
        the full source remains in ArtifactStore and must be verified before
        citation.
        """
        await self._ensure()
        terms = [term.casefold() for term in _search_terms(query)]
        if not terms:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None and project_id is not None:
            clauses.append("(s.task_id = ? OR (s.task_id IS NULL AND s.project_id = ?))")
            params.extend((task_id, project_id))
        elif task_id is not None:
            clauses.append("s.task_id = ?")
            params.append(task_id)
        elif project_id is not None:
            clauses.append("s.project_id = ?")
            params.append(project_id)
        for term in terms:
            clauses.append("LOWER(c.content) LIKE ?")
            params.append(f"%{term}%")
        rows = await self._db.fetch_all(
            "SELECT s.*, c.content, c.content_hash AS indexed_content_hash, "
            "c.mime_type FROM research_source_content c "
            "JOIN research_sources s ON s.id = c.source_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY s.retrieved_at DESC, s.id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        )
        bounded = max(80, min(int(snippet_chars), 2000))
        return [
            {
                "source": SourceRecord.from_record(_decode_row(row)).to_record(),
                "snippet": _snippet(str(row.get("content") or ""), terms, bounded),
                "indexed_content_hash": row.get("indexed_content_hash"),
                "mime_type": row.get("mime_type") or "",
            }
            for row in rows
        ]

    async def save_source(self, source: SourceRecord) -> SourceRecord:
        await self._ensure()
        await self._db.execute(
            "INSERT INTO research_sources("
            "id, canonical_uri, title, source_type, authority_class, retrieved_at, "
            "published_at, content_hash, artifact_uri, task_id, project_id, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
            "source_type=excluded.source_type, authority_class=excluded.authority_class, "
            "published_at=excluded.published_at, artifact_uri=excluded.artifact_uri, "
            "task_id=excluded.task_id, project_id=excluded.project_id, "
            "metadata=excluded.metadata",
            (
                source.id,
                source.canonical_uri,
                source.title,
                source.source_type,
                source.authority_class,
                source.retrieved_at,
                source.published_at,
                source.content_hash,
                source.artifact_uri,
                source.task_id,
                source.project_id,
                json.dumps(dict(source.metadata), sort_keys=True),
            ),
        )
        self._generation += 1
        return source

    async def get_source(self, source_id: str) -> SourceRecord | None:
        await self._ensure()
        row = await self._db.fetch_one("SELECT * FROM research_sources WHERE id = ?", (source_id,))
        return SourceRecord.from_record(_decode_row(row)) if row else None

    async def latest_source_for_uri(self, canonical_uri: str) -> SourceRecord | None:
        """Return the newest captured version of one canonical source."""
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT * FROM research_sources WHERE canonical_uri = ? "
            "ORDER BY retrieved_at DESC, id DESC LIMIT 1",
            (canonical_uri,),
        )
        return SourceRecord.from_record(_decode_row(row)) if row else None

    async def list_sources(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[SourceRecord]:
        await self._ensure()
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None and project_id is not None:
            # A task can see its own captures and captures promoted to its
            # project overlay. It must not see another task's private source.
            clauses.append("(task_id = ? OR (task_id IS NULL AND project_id = ?))")
            params.extend((task_id, project_id))
        elif task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        elif project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if query:
            clauses.append("(canonical_uri LIKE ? OR title LIKE ?)")
            pattern = f"%{query}%"
            params.extend((pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._db.fetch_all(
            "SELECT * FROM research_sources"
            + where
            + " ORDER BY retrieved_at DESC, id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        )
        return [SourceRecord.from_record(_decode_row(row)) for row in rows]

    async def save_evidence(self, evidence: EvidenceObject) -> EvidenceObject:
        await self._ensure()
        if await self.get_source(evidence.source_id) is None:
            raise KeyError(f"unknown source: {evidence.source_id}")
        await self._db.execute(
            "INSERT INTO research_evidence("
            "id, source_id, claim_id, extracted_claim, exact_supporting_excerpt, "
            "locator, evidence_type, authority_class, extraction_method, "
            "extraction_model, confidence, task_id, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET claim_id=excluded.claim_id, "
            "extracted_claim=excluded.extracted_claim, "
            "exact_supporting_excerpt=excluded.exact_supporting_excerpt, "
            "locator=excluded.locator, authority_class=excluded.authority_class, "
            "extraction_method=excluded.extraction_method, "
            "extraction_model=excluded.extraction_model, confidence=excluded.confidence, "
            "task_id=excluded.task_id, metadata=excluded.metadata",
            (
                evidence.id,
                evidence.source_id,
                evidence.claim_id,
                evidence.extracted_claim,
                evidence.exact_supporting_excerpt,
                json.dumps(dict(evidence.locator), sort_keys=True),
                evidence.evidence_type,
                evidence.authority_class,
                evidence.extraction_method,
                evidence.extraction_model,
                evidence.confidence,
                evidence.task_id,
                evidence.created_at,
                json.dumps(dict(evidence.metadata), sort_keys=True),
            ),
        )
        for related_id in evidence.corroborates:
            await self._link(evidence.id, related_id, "corroborates")
        for related_id in evidence.contradicts:
            await self._link(evidence.id, related_id, "contradicts")
        if evidence.claim_id:
            await self._db.execute(
                "INSERT OR IGNORE INTO research_claim_evidence("
                "claim_id, evidence_id, task_id, created_at) VALUES (?, ?, ?, ?)",
                (evidence.claim_id, evidence.id, evidence.task_id, evidence.created_at),
            )
        self._generation += 1
        return evidence

    async def _link(self, evidence_id: str, related_id: str, relation: str) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO research_evidence_links("
            "evidence_id, related_evidence_id, relation) VALUES (?, ?, ?)",
            (evidence_id, related_id, relation),
        )

    async def get_evidence(self, evidence_id: str) -> EvidenceObject | None:
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT * FROM research_evidence WHERE id = ?", (evidence_id,)
        )
        if row is None:
            return None
        return EvidenceObject.from_record(await self._evidence_record(row))

    async def list_evidence(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        source_id: str | None = None,
        claim_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[EvidenceObject]:
        await self._ensure()
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None and project_id is not None:
            clauses.append("(e.task_id = ? OR (e.task_id IS NULL AND s.project_id = ?))")
            params.extend((task_id, project_id))
        elif task_id is not None:
            clauses.append("e.task_id = ?")
            params.append(task_id)
        elif project_id is not None:
            clauses.append("s.project_id = ?")
            params.append(project_id)
        for column, value in (("e.source_id", source_id), ("e.claim_id", claim_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if query:
            clauses.append("(e.extracted_claim LIKE ? OR e.exact_supporting_excerpt LIKE ?)")
            pattern = f"%{query}%"
            params.extend((pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._db.fetch_all(
            "SELECT e.* FROM research_evidence e "
            "JOIN research_sources s ON s.id = e.source_id"
            + where
            + " ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        )
        return [EvidenceObject.from_record(await self._evidence_record(row)) for row in rows]

    async def _evidence_record(self, row: dict[str, Any]) -> dict[str, Any]:
        record = _decode_row(row)
        links = await self._db.fetch_all(
            "SELECT related_evidence_id, relation FROM research_evidence_links "
            "WHERE evidence_id = ?",
            (record["id"],),
        )
        record["corroborates"] = [
            link["related_evidence_id"] for link in links if link["relation"] == "corroborates"
        ]
        record["contradicts"] = [
            link["related_evidence_id"] for link in links if link["relation"] == "contradicts"
        ]
        return record

    async def save_gap(self, gap: ResearchGap) -> ResearchGap:
        await self._ensure()
        await self._db.execute(
            "INSERT INTO research_gaps("
            "id, objective, question, kind, required, status, task_id, evidence_ids, "
            "created_at, resolved_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "evidence_ids=excluded.evidence_ids, resolved_at=excluded.resolved_at, "
            "metadata=excluded.metadata",
            (
                gap.id,
                gap.objective,
                gap.question,
                gap.kind,
                int(gap.required),
                gap.status,
                gap.task_id,
                json.dumps(list(gap.evidence_ids)),
                gap.created_at,
                gap.resolved_at,
                json.dumps(dict(gap.metadata), sort_keys=True),
            ),
        )
        self._generation += 1
        return gap

    async def list_gaps(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ResearchGap]:
        await self._ensure()
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._db.fetch_all(
            "SELECT * FROM research_gaps" + where + " ORDER BY created_at ASC, id ASC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        )
        return [ResearchGap.from_record(_decode_row(row)) for row in rows]

    async def close_gap(
        self,
        gap_id: str,
        *,
        evidence_ids: tuple[str, ...] = (),
        task_id: str | None = None,
    ) -> ResearchGap | None:
        gap = await self._get_gap(gap_id)
        if gap is None:
            return None
        if task_id is not None and gap.task_id != task_id:
            raise PermissionError("research gap belongs to another task")
        updated = ResearchGap(
            id=gap.id,
            objective=gap.objective,
            question=gap.question,
            kind=gap.kind,
            required=gap.required,
            status="CLOSED",
            task_id=gap.task_id,
            evidence_ids=tuple(evidence_ids),
            created_at=gap.created_at,
            resolved_at=gap.resolved_at or _now(),
            metadata=gap.metadata,
        )
        await self.save_gap(updated)
        return updated

    async def _get_gap(self, gap_id: str) -> ResearchGap | None:
        await self._ensure()
        row = await self._db.fetch_one("SELECT * FROM research_gaps WHERE id = ?", (gap_id,))
        return ResearchGap.from_record(_decode_row(row)) if row else None


def _decode_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    record = dict(row)
    for key in ("metadata", "locator", "evidence_ids"):
        value = record.get(key)
        if isinstance(value, str):
            try:
                record[key] = json.loads(value)
            except (TypeError, ValueError):
                record[key] = {} if key != "evidence_ids" else []
    return record


def _search_terms(query: str) -> list[str]:
    import re

    return [term for term in re.findall(r"[\w.-]+", str(query or "")) if term]


def _snippet(content: str, terms: list[str], width: int) -> str:
    folded = content.casefold()
    positions = [folded.find(term) for term in terms]
    position = min((item for item in positions if item >= 0), default=0)
    start = max(0, position - width // 3)
    end = min(len(content), start + width)
    if start:
        prefix = "…"
        start = max(0, start - 1)
    else:
        prefix = ""
    suffix = "…" if end < len(content) else ""
    return prefix + content[start:end].replace("\x00", " ") + suffix


def _now() -> str:
    from datetime import datetime

    return datetime.now(timezone.utc).isoformat()


__all__ = ["ResearchStore"]
