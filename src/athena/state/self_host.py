"""Durable state for the bounded Athena self-host mission."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from athena.protocol.ids import new_id
from athena.protocol.messages import utcnow
from athena.state.database import Database


class SelfHostMissionStore:
    """Persist mission continuity without introducing another task engine."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        project_root: str,
        objective: str,
        task_id: str,
        base_revision: str,
        design_bundle_hash: str,
        gate_bundle_hash: str,
        plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        mission_id = new_id("mission")
        now = utcnow().isoformat()
        mission_plan = dict(plan or {"bounded": True, "step": 1})
        record = {
            "id": mission_id,
            "project_root": project_root,
            "objective": objective,
            "status": "active",
            "current_task_id": task_id,
            "base_revision": base_revision,
            "design_bundle_hash": design_bundle_hash,
            "gate_bundle_hash": gate_bundle_hash,
            "candidate_fingerprint": None,
            "plan": mission_plan,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        await self._db.execute(
            "INSERT INTO self_host_missions ("
            "id, project_root, objective, status, current_task_id, base_revision, "
            "design_bundle_hash, gate_bundle_hash, candidate_fingerprint, plan, "
            "last_error, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                project_root,
                objective,
                "active",
                task_id,
                base_revision,
                design_bundle_hash,
                gate_bundle_hash,
                None,
                json.dumps(mission_plan, sort_keys=True),
                None,
                now,
                now,
            ),
        )
        return record

    async def get(self, mission_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM self_host_missions WHERE id = ?", (mission_id,)
        )
        return _decode(row) if row is not None else None

    async def for_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM self_host_missions WHERE current_task_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (task_id,),
        )
        return _decode(row) if row is not None else None

    async def latest_active(self, project_root: str) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT * FROM self_host_missions WHERE project_root = ? "
            "AND status IN ('active', 'review', 'blocked') "
            "ORDER BY updated_at DESC LIMIT 1",
            (project_root,),
        )
        return _decode(row) if row is not None else None

    async def list_recent(self, project_root: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM self_host_missions WHERE project_root = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (project_root, max(1, min(int(limit), 100))),
        )
        return [_decode(row) for row in rows]

    async def update(self, mission_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "current_task_id",
            "candidate_fingerprint",
            "plan",
            "last_error",
            "base_revision",
            "design_bundle_hash",
            "gate_bundle_hash",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return await self.get(mission_id)
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in updates.items():
            assignments.append(f"{key} = ?")
            values.append(
                json.dumps(value, sort_keys=True)
                if key == "plan" and not isinstance(value, str)
                else value
            )
        assignments.append("updated_at = ?")
        values.extend((utcnow().isoformat(), mission_id))
        await self._db.execute(
            "UPDATE self_host_missions SET " + ", ".join(assignments) + " WHERE id = ?",
            tuple(values),
        )
        return await self.get(mission_id)


def _decode(row: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(row)
    try:
        record["plan"] = json.loads(record.get("plan") or "{}")
    except (TypeError, ValueError):
        record["plan"] = {}
    return record


__all__ = ["SelfHostMissionStore"]
