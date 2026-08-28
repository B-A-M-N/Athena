"""Durable records for promoted generated capabilities.

Task-local machinery is intentionally not stored here. Project/user records
are persisted with their source, hashes, validation proof, and scope so the
service can rehydrate them after restart without trusting an opaque closure.
"""

from __future__ import annotations

import json
from typing import List

from athena.affordances.models import AffordanceScope, GeneratedCapability
from athena.protocol.messages import utcnow
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
            AffordanceScope.CANDIDATE,
            AffordanceScope.PROJECT,
            AffordanceScope.USER,
        }:
            raise ValueError("only candidate/project/user generated capabilities are durable")
        if not owner:
            raise ValueError("durable generated capability requires an owner")
        if capability.scope is AffordanceScope.PROJECT and capability.project_scope not in (
            None,
            owner,
        ):
            raise ValueError("project capability owner does not match project_scope")
        if capability.scope is AffordanceScope.USER and capability.user_scope not in (None, owner):
            raise ValueError("user capability owner does not match user_scope")
        if capability.scope is AffordanceScope.CANDIDATE and capability.task_scope not in (
            None,
            owner,
        ):
            raise ValueError("candidate owner does not match task_scope")
        await self._ensure()
        existing = await self._db.fetch_one(
            "SELECT scope, owner, definition FROM generated_capabilities WHERE id = ?",
            (capability.id,),
        )
        promoting_candidate = (
            existing is not None
            and existing["scope"] == AffordanceScope.CANDIDATE.value
            and capability.scope
            in {
                AffordanceScope.PROJECT,
                AffordanceScope.USER,
            }
            and str(capability.provenance.get("task_id") or capability.task_scope or "")
            == existing["owner"]
        )
        if (
            existing is not None
            and not (existing["scope"] == capability.scope.value and existing["owner"] == owner)
            and not promoting_candidate
        ):
            raise ValueError(f"generated capability {capability.id} is owned by another scope")

        now = utcnow().isoformat()
        definition = capability.to_record()
        if promoting_candidate and existing is not None:
            previous = json.loads(existing["definition"])
            history = list(previous.get("lifecycle_history") or ())
            history.append(
                {
                    "event": "lifecycle_transition",
                    "from": previous.get("lifecycle_state", "CANDIDATE"),
                    "to": capability.lifecycle_state,
                    "reason": "explicit promotion",
                    "at": now,
                }
            )
            definition["lifecycle_history"] = history[-100:]
        elif existing is not None:
            # Repeated candidate/proof saves must not erase the durable
            # lifecycle trail carried by the prior upsert.  Avoid duplicating
            # an identical event while retaining newer usage-bearing records.
            previous = json.loads(existing["definition"])
            history = list(previous.get("lifecycle_history") or ())
            for event in definition.get("lifecycle_history") or ():
                if not history or dict(event) != dict(history[-1]):
                    history.append(dict(event))
            definition["lifecycle_history"] = history[-100:]
        if capability.scope is AffordanceScope.PROJECT:
            definition["project_scope"] = capability.project_scope or owner
        elif capability.scope is AffordanceScope.USER:
            definition["user_scope"] = capability.user_scope or owner
        else:
            definition["task_scope"] = capability.task_scope or owner
        definition["provenance"] = {
            **dict(capability.provenance),
            "owner": owner,
        }
        if not definition.get("lifecycle_history") and capability.lifecycle_state != "DRAFT":
            definition["lifecycle_history"] = [
                {
                    "event": "lifecycle_started",
                    "state": capability.lifecycle_state,
                    "owner": owner,
                    "at": now,
                }
            ]
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
                if capability.scope is AffordanceScope.PROJECT
                else None,
                (capability.user_scope or owner)
                if capability.scope is AffordanceScope.USER
                else None,
                json.dumps(definition, sort_keys=True),
                now,
                now,
            ),
        )

    async def update_proof(
        self,
        capability_id: str,
        proof_record: dict,
    ) -> GeneratedCapability:
        """Persist updated validation/usage proof for a durable capability.

        Source and schema identity stay immutable while the operational proof
        grows with real executions.  This method deliberately updates the
        stored definition through the same ownership record; it cannot create
        a proof for an unknown or deleted capability.
        """
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT definition FROM generated_capabilities WHERE id = ? AND enabled = 1",
            (capability_id,),
        )
        if row is None:
            raise KeyError(f"unknown generated capability: {capability_id}")
        definition = json.loads(row["definition"])
        definition["proof_record"] = dict(proof_record)
        usage = dict(proof_record.get("usage") or {})
        for key, value in {
            "use_count": usage.get("uses"),
            "success_count": usage.get("successes"),
            "failure_count": usage.get("failures"),
            "quality_score": proof_record.get("quality_score"),
            "last_used_at": proof_record.get("last_used_at"),
            "lifecycle_state": proof_record.get("lifecycle_state"),
        }.items():
            if value is not None:
                definition[key] = value
        history = list(definition.get("lifecycle_history") or ())
        history.append(
            {
                "event": "proof_updated",
                "at": utcnow().isoformat(),
                "usage": usage,
                "quality_score": definition.get("quality_score", 0.0),
            }
        )
        definition["lifecycle_history"] = history[-100:]

        await self._db.execute(
            "UPDATE generated_capabilities SET definition = ?, updated_at = ? "
            "WHERE id = ? AND enabled = 1",
            (json.dumps(definition, sort_keys=True), utcnow().isoformat(), capability_id),
        )
        return GeneratedCapability.from_record(definition)

    async def get(
        self,
        capability_id: str,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> GeneratedCapability | None:
        await self._ensure()
        row = await self._db.fetch_one(
            "SELECT scope, owner, definition FROM generated_capabilities "
            "WHERE id = ? AND enabled = 1",
            (capability_id,),
        )
        if row is None:
            return None
        if row["scope"] == AffordanceScope.PROJECT.value and project_id != row["owner"]:
            return None
        if row["scope"] == AffordanceScope.USER.value and user_id != row["owner"]:
            return None
        if row["scope"] == AffordanceScope.CANDIDATE.value and task_id != row["owner"]:
            return None
        return GeneratedCapability.from_record(json.loads(row["definition"]))

    async def list(
        self,
        *,
        project_id: str | None = None,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> list[GeneratedCapability]:
        await self._ensure()
        rows = await self._db.fetch_all(
            "SELECT scope, owner, definition FROM generated_capabilities "
            "WHERE enabled = 1 ORDER BY id"
        )
        out: list[GeneratedCapability] = []
        for row in rows:
            capability = GeneratedCapability.from_record(json.loads(row["definition"]))
            if row["scope"] == AffordanceScope.PROJECT.value and row["owner"] != project_id:
                continue
            if row["scope"] == AffordanceScope.USER.value and row["owner"] != user_id:
                continue
            if row["scope"] == AffordanceScope.CANDIDATE.value and row["owner"] != task_id:
                continue
            out.append(capability)
        return out

    async def transition(
        self,
        capability_id: str,
        lifecycle_state: str,
        *,
        owner: str | None = None,
        reason: str = "",
    ) -> GeneratedCapability:
        """Persist a lifecycle transition and retain its audit history."""
        await self._ensure()
        clauses = ["id = ?"]
        params: list[str] = [capability_id]
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        row = await self._db.fetch_one(
            "SELECT definition FROM generated_capabilities WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        if row is None:
            raise KeyError(f"unknown generated capability: {capability_id}")
        definition = json.loads(row["definition"])
        history = list(definition.get("lifecycle_history") or ())
        history.append(
            {
                "event": "lifecycle_transition",
                "from": definition.get("lifecycle_state", "DRAFT"),
                "to": lifecycle_state,
                "reason": reason,
                "at": utcnow().isoformat(),
            }
        )
        definition["lifecycle_state"] = lifecycle_state
        definition["lifecycle_history"] = history[-100:]
        enabled = 0 if lifecycle_state == "DEPRECATED" else 1
        await self._db.execute(
            "UPDATE generated_capabilities SET definition = ?, enabled = ?, "
            "updated_at = ? WHERE id = ?",
            (json.dumps(definition, sort_keys=True), enabled, utcnow().isoformat(), capability_id),
        )
        return GeneratedCapability.from_record(definition)

    async def find_equivalent(
        self,
        capability: GeneratedCapability,
        *,
        owner: str,
    ) -> GeneratedCapability | None:
        """Find an active same-source/schema affordance for one owner."""
        await self._ensure()
        rows = await self._db.fetch_all(
            "SELECT definition FROM generated_capabilities WHERE enabled = 1 "
            "AND scope = ? AND owner = ? ORDER BY updated_at DESC",
            (capability.scope.value, owner),
        )
        for row in rows:
            candidate = GeneratedCapability.from_record(json.loads(row["definition"]))
            if (
                candidate.code_hash == capability.code_hash
                and candidate.schema_hash == capability.schema_hash
                and candidate.declared_effects == capability.declared_effects
                and candidate.required_dependencies == capability.required_dependencies
            ):
                return candidate
        return None

    async def history(
        self,
        capability_id: str,
        *,
        owner: str | None = None,
    ) -> List[dict]:
        """Return lifecycle/proof history, including retired definitions."""
        await self._ensure()
        clauses = ["id = ?"]
        params: list[str] = [capability_id]
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        row = await self._db.fetch_one(
            "SELECT definition FROM generated_capabilities WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        if row is None:
            return []
        definition = json.loads(row["definition"])
        return [dict(event) for event in definition.get("lifecycle_history") or ()]

    async def delete(self, capability_id: str) -> None:
        await self._ensure()
        await self._db.execute("DELETE FROM generated_capabilities WHERE id = ?", (capability_id,))

    async def disable(self, capability_id: str, *, owner: str | None = None) -> bool:
        """Retire a durable definition without destroying its audit record."""
        await self._ensure()
        clauses = ["id = ?", "enabled = 1"]
        params: list[str] = [capability_id]
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        row = await self._db.fetch_one(
            "SELECT definition FROM generated_capabilities WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        if row is None:
            return False
        definition = json.loads(row["definition"])
        history = list(definition.get("lifecycle_history") or ())
        history.append(
            {
                "event": "lifecycle_transition",
                "from": definition.get("lifecycle_state", "ACTIVE"),
                "to": "DEPRECATED",
                "reason": "deprecated",
                "at": utcnow().isoformat(),
            }
        )
        definition["lifecycle_state"] = "DEPRECATED"
        definition["lifecycle_history"] = history[-100:]
        cursor = await self._db.execute(
            "UPDATE generated_capabilities SET enabled = 0, definition = ?, "
            "updated_at = ? WHERE " + " AND ".join(clauses),
            (json.dumps(definition, sort_keys=True), utcnow().isoformat(), *params),
        )
        return cursor.rowcount > 0

    async def garbage_collect(self, *, before: str | None = None) -> int:
        """Purge explicitly retired definitions beyond the retention boundary."""
        await self._ensure()
        if before is None:
            cursor = await self._db.execute("DELETE FROM generated_capabilities WHERE enabled = 0")
        else:
            cursor = await self._db.execute(
                "DELETE FROM generated_capabilities WHERE enabled = 0 AND updated_at < ?",
                (before,),
            )
        return int(cursor.rowcount or 0)


__all__ = ["GeneratedCapabilityStore"]
