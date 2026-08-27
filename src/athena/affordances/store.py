"""Durable records for promoted generated capabilities.

Task-local machinery is intentionally not stored here. Project/user records
are persisted with their source, hashes, validation proof, and scope so the
service can rehydrate them after restart without trusting an opaque closure.
"""

from __future__ import annotations

import json

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.state.database import Database


class GeneratedCapabilityStore:
    """SQLite-backed store for project and user generated machinery."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS generated_capabilities ("
            "id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL, "
            "project_scope TEXT, user_scope TEXT, definition TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_generated_scope_owner "
            "ON generated_capabilities(scope, owner)"
        )
        self._ready = True

    async def save(self, capability: GeneratedCapability, *, owner: str) -> None:
        if capability.scope not in {
            AffordanceScope.PROJECT, AffordanceScope.USER
        }:
            raise ValueError("only project/user generated capabilities are durable")
        if not owner:
            raise ValueError("durable generated capability requires an owner")
        if (capability.scope is AffordanceScope.PROJECT
                and capability.project_scope not in (None, owner)):
            raise ValueError("project capability owner does not match project_scope")
        if (capability.scope is AffordanceScope.USER
                and capability.user_scope not in (None, owner)):
            raise ValueError("user capability owner does not match user_scope")
        await self._ensure()
        from athena.protocol.messages import utcnow

        existing = await self._db.fetch_one(
            "SELECT scope, owner FROM generated_capabilities WHERE id = ?",
            (capability.id,),
        )
        if existing is not None and (
            existing["scope"] != capability.scope.value
            or existing["owner"] != owner
        ):
            raise ValueError(
                f"generated capability {capability.id} is owned by another scope")

        now = utcnow().isoformat()
        definition = capability.to_record()
        if capability.scope is AffordanceScope.PROJECT:
            definition["project_scope"] = capability.project_scope or owner
        else:
            definition["user_scope"] = capability.user_scope or owner
        definition["provenance"] = {
            **dict(capability.provenance),
            "owner": owner,
        }
        await self._db.execute(
            "INSERT INTO generated_capabilities("
            "id, scope, owner, project_scope, user_scope, definition, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET scope=excluded.scope, "
            "owner=excluded.owner, project_scope=excluded.project_scope, "
            "user_scope=excluded.user_scope, definition=excluded.definition, "
            "updated_at=excluded.updated_at",
            (
                capability.id,
                capability.scope.value,
                owner,
                (capability.project_scope or owner)
                if capability.scope is AffordanceScope.PROJECT else None,
                (capability.user_scope or owner)
                if capability.scope is AffordanceScope.USER else None,
                json.dumps(definition, sort_keys=True),
                now,
                now,
            ),
        )

    async def update_proof(
        self, capability_id: str, proof_record: dict,
    ) -> GeneratedCapability:
        """Persist updated validation/usage proof for a durable capability.

        Source and schema identity stay immutable while the operational proof
        grows with real executions.  This method deliberately updates the
        stored definition through the same ownership record; it cannot create
        a proof for an unknown or deleted capability.
        """
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT definition FROM generated_capabilities "
            "WHERE id = ? AND enabled = 1",
            (capability_id,),
        )
        if row is None:
            raise KeyError(f"unknown generated capability: {capability_id}")
        definition = json.loads(row["definition"])
        definition["proof_record"] = dict(proof_record)
        from athena.protocol.messages import utcnow

        await self._db.execute(
            "UPDATE generated_capabilities SET definition = ?, updated_at = ? "
            "WHERE id = ? AND enabled = 1",
            (json.dumps(definition, sort_keys=True), utcnow().isoformat(), capability_id),
        )
        return GeneratedCapability.from_record(definition)

    async def get(
        self, capability_id: str, *, project_id: str | None = None,
        user_id: str | None = None,
    ) -> GeneratedCapability | None:
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT scope, owner, definition FROM generated_capabilities "
            "WHERE id = ? AND enabled = 1", (capability_id,))
        if row is None:
            return None
        if row["scope"] == AffordanceScope.PROJECT.value \
                and project_id != row["owner"]:
            return None
        if row["scope"] == AffordanceScope.USER.value \
                and user_id != row["owner"]:
            return None
        return GeneratedCapability.from_record(json.loads(row["definition"]))

    async def list(
        self, *, project_id: str | None = None, user_id: str | None = None,
    ) -> list[GeneratedCapability]:
        await self._ensure()
        rows = await self._db.fetch_all(
            "SELECT scope, owner, definition FROM generated_capabilities "
            "WHERE enabled = 1 ORDER BY id")
        out: list[GeneratedCapability] = []
        for row in rows:
            capability = GeneratedCapability.from_record(
                json.loads(row["definition"]))
            if row["scope"] == AffordanceScope.PROJECT.value \
                    and row["owner"] != project_id:
                continue
            if row["scope"] == AffordanceScope.USER.value \
                    and row["owner"] != user_id:
                continue
            out.append(capability)
        return out

    async def delete(self, capability_id: str) -> None:
        await self._ensure()
        await self._db.execute(
            "DELETE FROM generated_capabilities WHERE id = ?", (capability_id,))


__all__ = ["GeneratedCapabilityStore"]
