"""Versioned persistence for explicitly attached context blocks."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Mapping, Sequence

from athena.context.blocks import ContextBlock
from athena.context.provenance import provenance_from_mapping
from athena.protocol.ids import new_id
from athena.protocol.messages import Provenance, TrustClass, utcnow
from athena.state.database import Database


class ContextBlockStore:
    """SQLite-backed current state plus append-only block versions."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic revision for compiled-context cache invalidation."""
        return self._generation

    async def create(
        self,
        *,
        label: str,
        content: str,
        scope: str,
        scope_id: str,
        trust: TrustClass = TrustClass.AGENT_CURATED,
        max_tokens: int = 2_500,
        attached: bool = True,
        provenance: Provenance | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextBlock:
        label = _bounded_text(label, 256, "label")
        content = _bounded_text(content, max(1, max_tokens) * 4, "content")
        scope = _scope(scope)
        scope_id = _bounded_text(scope_id, 256, "scope_id")
        block_id = new_id("ctx")
        now = utcnow()
        block = ContextBlock(
            id=block_id,
            label=label,
            content=content,
            scope=scope,
            scope_id=scope_id,
            trust=trust,
            max_tokens=max(1, min(int(max_tokens), 32_000)),
            attached=bool(attached),
            version=1,
            provenance=provenance,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        await self._db.execute(
            "INSERT INTO context_blocks (id, label, content, scope, scope_id, "
            "trust, max_tokens, attached, version, provenance, metadata, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _params(block),
        )
        await self._write_version(block)
        self._generation += 1
        return block

    async def get(
        self, block_id: str, *, scope: str | None = None, scope_id: str | None = None
    ) -> ContextBlock | None:
        clauses = ["id = ?"]
        params: list[Any] = [block_id]
        if scope is not None:
            clauses.append("scope = ?")
            params.append(_scope(scope))
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        row = await self._db.fetch_one(
            "SELECT * FROM context_blocks WHERE " + " AND ".join(clauses), params
        )
        return _from_row(row) if row else None

    async def list(
        self,
        *,
        scopes: Sequence[tuple[str, str]] = (),
        attached_only: bool = False,
        limit: int = 100,
    ) -> list[ContextBlock]:
        limit = max(1, min(int(limit), 500))
        rows: list[dict[str, Any]]
        if scopes:
            pairs = [(_scope(s), sid) for s, sid in scopes]
            where = " OR ".join("(scope = ? AND scope_id = ?)" for _ in pairs)
            params: list[Any] = [value for pair in pairs for value in pair]
            suffix = " AND attached = 1" if attached_only else ""
            rows = await self._db.fetch_all(
                "SELECT * FROM context_blocks WHERE ("
                + where
                + ")"
                + suffix
                + " ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            )
        else:
            suffix = " WHERE attached = 1" if attached_only else ""
            rows = await self._db.fetch_all(
                "SELECT * FROM context_blocks" + suffix + " ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        return [_from_row(row) for row in rows]

    async def update(
        self,
        block_id: str,
        *,
        scope: str,
        scope_id: str,
        label: str | None = None,
        content: str | None = None,
        max_tokens: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> ContextBlock:
        current = await self.get(block_id, scope=scope, scope_id=scope_id)
        if current is None:
            raise KeyError(f"context block not found: {block_id}")
        if expected_version is not None and current.version != expected_version:
            raise ValueError(
                f"context block version conflict: expected {expected_version}, "
                f"current {current.version}"
            )
        tokens = max(1, min(int(max_tokens or current.max_tokens), 32_000))
        updated = ContextBlock(
            id=current.id,
            label=_bounded_text(label if label is not None else current.label, 256, "label"),
            content=_bounded_text(
                content if content is not None else current.content, tokens * 4, "content"
            ),
            scope=current.scope,
            scope_id=current.scope_id,
            trust=current.trust,
            max_tokens=tokens,
            attached=current.attached,
            version=current.version + 1,
            provenance=current.provenance,
            metadata=dict(current.metadata if metadata is None else metadata),
            created_at=current.created_at,
            updated_at=utcnow(),
        )
        await self._db.execute(
            "UPDATE context_blocks SET label = ?, content = ?, max_tokens = ?, "
            "version = ?, metadata = ?, updated_at = ? WHERE id = ? AND version = ?",
            (
                updated.label,
                updated.content,
                updated.max_tokens,
                updated.version,
                json.dumps(dict(updated.metadata), sort_keys=True),
                _dt(updated.updated_at),
                updated.id,
                current.version,
            ),
        )
        await self._write_version(updated)
        self._generation += 1
        return updated

    async def set_attached(
        self, block_id: str, *, scope: str, scope_id: str, attached: bool
    ) -> ContextBlock:
        current = await self.get(block_id, scope=scope, scope_id=scope_id)
        if current is None:
            raise KeyError(f"context block not found: {block_id}")
        if current.attached == attached:
            return current
        updated = ContextBlock(
            **{
                **current.__dict__,
                "attached": bool(attached),
                "version": current.version + 1,
                "updated_at": utcnow(),
            }
        )
        await self._db.execute(
            "UPDATE context_blocks SET attached = ?, version = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (
                1 if attached else 0,
                updated.version,
                _dt(updated.updated_at),
                updated.id,
                current.version,
            ),
        )
        await self._write_version(updated)
        self._generation += 1
        return updated

    async def history(
        self, block_id: str, *, scope: str, scope_id: str, limit: int = 100
    ) -> List[ContextBlock]:
        current = await self.get(block_id, scope=scope, scope_id=scope_id)
        if current is None:
            raise KeyError(f"context block not found: {block_id}")
        rows = await self._db.fetch_all(
            "SELECT * FROM context_block_versions WHERE block_id = ? ORDER BY version DESC LIMIT ?",
            (block_id, max(1, min(int(limit), 500))),
        )
        return [_from_row(row) for row in rows]

    async def _write_version(self, block: ContextBlock) -> None:
        await self._db.execute(
            "INSERT INTO context_block_versions (block_id, version, label, content, "
            "scope, scope_id, trust, max_tokens, attached, provenance, metadata, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _params(block, include_id=False, block_id=block.id),
        )


def _scope(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in {"task", "session", "project", "user", "global"}:
        raise ValueError("scope must be task, session, project, user, or global")
    return value


def _bounded_text(value: str, limit: int, field: str) -> str:
    value = str(value or "")
    if not value:
        raise ValueError(f"context block {field} is required")
    if len(value) > limit:
        raise ValueError(f"context block {field} exceeds {limit} characters")
    return value


def _dt(value: datetime | None) -> str:
    return (value or utcnow()).isoformat()


def _params(
    block: ContextBlock, *, include_id: bool = True, block_id: str | None = None
) -> tuple[Any, ...]:
    values: list[Any] = []
    if include_id:
        values.append(block.id)
        values.extend(
            [
                block.label,
                block.content,
                block.scope,
                block.scope_id,
                block.trust.value,
                block.max_tokens,
                1 if block.attached else 0,
                block.version,
                json.dumps(_provenance(block.provenance), sort_keys=True),
                json.dumps(dict(block.metadata), sort_keys=True),
                _dt(block.created_at),
                _dt(block.updated_at),
            ]
        )
    else:
        if block_id is None:
            raise ValueError("version row requires block_id")
        values.extend(
            [
                block_id,
                block.version,
                block.label,
                block.content,
                block.scope,
                block.scope_id,
                block.trust.value,
                block.max_tokens,
                1 if block.attached else 0,
                json.dumps(_provenance(block.provenance), sort_keys=True),
                json.dumps(dict(block.metadata), sort_keys=True),
                _dt(block.created_at),
                _dt(block.updated_at),
            ]
        )
    return tuple(values)


def _provenance(value: Provenance | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "source_type": value.source_type.value,
        "source_id": value.source_id,
        "trust": value.trust.value,
        "scope": value.scope,
        "created_at": _dt(value.created_at),
    }


def _from_row(row: Mapping[str, Any]) -> ContextBlock:
    raw_trust = str(row.get("trust") or TrustClass.AGENT_CURATED.value)
    trust = (
        TrustClass(raw_trust)
        if raw_trust in TrustClass._value2member_map_
        else TrustClass.AGENT_CURATED
    )
    raw_prov = _json(row.get("provenance"), {})
    provenance = provenance_from_mapping(raw_prov) if raw_prov else None
    return ContextBlock(
        id=str(row["id"] if "id" in row else row["block_id"]),
        label=str(row.get("label") or ""),
        content=str(row.get("content") or ""),
        scope=str(row.get("scope") or "project"),
        scope_id=str(row.get("scope_id") or ""),
        trust=trust,
        max_tokens=max(1, int(row.get("max_tokens") or 2_500)),
        attached=bool(row.get("attached")),
        version=max(1, int(row.get("version") or 1)),
        provenance=provenance,
        metadata=_json(row.get("metadata"), {}),
        created_at=_parse_dt(row.get("created_at")) or utcnow(),
        updated_at=_parse_dt(row.get("updated_at")),
    )


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if parsed is not None else default
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


__all__ = ["ContextBlockStore"]
