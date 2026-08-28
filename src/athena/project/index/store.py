"""Durable storage for project indexes."""

from __future__ import annotations

import json

from athena.project.index.models import ProjectIndex
from athena.protocol.messages import utcnow
from athena.state.database import Database


class ProjectIndexStore:
    """SQLite-backed latest index per canonical workspace root."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, index: ProjectIndex) -> None:
        now = utcnow().isoformat()
        await self._db.execute(
            "INSERT INTO project_indexes(root, index_revision, definition, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(root) DO UPDATE SET index_revision=excluded.index_revision, "
            "definition=excluded.definition, updated_at=excluded.updated_at",
            (
                index.root,
                index.index_revision,
                json.dumps(index.to_record(), sort_keys=True),
                index.built_at or now,
                now,
            ),
        )

    async def get(self, root: str) -> ProjectIndex | None:
        row = await self._db.fetch_one(
            "SELECT definition FROM project_indexes WHERE root = ?", (root,)
        )
        if row is None:
            return None
        try:
            value = json.loads(row["definition"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return ProjectIndex.from_record(value) if isinstance(value, dict) else None

    async def list(self, limit: int = 100) -> list[ProjectIndex]:
        rows = await self._db.fetch_all(
            "SELECT definition FROM project_indexes ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        )
        out: list[ProjectIndex] = []
        for row in rows:
            try:
                value = json.loads(row["definition"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                out.append(ProjectIndex.from_record(value))
        return out


__all__ = ["ProjectIndexStore"]
